"""Scrape runner: fetch a URL, dispatch by host, write Offer + PriceObservation.

The runner is the only place that touches the network OR the DB. The parsers
themselves are pure (HTML/JSON in, list of dicts out) so they're cheap to
unit-test.

Behavior locked in eng review:
  - 4D: sequential httpx calls, polite 1.5s delay between requests per host
  - 4A: prefetch_related friendly UPSERT (no per-row N+1 by routing all variants
    of one page through one bootstrap)
  - Tension C: each scrape writes a NEW PriceObservation row; offers UPSERT on
    (website, sku, public). Partial-scrape failure leaves prior PriceObservations
    visible in exports — last-good-price fallback.
  - 2F: 0-offer page raises NoOffersFound to bubble up to caller (Slack alert wired
    by H10).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from django.utils import timezone

from . import REGISTRY, get_spec
from .geant import ParsedOffer
from ..models import Brand, Channel, Offer, Page, PriceObservation, Retailer, Website

logger = logging.getLogger(__name__)

USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)
DEFAULT_TIMEOUT = 30.0
DEFAULT_DELAY_SECONDS = 1.5


class NoOffersFound(Exception):
    """Raised when a page returns 0 valid offers — H10 hook for Slack alerts."""


class UnsupportedHost(Exception):
    pass


@dataclass
class _Context:
    retailer: Retailer
    website: Website
    channel: Channel


def _bootstrap(spec) -> _Context:
    retailer, _ = Retailer.objects.get_or_create(name=spec.retailer_name)
    website, _ = Website.objects.get_or_create(
        host=spec.website_host, defaults={'scrapable': True}
    )
    channel, _ = Channel.objects.get_or_create(
        name=spec.channel_name, defaults={'retailer': retailer, 'website': website}
    )
    return _Context(retailer=retailer, website=website, channel=channel)


def fetch(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Plain httpx GET. follow_redirects=True so the Oxid 301 -> www works."""
    headers = {'User-Agent': USER_AGENT}
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def _ttc_to_ht_cents(ttc_cents: int, vat_rate: float) -> int:
    """Convert displayed-TTC price to stored-HT price, matching legacy semantics.

    Legacy: `(price_ttc * 100 / 121).round(2)` for BE, /120 for FR. We work in
    integer cents so a single round() suffices. vat_rate=0 leaves the value
    unchanged (used for sites that already display HT)."""
    if vat_rate <= 0:
        return ttc_cents
    return int(round(ttc_cents / (1 + vat_rate)))


def _persist_offers(parsed: list[ParsedOffer], page: Page, ctx: _Context, vat_rate: float) -> int:
    """UPSERT each parsed offer + create a fresh PriceObservation row. Returns
    the number of offers written."""
    written = 0
    for p in parsed:
        offer, _ = Offer.objects.update_or_create(
            website=ctx.website, sku=p.sku, public=True,
            defaults=dict(
                retailer=ctx.retailer,
                channel=ctx.channel,
                name=p.name,
                ean=p.ean,
                original_image_url=p.image_url,
            ),
        )
        offer.pages.add(page)
        PriceObservation.objects.create(
            offer=offer,
            price_cents=_ttc_to_ht_cents(p.price_cents, vat_rate),
            price_currency=p.price_currency,
            observed_at=timezone.now(),
        )
        written += 1
    return written


def scrape_url(url: str, *, html: str | None = None) -> int:
    """Process a single URL end-to-end. Returns the number of offers persisted.

    `html` lets callers inject pre-fetched HTML (for tests or bulk fetches done
    with shared http session).
    """
    host = urlparse(url).hostname or ''
    spec = get_spec(host)
    if spec is None:
        raise UnsupportedHost(f'No scraper registered for host {host!r}')

    if html is None:
        html = fetch(url)

    ctx = _bootstrap(spec)
    page, _ = Page.objects.get_or_create(
        url=url, defaults={'website': ctx.website}
    )

    try:
        parsed = list(spec.parse(html, page_url=url))
    except Exception as exc:
        page.last_error = f'{type(exc).__name__}: {exc}'
        page.last_status_code = None
        page.consecutive_failures = (page.consecutive_failures or 0) + 1
        page.save(update_fields=['last_error', 'last_status_code', 'consecutive_failures', 'updated_at'])
        raise

    if not parsed:
        page.scraped_at = timezone.now()
        page.last_error = 'NoOffersFound'
        page.consecutive_failures = (page.consecutive_failures or 0) + 1
        page.save(update_fields=['scraped_at', 'last_error', 'consecutive_failures', 'updated_at'])
        raise NoOffersFound(f'0 offers parsed from {url}')

    written = _persist_offers(parsed, page, ctx, vat_rate=spec.vat_rate)
    page.scraped_at = timezone.now()
    page.last_error = ''
    page.last_status_code = 200
    page.consecutive_failures = 0
    page.save(update_fields=['scraped_at', 'last_error', 'last_status_code',
                             'consecutive_failures', 'updated_at'])
    return written


def scrape_queue(*, limit: int = 100, delay: float = DEFAULT_DELAY_SECONDS,
                 max_age_hours: int = 12) -> dict[str, int]:
    """Walk the Page table and scrape any page not scraped in the last N hours.

    Sequential with sleep between requests (eng review 4D). Returns a counters
    dict for ops dashboards / Slack alerts (H10).
    """
    cutoff = timezone.now() - timedelta(hours=max_age_hours)
    pages = list(Page.objects.filter(
        website__scrapable=True
    ).filter(
        scraped_at__lt=cutoff
    ).order_by('scraped_at')[:limit])

    counters = {'pages_scraped': 0, 'offers_written': 0, 'failures': 0, 'no_offers': 0}
    for i, page in enumerate(pages):
        if i > 0:
            time.sleep(delay)
        try:
            written = scrape_url(page.url)
            counters['pages_scraped'] += 1
            counters['offers_written'] += written
        except NoOffersFound:
            counters['no_offers'] += 1
        except Exception:
            logger.exception('Scrape failed for %s', page.url)
            counters['failures'] += 1
    return counters

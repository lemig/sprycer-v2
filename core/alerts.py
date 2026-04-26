"""Lightweight Slack webhook poster.

Wired into scrape_queue (eng review 2F + PLAN H10) so the scheduled scrapers
emit an alert when an anomaly is detected — typically a 0-offer page (legacy
parser broke / site changed) or a fetch failure surge.

Set SLACK_WEBHOOK_URL in .env / Fly secrets to enable. Empty/unset is a no-op
(safe default for local dev + tests). All errors are swallowed so an alert
failure never crashes the scraper that needs to keep running.
"""
from __future__ import annotations

import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


def post_slack(text: str, *, channel: str | None = None) -> bool:
    """POST `text` to the configured Slack incoming webhook.

    Returns True on a 2xx response, False on any failure (or if no URL configured).
    Never raises.
    """
    url = getattr(settings, 'SLACK_WEBHOOK_URL', '') or ''
    if not url:
        logger.debug('SLACK_WEBHOOK_URL not set; skipping alert: %s', text[:80])
        return False

    payload: dict[str, object] = {'text': text}
    if channel:
        payload['channel'] = channel

    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            return True
    except httpx.HTTPError as exc:
        logger.warning('Slack alert failed: %s', exc)
        return False


def alert_scrape_run(counters: dict) -> None:
    """Emit a Slack alert if a scrape run shows any anomaly.

    counters comes from scrape_queue(): pages_scraped, offers_written,
    no_offers, failures. We alert when no_offers > 0 OR failures > 0.
    """
    no_offers = counters.get('no_offers', 0)
    failures = counters.get('failures', 0)
    if not (no_offers or failures):
        return

    pages_scraped = counters.get('pages_scraped', 0)
    offers_written = counters.get('offers_written', 0)
    text = (
        f':rotating_light: Scrape anomaly: '
        f'no_offers={no_offers} failures={failures} '
        f'(pages_scraped={pages_scraped} offers_written={offers_written})'
    )
    post_slack(text)

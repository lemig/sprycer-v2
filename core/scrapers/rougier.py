"""Rougier & Plé microdata parser.

Eng review 1D found that R&P uses schema.org MICRODATA (itemprop / itemscope /
itemtype) rather than JSON-LD. The legacy AJAX-based parser
(app/parsers/www_rougier_ple_fr/) is silently broken in production — the AJAX
endpoints it relied on no longer exist after a site redesign. v2 uses the
modern microdata path, ~50 LOC.

Coverage at cutover:
  - Single-product pages: handled
  - Color-variant pages: prices are loaded per-color via JS, not in microdata
    at the listing level. Returns 0 offers; H10's NoOffersFound alert fires.
    Plan: TODO post-cutover (variant page handling).
  - "Discriminant"-variant pages: many of these legacy URLs now 404.

Fields extracted per Product itemscope:
  - itemprop="price" (text content, comma decimal): TTC price
  - itemprop="priceCurrency" (meta content): currency (EUR)
  - itemprop="name" (h1 text): product name
  - itemprop="image" (img src): used to derive SKU via P_(\\d+)_P regex
  - itemprop="brand">name (nested Organization): brand name (parser doesn't
    use it directly; downstream Brand.find_or_create handles aliases)

Output is the same ParsedOffer dataclass the Géant parser returns, so the
runner code is unchanged.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .geant import ParsedOffer

SKU_RE = re.compile(r'P_(\d+)_P')

INSTOCK_TOKENS = {'instock', 'in_stock', 'http://schema.org/instock', 'https://schema.org/instock'}


def _is_in_stock(availability) -> bool:
    if not availability:
        return True
    token = str(availability).strip().lower()
    return any(t in token for t in INSTOCK_TOKENS)


def _price_to_cents(text: str | None) -> int | None:
    if not text:
        return None
    cleaned = text.strip().replace(',', '.').replace('\xa0', '').replace(' ', '')
    cleaned = re.sub(r'[^\d.]', '', cleaned)
    if not cleaned:
        return None
    try:
        return int(round(float(cleaned) * 100))
    except (TypeError, ValueError):
        return None


def _itemprop_text(scope, name: str) -> str:
    el = scope.find(attrs={'itemprop': name})
    if el is None:
        return ''
    return (el.get_text(strip=True) or el.get('content') or el.get('src') or '').strip()


def _sku_from_image(scope) -> str:
    img = scope.find('img', attrs={'itemprop': 'image'})
    if img is None:
        return ''
    src = img.get('src', '')
    match = SKU_RE.search(src)
    return match.group(1) if match else ''


def parse(html: str | bytes, page_url: str = '') -> list[ParsedOffer]:
    """Return at most one ParsedOffer for an R&P single-product page.

    Returns [] if the page has no product microdata, no SKU (image URL doesn't
    match the P_<id>_P pattern), or no price (variant pages with JS-loaded
    prices). Caller's NoOffersFound handler covers that.
    """
    if isinstance(html, bytes):
        html = html.decode('utf-8', errors='replace')

    soup = BeautifulSoup(html, 'html.parser')

    product_scope = soup.find(attrs={'itemtype': re.compile(r'schema\.org/Product$')})
    if product_scope is None:
        return []

    sku = _sku_from_image(product_scope)
    if not sku:
        return []

    price_text = _itemprop_text(product_scope, 'price')
    price_cents = _price_to_cents(price_text)
    if price_cents is None:
        return []

    offer_scope = product_scope.find(attrs={'itemtype': re.compile(r'schema\.org/Offer$')}) or product_scope
    availability = ''
    avail_el = offer_scope.find(attrs={'itemprop': 'availability'})
    if avail_el is not None:
        availability = (avail_el.get('href') or avail_el.get('content') or '').strip()
    if not _is_in_stock(availability):
        return []

    currency_el = product_scope.find(attrs={'itemprop': 'priceCurrency'})
    currency = (currency_el.get('content') if currency_el is not None else 'EUR') or 'EUR'

    name = _itemprop_text(product_scope, 'name')
    image_url = ''
    img = product_scope.find('img', attrs={'itemprop': 'image'})
    if img is not None:
        raw_src = img.get('src', '')
        # R&P emits relative paths (e.g. '/phproduct/.../P_141980_P_1.jpg').
        # Resolve against the page URL so Offer.original_image_url is a full
        # URL the export consumer can fetch / the match-review UI can render.
        image_url = urljoin(page_url, raw_src) if raw_src else ''

    return [ParsedOffer(
        sku=sku,
        name=name,
        ean='',  # no itemprop=gtin13 on R&P pages
        image_url=image_url,
        page_url=page_url,
        price_cents=price_cents,
        price_currency=currency,
        in_stock=True,
    )]

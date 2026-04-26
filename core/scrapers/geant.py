"""Géant des Beaux-Arts JSON-LD parser (covers .be and .fr — same Oxid eShop).

Verified live 2026-04-26 against
  https://www.geant-beaux-arts.be/peinture-acrylique-darwi-for-you.html
  https://www.geant-beaux-arts.fr/pastel-sec-carre-cretacolor.html

Structure (both domains identical):
  - One <script type='application/ld+json'> block per product page
  - Content is a JSON LIST of length 1 wrapping a ProductGroup (eng review found
    this; PLAN.md's snippet showed a dict — handle both)
  - ProductGroup.hasVariant is the variant array (40-72 typical)
  - Each variant has sku, gtin13, name, image, url, plus a single offers dict
  - offers has price (string), priceCurrency, availability ('http://schema.org/InStock'
    or '...OutOfStock')

This module is PURE: bytes/str in, list[dict] out. The DB/HTTP runner is in
core/scrapers/runner.py.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

JSONLD_RE = re.compile(
    r'<script[^>]*type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>',
    re.S | re.I,
)

INSTOCK_TOKENS = {'instock', 'in_stock', 'http://schema.org/instock', 'https://schema.org/instock'}


@dataclass
class ParsedOffer:
    sku: str
    name: str
    ean: str
    image_url: str
    page_url: str
    price_cents: int | None
    price_currency: str
    in_stock: bool

    def as_dict(self) -> dict:
        return {
            'sku': self.sku,
            'name': self.name,
            'ean': self.ean,
            'image_url': self.image_url,
            'page_url': self.page_url,
            'price_cents': self.price_cents,
            'price_currency': self.price_currency,
            'in_stock': self.in_stock,
        }


def _is_in_stock(availability) -> bool:
    if not availability:
        return True  # absence = treat as available, matches Oxid default
    token = str(availability).strip().lower()
    return any(t in token for t in INSTOCK_TOKENS)


def _price_to_cents(price: object) -> int | None:
    if price is None:
        return None
    try:
        return int(round(float(str(price).replace(',', '.')) * 100))
    except (TypeError, ValueError):
        return None


def _walk_jsonld(blocks: list) -> list[dict]:
    """Flatten outer list/dict structures and yield Product / ProductGroup dicts."""
    out: list[dict] = []
    for block in blocks:
        if isinstance(block, list):
            out.extend(_walk_jsonld(block))
        elif isinstance(block, dict):
            t = block.get('@type')
            if t in ('Product', 'ProductGroup'):
                out.append(block)
    return out


def _parse_offers(offers) -> tuple[int | None, str, bool]:
    """Schema.org Offer can be a dict or list. Pick the first usable one."""
    if isinstance(offers, list):
        offer = offers[0] if offers else {}
    elif isinstance(offers, dict):
        offer = offers
    else:
        return (None, 'EUR', True)
    return (
        _price_to_cents(offer.get('price')),
        str(offer.get('priceCurrency', 'EUR')) or 'EUR',
        _is_in_stock(offer.get('availability')),
    )


def parse_jsonld(html: str | bytes, page_url: str = '') -> list[ParsedOffer]:
    """Return one ParsedOffer per variant (or per simple Product) for a page.

    Skips out-of-stock variants. Skips entries without a usable sku or price.
    """
    if isinstance(html, bytes):
        html = html.decode('utf-8', errors='replace')

    blocks: list = []
    for raw in JSONLD_RE.findall(html):
        try:
            blocks.append(json.loads(raw.strip()))
        except json.JSONDecodeError:
            continue

    products = _walk_jsonld(blocks)
    out: list[ParsedOffer] = []

    for prod in products:
        if prod.get('@type') == 'ProductGroup':
            variants = prod.get('hasVariant') or []
            for v in variants:
                offer = _build_parsed_offer(v, page_url)
                if offer:
                    out.append(offer)
        else:  # plain Product (single-offer page)
            offer = _build_parsed_offer(prod, page_url)
            if offer:
                out.append(offer)

    return out


def _build_parsed_offer(node: dict, page_url: str) -> ParsedOffer | None:
    sku = str(node.get('sku') or '').strip()
    if not sku:
        return None
    price_cents, currency, in_stock = _parse_offers(node.get('offers'))
    if not in_stock:
        return None
    if price_cents is None:
        return None
    return ParsedOffer(
        sku=sku,
        name=str(node.get('name') or '').strip(),
        ean=str(node.get('gtin13') or '').strip(),
        image_url=str(node.get('image') or '').strip(),
        page_url=str(node.get('url') or page_url or '').strip(),
        price_cents=price_cents,
        price_currency=currency,
        in_stock=in_stock,
    )

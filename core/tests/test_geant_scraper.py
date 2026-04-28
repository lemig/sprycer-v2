"""Tests for the Géant JSON-LD parser + scrape runner.

Parser tests use synthesized JSON-LD (cleaner + IP-safe than real HTML
snapshots). Runner tests inject HTML via the `html=` kwarg to avoid network
calls during unit-test runs.
"""
import json

import pytest

from core.models import Channel, Offer, Page, PriceObservation, Retailer, Website
from core.scrapers import get_spec
from core.scrapers.geant import parse_jsonld
from core.scrapers.runner import (
    NoOffersFound,
    UnsupportedHost,
    scrape_queue,
    scrape_url,
)


# ---- Helper: wrap a dict in the Oxid HTML envelope -----------------------

def _html_with(jsonld_obj) -> str:
    payload = json.dumps(jsonld_obj, ensure_ascii=False)
    return (
        '<html><head>'
        f'<script type="application/ld+json">{payload}</script>'
        '</head><body>...</body></html>'
    )


def _variant(sku='V1', price='2.44', availability='http://schema.org/InStock',
             gtin='5411711463445', name=None):
    return {
        '@type': 'Product',
        'sku': sku,
        'gtin13': gtin,
        'name': name or f'Variant {sku}',
        'image': f'https://images.example/{sku}.jpg',
        'url': f'https://www.geant-beaux-arts.be/page-{sku}.html',
        'offers': {
            '@type': 'Offer',
            'availability': availability,
            'price': price,
            'priceCurrency': 'EUR',
        },
    }


def _product_group(variants):
    return [{
        '@context': 'http://schema.org',
        '@type': 'ProductGroup',
        'name': 'Test Group',
        'sku': 'TG',
        'description': '...',
        'image': 'https://images.example/group.jpg',
        'url': 'https://www.geant-beaux-arts.be/test.html',
        'hasVariant': variants,
    }]


# ---- Pure parser tests --------------------------------------------------


class TestParseJsonld:
    def test_outer_list_with_product_group(self):
        html = _html_with(_product_group([_variant(sku='A', price='1.50')]))
        offers = parse_jsonld(html)
        assert len(offers) == 1
        assert offers[0].sku == 'A'
        assert offers[0].price_cents == 150

    def test_outer_dict_with_product_group(self):
        # Older form (matches PLAN.md snippet); should still parse
        html = _html_with(_product_group([_variant(sku='A')])[0])
        offers = parse_jsonld(html)
        assert len(offers) == 1

    def test_plain_product_without_variants(self):
        html = _html_with({'@type': 'Product', 'sku': 'SOLO', 'name': 'Solo Item',
                           'gtin13': '999', 'image': 'x.jpg',
                           'offers': {'@type': 'Offer', 'price': '5.99', 'priceCurrency': 'EUR',
                                      'availability': 'http://schema.org/InStock'}})
        offers = parse_jsonld(html)
        assert len(offers) == 1
        assert offers[0].sku == 'SOLO'
        assert offers[0].price_cents == 599

    def test_skips_out_of_stock(self):
        html = _html_with(_product_group([
            _variant(sku='A', availability='http://schema.org/InStock'),
            _variant(sku='B', availability='http://schema.org/OutOfStock'),
        ]))
        offers = parse_jsonld(html)
        assert {o.sku for o in offers} == {'A'}

    def test_skips_no_price(self):
        html = _html_with(_product_group([
            _variant(sku='A', price='1.00'),
            _variant(sku='B', price=None),
        ]))
        offers = parse_jsonld(html)
        assert {o.sku for o in offers} == {'A'}

    def test_skips_no_sku(self):
        # Variant with empty sku is dropped
        v = _variant(sku='A')
        v_bad = _variant(sku='')
        html = _html_with(_product_group([v, v_bad]))
        offers = parse_jsonld(html)
        assert {o.sku for o in offers} == {'A'}

    def test_offers_can_be_list(self):
        # Some Schema.org publishers ship offers as a list — pick first usable
        v = _variant(sku='A')
        v['offers'] = [
            {'@type': 'Offer', 'price': '1.00', 'priceCurrency': 'EUR', 'availability': 'http://schema.org/InStock'},
        ]
        html = _html_with(_product_group([v]))
        offers = parse_jsonld(html)
        assert offers[0].price_cents == 100

    def test_comma_decimal_price(self):
        v = _variant(sku='A', price='2,44')
        html = _html_with(_product_group([v]))
        offers = parse_jsonld(html)
        assert offers[0].price_cents == 244

    def test_availability_token_variants(self):
        forms = [
            'InStock',
            'http://schema.org/InStock',
            'https://schema.org/InStock',
            'http://schema.org/inStock',
        ]
        for token in forms:
            html = _html_with(_product_group([_variant(sku='A', availability=token)]))
            assert len(parse_jsonld(html)) == 1, f'failed for token {token!r}'

    def test_availability_missing_treated_as_in_stock(self):
        v = _variant(sku='A')
        del v['offers']['availability']
        html = _html_with(_product_group([v]))
        assert len(parse_jsonld(html)) == 1

    def test_multiple_jsonld_blocks_only_picks_products(self):
        html = (
            '<html><head>'
            '<script type="application/ld+json">{"@type": "Organization", "name": "Géant"}</script>'
            f'<script type="application/ld+json">{json.dumps(_product_group([_variant()]))}</script>'
            '</head></html>'
        )
        offers = parse_jsonld(html)
        assert len(offers) == 1

    def test_invalid_json_block_ignored(self):
        html = (
            '<html><script type="application/ld+json">not json</script>'
            f'<script type="application/ld+json">{json.dumps(_product_group([_variant()]))}</script>'
            '</html>'
        )
        offers = parse_jsonld(html)
        assert len(offers) == 1

    def test_currency_extracted(self):
        v = _variant(sku='A')
        v['offers']['priceCurrency'] = 'GBP'
        html = _html_with(_product_group([v]))
        assert parse_jsonld(html)[0].price_currency == 'GBP'

    def test_gtin13_becomes_ean(self):
        v = _variant(sku='A', gtin='5411711463445')
        html = _html_with(_product_group([v]))
        assert parse_jsonld(html)[0].ean == '5411711463445'

    def test_no_jsonld_at_all_returns_empty(self):
        html = '<html><body>no script</body></html>'
        assert parse_jsonld(html) == []


# ---- Registry -----------------------------------------------------------


class TestRegistry:
    def test_geant_be_registered(self):
        spec = get_spec('www.geant-beaux-arts.be')
        assert spec is not None
        assert spec.retailer_name == 'Le Géant des Beaux-Arts (BE)'

    def test_geant_fr_registered(self):
        spec = get_spec('www.geant-beaux-arts.fr')
        assert spec is not None
        assert spec.retailer_name == 'Le Géant des Beaux-Arts (FR)'

    def test_unknown_host_returns_none(self):
        assert get_spec('example.com') is None


# ---- Runner integration ------------------------------------------------


@pytest.mark.django_db
class TestScrapeUrl:
    URL = 'https://www.geant-beaux-arts.be/test-page.html'

    def _html(self, variants):
        return _html_with(_product_group(variants))

    def test_bootstraps_retailer_channel_website(self):
        html = self._html([_variant(sku='A', price='1.00')])
        scrape_url(self.URL, html=html)
        assert Retailer.objects.filter(name='Le Géant des Beaux-Arts (BE)').exists()
        assert Website.objects.filter(host='www.geant-beaux-arts.be', scrapable=True).exists()
        assert Channel.objects.filter(name='www.geant-beaux-arts.be').exists()

    def test_creates_page_row(self):
        scrape_url(self.URL, html=self._html([_variant(sku='A')]))
        page = Page.objects.get(url=self.URL)
        assert page.scraped_at is not None
        assert page.last_status_code == 200

    def test_writes_one_offer_per_variant(self):
        html = self._html([
            _variant(sku='A', price='1.00'),
            _variant(sku='B', price='2.00'),
            _variant(sku='C', price='3.00'),
        ])
        written = scrape_url(self.URL, html=html)
        assert written == 3
        assert Offer.objects.count() == 3

    def test_writes_fresh_price_observation_per_scrape_with_be_vat(self):
        # BE: 21% VAT. €1.00 TTC -> round(100 / 1.21) = 83 cents HT.
        # €1.50 TTC -> round(150 / 1.21) = 124 cents HT.
        html_v1 = self._html([_variant(sku='A', price='1.00')])
        html_v2 = self._html([_variant(sku='A', price='1.50')])
        scrape_url(self.URL, html=html_v1)
        scrape_url(self.URL, html=html_v2)
        offer = Offer.objects.get(sku='A')
        prices = list(offer.price_observations.order_by('observed_at').values_list('price_cents', flat=True))
        assert prices == [83, 124]

    def test_fr_vat_conversion(self):
        # FR: 20% VAT. €2.44 TTC -> round(244 / 1.20) = 203 cents HT.
        url = 'https://www.geant-beaux-arts.fr/test.html'
        scrape_url(url, html=self._html([_variant(sku='A', price='2.44')]))
        offer = Offer.objects.get(sku='A')
        po = offer.price_observations.first()
        assert po.price_cents == 203

    def test_re_scrape_upserts_offer_no_duplicate(self):
        scrape_url(self.URL, html=self._html([_variant(sku='A', price='1.00')]))
        scrape_url(self.URL, html=self._html([_variant(sku='A', price='2.00')]))
        assert Offer.objects.filter(sku='A').count() == 1

    def test_offer_pages_m2m_populated(self):
        scrape_url(self.URL, html=self._html([_variant(sku='A')]))
        offer = Offer.objects.get(sku='A')
        assert list(offer.pages.values_list('url', flat=True)) == [self.URL]

    def test_no_offers_raises_and_marks_page(self):
        html = self._html([_variant(sku='X', availability='http://schema.org/OutOfStock')])
        with pytest.raises(NoOffersFound):
            scrape_url(self.URL, html=html)
        page = Page.objects.get(url=self.URL)
        assert page.consecutive_failures == 1
        assert page.last_error == 'NoOffersFound'

    def test_unknown_host_raises(self):
        with pytest.raises(UnsupportedHost):
            scrape_url('https://example.com/x.html', html='<html></html>')

    def test_consecutive_failures_resets_on_success(self):
        # First scrape fails (no offers) -> failures=1
        with pytest.raises(NoOffersFound):
            scrape_url(self.URL, html=self._html([_variant(sku='X', availability='http://schema.org/OutOfStock')]))
        # Second scrape succeeds
        scrape_url(self.URL, html=self._html([_variant(sku='Y', price='1.00')]))
        page = Page.objects.get(url=self.URL)
        assert page.consecutive_failures == 0


@pytest.mark.django_db
class TestScrapeQueue:
    """Regression: never-scraped pages (scraped_at IS NULL) must be picked up
    by scrape_queue. Without the IS NULL clause they'd be invisible forever."""

    def test_null_scraped_at_pages_are_picked_up(self):
        from core.models import Page, Website
        # A freshly seeded page on a known scrapable host
        web, _ = Website.objects.get_or_create(host='www.geant-beaux-arts.be',
                                               defaults={'scrapable': True})
        Page.objects.create(url='https://www.geant-beaux-arts.be/never-scraped.html',
                            website=web)

        # Run with delay=0 to skip sleeps; html injected via monkey patch isn't easy here,
        # so we accept that scrape_url will try to fetch and fail. The point is that the
        # queue *selects* the page — fetch fails are counted as failures, not skipped.
        # We patch scrape_url to a no-op for the assertion.
        from core.scrapers import runner
        called = []
        original = runner.scrape_url
        runner.scrape_url = lambda url, **kw: called.append(url) or 0
        try:
            counters = scrape_queue(limit=10, delay=0, max_age_hours=12)
        finally:
            runner.scrape_url = original

        assert called == ['https://www.geant-beaux-arts.be/never-scraped.html']
        assert counters['pages_scraped'] == 1

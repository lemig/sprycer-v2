"""Tests for the Rougier & Plé microdata parser.

Synthesized fixtures mirror the live page structure verified 2026-04-26 against
https://www.rougier-ple.fr/aquarelle-super-fine-van-gogh-demi-godet.r.html.
"""
import pytest

from core.scrapers import get_spec
from core.scrapers.rougier import parse
from core.scrapers.runner import NoOffersFound, scrape_url
from core.models import Channel, Offer, PriceObservation, Retailer, Website


def _product_html(*, sku='141980', price='2,80', name='Aquarelle Van Gogh',
                  image_path=None, brand='Van Gogh', currency='EUR',
                  availability='http://schema.org/InStock'):
    """Build a synthesized R&P product page with microdata mirroring real structure."""
    if image_path is None:
        image_path = f'/phproduct/20120605/P_{sku}_P_1_PRODUIT.jpg' if sku else '/no-sku.jpg'
    avail_attr = f'<meta itemprop="availability" content="{availability}" />' if availability else ''
    return f'''
<html>
<body>
<div itemscope itemtype="http://schema.org/Product">
  <h1 itemprop="name">{name}</h1>
  <img src="{image_path}" itemprop="image" />
  <div itemprop="brand" itemscope itemtype="http://schema.org/Organization">
    <span itemprop="name">{brand}</span>
  </div>
  <div itemscope itemtype="http://schema.org/Offer">
    <span itemprop="price">{price}</span>€
    <meta itemprop="priceCurrency" content="{currency}" />
    {avail_attr}
  </div>
</div>
</body>
</html>
'''


# ---- Pure parser tests ---------------------------------------------------


class TestParseRougier:
    def test_extracts_sku_from_image_url(self):
        html = _product_html(sku='141980')
        offers = parse(html)
        assert len(offers) == 1
        assert offers[0].sku == '141980'

    def test_extracts_name(self):
        html = _product_html(name='Pinceau scolaire Raphaël rond')
        assert parse(html)[0].name == 'Pinceau scolaire Raphaël rond'

    def test_comma_decimal_price_to_cents(self):
        # R&P always shows TTC with comma decimal in microdata
        html = _product_html(price='2,80')
        assert parse(html)[0].price_cents == 280

    def test_period_decimal_also_works(self):
        html = _product_html(price='2.80')
        assert parse(html)[0].price_cents == 280

    def test_currency_extracted(self):
        html = _product_html(currency='EUR')
        assert parse(html)[0].price_currency == 'EUR'

    def test_no_product_microdata_returns_empty(self):
        html = '<html><body>Not a product page.</body></html>'
        assert parse(html) == []

    def test_no_image_no_sku_returns_empty(self):
        html = '''
<html><body>
<div itemscope itemtype="http://schema.org/Product">
  <h1 itemprop="name">Bare product</h1>
  <span itemprop="price">1,00</span>
</div></body></html>'''
        assert parse(html) == []

    def test_image_without_sku_pattern_returns_empty(self):
        # Image path that does not match P_<digits>_P (e.g., a generic logo)
        html = _product_html(image_path='/static/logo.jpg')
        assert parse(html) == []

    def test_no_price_returns_empty(self):
        # Variant pages: prices loaded per-color via JS, microdata has no price
        html = '''
<html><body>
<div itemscope itemtype="http://schema.org/Product">
  <h1 itemprop="name">Color variant page</h1>
  <img src="/phproduct/x/P_141980_P_1.jpg" itemprop="image" />
</div></body></html>'''
        assert parse(html) == []

    def test_out_of_stock_returns_empty(self):
        html = _product_html(availability='http://schema.org/OutOfStock')
        assert parse(html) == []

    def test_image_url_preserved(self):
        html = _product_html(image_path='/phproduct/20120605/P_141980_P_1_PRODUIT.jpg')
        assert parse(html)[0].image_url == '/phproduct/20120605/P_141980_P_1_PRODUIT.jpg'

    def test_no_ean_for_rougier(self):
        # R&P doesn't expose gtin13 anywhere in microdata
        assert parse(_product_html())[0].ean == ''

    def test_thousands_separator_in_price(self):
        # R&P shows '1 234,56' for high prices (French locale grouping with NBSP)
        html = _product_html(price='1\xa0234,56')
        assert parse(html)[0].price_cents == 123456


# ---- Registry ------------------------------------------------------------


class TestRougierRegistry:
    def test_rougier_registered(self):
        spec = get_spec('www.rougier-ple.fr')
        assert spec is not None
        assert spec.retailer_name == 'Rougier & Plé'
        assert spec.vat_rate == 0.20


# ---- Runner integration --------------------------------------------------


@pytest.mark.django_db
class TestScrapeRougier:
    URL = 'https://www.rougier-ple.fr/aquarelle-super-fine-van-gogh-demi-godet.r.html'

    def test_scrape_writes_offer_with_ht_price(self):
        # 2,80 TTC -> round(280 / 1.20) = 233 cents HT
        scrape_url(self.URL, html=_product_html(sku='141980', price='2,80'))
        offer = Offer.objects.get(sku='141980')
        assert offer.retailer.name == 'Rougier & Plé'
        po = offer.price_observations.first()
        assert po.price_cents == 233
        assert po.price_currency == 'EUR'

    def test_re_scrape_upserts_offer_appends_observation(self):
        scrape_url(self.URL, html=_product_html(sku='141980', price='2,80'))
        scrape_url(self.URL, html=_product_html(sku='141980', price='3,00'))
        offer = Offer.objects.get(sku='141980')
        assert Offer.objects.filter(sku='141980').count() == 1
        # 3,00 / 1.20 = 2.50 -> 250 cents
        prices = list(offer.price_observations.order_by('observed_at').values_list('price_cents', flat=True))
        assert prices == [233, 250]

    def test_no_microdata_raises_no_offers(self):
        with pytest.raises(NoOffersFound):
            scrape_url(self.URL, html='<html><body>404 not found</body></html>')

    def test_bootstraps_rougier_context(self):
        scrape_url(self.URL, html=_product_html())
        assert Retailer.objects.filter(name='Rougier & Plé').exists()
        assert Channel.objects.filter(name='rougier-ple.fr').exists()
        assert Website.objects.filter(host='www.rougier-ple.fr', scrapable=True).exists()

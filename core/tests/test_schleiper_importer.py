"""Tests for the Schleiper catalog importer.

The pure transform logic (no DB) lives in TestTransform. The DB-touching
behavior lives in TestRunImporter and uses the mini CSV fixture under
core/tests/fixtures/schleiper_mini.csv.

The fixture covers the eng-review-flagged transforms:
  - Color ID -> name suffix " n° {id}"
  - express?=yes -> eshopexpress channel
  - express?=blank -> onlinecatalogue channel
  - Prix HTVA accepts both '1234.56' and '12,50'
  - Sprycer ID present -> UPSERT keeps the same pk
  - Deleted=true -> soft delete (public=False)
  - Brand alias normalization
"""
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from pathlib import Path

from core.importers.schleiper import (
    SchleiperImporter,
    _clean_str,
    _parse_price_cents,
    _truthy,
    transform_row,
)
from core.models import Brand, Channel, Import, Offer, PriceObservation, Retailer, Website


FIXTURE = Path(__file__).parent / 'fixtures' / 'schleiper_mini.csv'


# ---- Pure logic ----------------------------------------------------------


class TestTruthy:
    @pytest.mark.parametrize('val,expected', [
        ('yes', True), ('YES', True), ('Yes', True),
        ('true', True), ('TRUE', True), ('1', True), ('y', True),
        ('oui', True),
        (True, True), (1, True), (1.5, True),
        ('no', False), ('false', False), ('0', False), ('', False), (None, False),
        (False, False), (0, False), (0.0, False),
        ('  yes  ', True),  # whitespace tolerated
    ])
    def test_truthy(self, val, expected):
        assert _truthy(val) is expected

    def test_truthy_handles_pandas_nan(self):
        import math
        assert _truthy(math.nan) is False


class TestCleanStr:
    def test_none(self):
        assert _clean_str(None) == ''

    def test_pandas_nan(self):
        import math
        assert _clean_str(math.nan) == ''

    def test_strips_whitespace(self):
        assert _clean_str('  hello  ') == 'hello'


class TestParsePriceCents:
    @pytest.mark.parametrize('val,expected', [
        ('12.50', 1250),
        ('12,50', 1250),    # comma decimal (European format)
        ('12', 1200),
        ('1234.56', 123456),
        ('', None),
        (None, None),
        ('not-a-number', None),
        ('  12.50  ', 1250),
    ])
    def test_parse(self, val, expected):
        assert _parse_price_cents(val) == expected


class TestTransformRow:
    def test_color_id_suffix(self):
        row = {'RefEtiq': 'X', 'Article_FR': 'Marker', 'Color ID': '724'}
        out = transform_row(row)
        assert out.name == 'Marker n° 724'

    def test_no_color_id_no_suffix(self):
        row = {'RefEtiq': 'X', 'Article_FR': 'Marker', 'Color ID': ''}
        out = transform_row(row)
        assert out.name == 'Marker'

    def test_express_truthy_channel(self):
        row = {'RefEtiq': 'X', 'Article_FR': 'A', 'express?': 'yes'}
        out = transform_row(row)
        assert out.is_express is True
        assert out.channel_name() == 'schleiper.com/eshopexpress'

    def test_express_falsy_channel(self):
        row = {'RefEtiq': 'X', 'Article_FR': 'A', 'express?': ''}
        out = transform_row(row)
        assert out.is_express is False
        assert out.channel_name() == 'schleiper.com/onlinecatalogue'

    def test_categorie_split(self):
        row = {'RefEtiq': 'X', 'Article_FR': 'A', 'Categorie': 'Beaux-Arts > Marqueurs > Doubles'}
        out = transform_row(row)
        assert out.categories == ['Beaux-Arts', 'Marqueurs', 'Doubles']

    def test_categorie_blank(self):
        row = {'RefEtiq': 'X', 'Article_FR': 'A', 'Categorie': ''}
        out = transform_row(row)
        assert out.categories == []

    def test_deleted_truthy(self):
        row = {'RefEtiq': 'X', 'Article_FR': 'A', 'Deleted': 'true'}
        out = transform_row(row)
        assert out.is_deleted is True

    def test_sprycer_id_parsed_when_numeric(self):
        row = {'RefEtiq': 'X', 'Article_FR': 'A', 'Sprycer ID': '124397'}
        out = transform_row(row)
        assert out.sprycer_id == 124397

    def test_sprycer_id_none_when_blank(self):
        row = {'RefEtiq': 'X', 'Article_FR': 'A', 'Sprycer ID': ''}
        out = transform_row(row)
        assert out.sprycer_id is None


# ---- Full-pipeline DB tests --------------------------------------------


@pytest.fixture
def import_obj(db):
    User = get_user_model()
    user = User.objects.create_user(username='miguel', password='x')
    file = SimpleUploadedFile(
        FIXTURE.name, FIXTURE.read_bytes(), content_type='text/csv'
    )
    return Import.objects.create(
        user=user, file=file,
        importer_class_name='SchleiperImporter',
        status=Import.Status.UNPROCESSED,
    )


@pytest.mark.django_db
class TestRunImporter:
    def test_creates_schleiper_context_idempotently(self, import_obj):
        SchleiperImporter().run(import_obj)
        # Bootstrap rows exist exactly once.
        assert Retailer.objects.filter(name='Schleiper').count() == 1
        assert Website.objects.filter(host='www.schleiper.com').count() == 1
        assert Channel.objects.filter(name='schleiper.com/eshopexpress').count() == 1
        assert Channel.objects.filter(name='schleiper.com/onlinecatalogue').count() == 1

    def test_imports_all_rows_with_failure_count_zero(self, import_obj):
        result = SchleiperImporter().run(import_obj)
        assert result.total == 5
        assert result.failures == 0
        assert Offer.objects.count() == 5

    def test_color_id_appears_in_name(self, import_obj):
        SchleiperImporter().run(import_obj)
        offer = Offer.objects.get(sku='WINPMY724')
        assert offer.name.endswith(' n° 724')

    def test_express_routes_to_eshopexpress_channel(self, import_obj):
        SchleiperImporter().run(import_obj)
        offer = Offer.objects.get(sku='WINPMY724')
        assert offer.channel.name == 'schleiper.com/eshopexpress'

    def test_blank_express_routes_to_onlinecatalogue(self, import_obj):
        SchleiperImporter().run(import_obj)
        offer = Offer.objects.get(sku='CANVAS800')
        assert offer.channel.name == 'schleiper.com/onlinecatalogue'

    def test_sprycer_id_preserved_as_pk(self, import_obj):
        SchleiperImporter().run(import_obj)
        offer = Offer.objects.get(sku='WINPMY724')
        assert offer.pk == 124397

    def test_deleted_row_is_soft_deleted(self, import_obj):
        SchleiperImporter().run(import_obj)
        offer = Offer.objects.get(sku='DELETED01')
        assert offer.public is False

    def test_comma_decimal_price_parsed_correctly(self, import_obj):
        SchleiperImporter().run(import_obj)
        offer = Offer.objects.get(sku='EUROPRICE')
        latest_price = offer.price_observations.order_by('-observed_at').first()
        assert latest_price is not None
        assert latest_price.price_cents == 1250

    def test_prices_recorded_for_each_priced_row(self, import_obj):
        SchleiperImporter().run(import_obj)
        # 4 of 5 rows have a price (DELETED01 also has 5.00, so all 5 do).
        assert PriceObservation.objects.count() == 5

    def test_brand_created_with_alias_normalization(self, import_obj):
        SchleiperImporter().run(import_obj)
        # "Winsor & Newton" should land as Brand row
        offer = Offer.objects.get(sku='WINPMY724')
        assert offer.brand is not None
        assert offer.brand.name == 'Winsor & Newton'

    def test_categorie_stored_as_array(self, import_obj):
        SchleiperImporter().run(import_obj)
        offer = Offer.objects.get(sku='WINPMY724')
        assert offer.categories == ['Beaux-Arts', 'Marqueurs']

    def test_import_record_marked_completed(self, import_obj):
        SchleiperImporter().run(import_obj)
        import_obj.refresh_from_db()
        assert import_obj.status == Import.Status.COMPLETED
        assert import_obj.total == 5
        assert import_obj.failures == 0

    def test_re_run_is_upsert_not_duplicate(self, import_obj, db):
        """UPSERT regression — running the same import twice doesn't create
        duplicate Offer rows. (Tension B's preserve-human-work principle is
        also relevant here for matchings, but for offers we just want UPSERT.)
        """
        SchleiperImporter().run(import_obj)
        first_count = Offer.objects.count()
        # Reset import status and run again
        import_obj.status = Import.Status.UNPROCESSED
        import_obj.save()
        SchleiperImporter().run(import_obj)
        assert Offer.objects.count() == first_count

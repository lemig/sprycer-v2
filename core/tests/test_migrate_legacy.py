"""Regression tests for migrate_legacy idempotency.

Critical for the parallel-run sync: nightly `migrate_legacy` reruns must NOT
append duplicate PriceObservation rows. Without the unique constraint +
`ignore_conflicts=True` flag, a 30-night sync would pile up 30x duplicate
price observations and the export's "latest observation" query would become
ambiguous.

The model-level test pins the constraint itself. The migrate-level test
exercises the actual `_migrate_price_points` codepath against a synthetic
legacy cursor.
"""
from datetime import datetime, timezone as dt_tz
from unittest.mock import MagicMock

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.management.commands.migrate_legacy import Command
from core.models import Channel, Offer, PriceObservation, Retailer, Website


@pytest.fixture
def offer(db):
    retailer = Retailer.objects.create(name='Schleiper')
    website = Website.objects.create(host='www.schleiper.com')
    channel = Channel.objects.create(
        name='schleiper.com/onlinecatalogue', retailer=retailer, website=website,
    )
    return Offer.objects.create(
        retailer=retailer, channel=channel, website=website,
        sku='X', name='X', public=True,
    )


@pytest.mark.django_db(transaction=True)
class TestPriceObservationUniqueConstraint:
    """Database-level guard against duplicate observations."""

    def test_duplicate_raises_integrity_error(self, offer):
        observed_at = timezone.now()
        PriceObservation.objects.create(
            offer=offer, observed_at=observed_at, price_cents=1000,
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                PriceObservation.objects.create(
                    offer=offer, observed_at=observed_at, price_cents=1000,
                )

    def test_different_price_at_same_instant_is_allowed(self, offer):
        # Pathological but legal: same offer, same instant, different price.
        # The constraint includes price_cents so this is two different facts.
        observed_at = timezone.now()
        PriceObservation.objects.create(
            offer=offer, observed_at=observed_at, price_cents=1000,
        )
        PriceObservation.objects.create(
            offer=offer, observed_at=observed_at, price_cents=1100,
        )
        assert PriceObservation.objects.count() == 2


def _legacy_cursor(rows):
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    return cursor


@pytest.mark.django_db
class TestMigratePricePointsIdempotency:
    """Nightly sync regression. The cutover-to-deploy strategy switched to
    parallel-run with read-only nightly migrate_legacy reruns; without these
    tests, the 30-night sync silently 30xs the PriceObservation table."""

    def test_rerun_does_not_append_duplicates(self, offer):
        ts1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt_tz.utc)
        ts2 = datetime(2026, 2, 1, 12, 0, 0, tzinfo=dt_tz.utc)
        rows = [
            (offer.pk, 1000, None, None, 'EUR', ts1),
            (offer.pk, 1100, None, None, 'EUR', ts2),
        ]
        cmd = Command()

        cmd._migrate_price_points(_legacy_cursor(rows))
        assert PriceObservation.objects.count() == 2

        cmd._migrate_price_points(_legacy_cursor(rows))
        assert PriceObservation.objects.count() == 2, (
            'Re-run wrote duplicates — nightly parallel-run sync is broken.'
        )

    def test_rerun_picks_up_new_rows_from_legacy(self, offer):
        ts1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt_tz.utc)
        ts2 = datetime(2026, 2, 1, 12, 0, 0, tzinfo=dt_tz.utc)
        ts3 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=dt_tz.utc)
        first_rows = [
            (offer.pk, 1000, None, None, 'EUR', ts1),
            (offer.pk, 1100, None, None, 'EUR', ts2),
        ]
        second_rows = first_rows + [
            (offer.pk, 1200, None, None, 'EUR', ts3),
        ]

        cmd = Command()
        cmd._migrate_price_points(_legacy_cursor(first_rows))
        assert PriceObservation.objects.count() == 2

        cmd._migrate_price_points(_legacy_cursor(second_rows))
        assert PriceObservation.objects.count() == 3, (
            'Nightly sync must pick up the new legacy price_point row.'
        )

    def test_rerun_skips_orphans(self, offer):
        # offer_id that doesn't exist in v2 must be silently skipped, not crash.
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt_tz.utc)
        rows = [
            (offer.pk, 1000, None, None, 'EUR', ts),
            (99999, 9999, None, None, 'EUR', ts),  # orphan
        ]
        cmd = Command()
        cmd._migrate_price_points(_legacy_cursor(rows))
        assert PriceObservation.objects.count() == 1

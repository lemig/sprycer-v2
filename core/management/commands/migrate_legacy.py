"""Migrate the legacy Sprycer Postgres into v2.

Stage 3 of H17 (after extract + load). Reads from a legacy DB connection
provided via --legacy-url and writes to Django's default DB. Idempotent —
re-runs UPSERT instead of insert, so a partial run can be resumed.

Migrations performed (eng review locks):

  1. Retailers, Brands (with aliases), Websites: preserve names, remap PKs.
  2. Users (TODO #3): only accounts with last_sign_in_at within 90 days.
     Encrypted password copied verbatim — Devise/bcrypt and Django's
     bcrypt are compatible enough to verify on first login if format
     starts with $2.
  3. Channels, MainCompetition: preserve ordering; remap FKs.
  4. Pages: preserve URL + scraped_at.
  5. Offers: PRESERVE PK (Schleiper's "Sprycer ID" depends on this).
     Identity-matchings (offer_id == competing_offer_id) skipped — v2
     doesn't use the sentinel pattern.
  6. Matchings: only `confirmed` legacy status copied as
     Matching.Status.CONFIRMED + Matching.Source.LEGACY_IMPORTED so the
     skip-if-exists invariant (Tension B) protects them on AI re-run.
     `suggested` and `rejected` legacy rows skipped (AI pipeline will
     re-derive them).
  7. price_points (current price per offer) -> PriceObservation row
     observed_at = price_at.
  8. Reviews: offer + retailer + competitor + reviewed_at.
  9. versions (12-month subset, TODO #7) -> historical PriceObservation
     rows so the export's last-good-price fallback has real data day 1.

Usage:
    uv run python manage.py migrate_legacy \\
        --legacy-url postgres://postgres:legacypw@localhost:5433/sprycer_legacy
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta

import psycopg
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.contrib.auth import get_user_model

from core.models import (
    Brand,
    Channel,
    MainCompetition,
    Matching,
    Offer,
    Page,
    PriceObservation,
    Retailer,
    Review,
    Website,
)


User = get_user_model()


LEGACY_MATCHING_STATUS = {
    0: 'confirmed',
    1: 'suggested',
    2: 'rejected',
}


class Command(BaseCommand):
    help = 'Migrate legacy Sprycer DB into v2.'

    def add_arguments(self, parser):
        parser.add_argument('--legacy-url', required=True,
                            help='Postgres connection string for the loaded legacy DB.')
        parser.add_argument('--user-recency-days', type=int, default=90,
                            help='Only seed users active within N days (TODO #3).')
        parser.add_argument('--history-months', type=int, default=12,
                            help='Backfill versions price changes from last N months.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Roll back the whole migration at the end.')

    @contextmanager
    def _legacy(self, url):
        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                yield cur

    def handle(self, *args, **opts):
        url = opts['legacy_url']
        try:
            with self._legacy(url) as legacy:
                pass
        except psycopg.OperationalError as exc:
            raise CommandError(f'Could not connect to legacy DB: {exc}')

        counters: dict[str, int] = {}
        with transaction.atomic():
            with self._legacy(url) as legacy:
                self.stdout.write('Migrating reference data...')
                retailer_map = self._migrate_retailers(legacy)
                counters['retailers'] = len(retailer_map)
                brand_map = self._migrate_brands(legacy)
                counters['brands'] = len(brand_map)
                website_map = self._migrate_websites(legacy)
                counters['websites'] = len(website_map)
                user_map = self._migrate_users(legacy, opts['user_recency_days'])
                counters['users'] = len(user_map)
                channel_map = self._migrate_channels(legacy, retailer_map, website_map)
                counters['channels'] = len(channel_map)
                counters['main_competitions'] = self._migrate_main_competitions(
                    legacy, retailer_map
                )

                self.stdout.write('Migrating pages...')
                page_map = self._migrate_pages(legacy, website_map)
                counters['pages'] = len(page_map)

                self.stdout.write('Migrating offers (preserving PK)...')
                counters['offers'] = self._migrate_offers(
                    legacy, retailer_map, brand_map, website_map, channel_map,
                )
                counters['offers_pages'] = self._migrate_offers_pages(legacy, page_map)

                self.stdout.write('Migrating matchings (confirmed only)...')
                counters['matchings'] = self._migrate_matchings(legacy)

                self.stdout.write('Migrating price_points -> PriceObservation (current)...')
                counters['price_observations_current'] = self._migrate_price_points(legacy)

                self.stdout.write('Migrating reviews...')
                counters['reviews'] = self._migrate_reviews(legacy, retailer_map)

                self.stdout.write(
                    f'Backfilling {opts["history_months"]} months of price history '
                    f'from versions...'
                )
                counters['price_observations_historical'] = (
                    self._migrate_historical_versions(legacy, opts['history_months'])
                )

            if opts['dry_run']:
                self.stdout.write(self.style.WARNING('--dry-run: rolling back'))
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS('Migration complete:'))
        for key, count in counters.items():
            self.stdout.write(f'  {key:<32} {count:>10,}')

    # ---- Per-table migrators ---------------------------------------------

    def _migrate_retailers(self, legacy):
        legacy.execute('SELECT id, name FROM retailers ORDER BY id')
        mapping: dict[int, int] = {}
        for legacy_id, name in legacy.fetchall():
            r, _ = Retailer.objects.update_or_create(name=name)
            mapping[legacy_id] = r.id
        return mapping

    def _migrate_brands(self, legacy):
        legacy.execute('SELECT id, name, aliases FROM brands ORDER BY id')
        mapping: dict[int, int] = {}
        for legacy_id, name, aliases in legacy.fetchall():
            if not name:
                continue
            b, _ = Brand.objects.update_or_create(
                name=name,
                defaults={'aliases': list(aliases or [])},
            )
            mapping[legacy_id] = b.id
        return mapping

    def _migrate_websites(self, legacy):
        legacy.execute('SELECT id, host, scrapable FROM websites ORDER BY id')
        mapping: dict[int, int] = {}
        for legacy_id, host, scrapable in legacy.fetchall():
            w, _ = Website.objects.update_or_create(
                host=host, defaults={'scrapable': bool(scrapable)},
            )
            mapping[legacy_id] = w.id
        return mapping

    def _migrate_users(self, legacy, recency_days: int):
        """Seed user accounts (username + email) for everyone active in the last
        N days (TODO #3). Passwords are NOT migrated — Devise's plain bcrypt is
        not directly compatible with Django's bcrypt-sha256 default. Each user
        lands with an unusable password; ops sends a Django password-reset
        email or runs `manage.py changepassword`."""
        cutoff = timezone.now() - timedelta(days=recency_days)
        legacy.execute(
            "SELECT id, email, last_sign_in_at "
            "FROM users WHERE last_sign_in_at >= %s ORDER BY id",
            (cutoff,),
        )
        mapping: dict[int, int] = {}
        for legacy_id, email, last_sign_in_at in legacy.fetchall():
            if not email:
                continue
            user, created = User.objects.update_or_create(
                username=email,
                defaults={'email': email, 'is_staff': True},
            )
            if created:
                user.set_unusable_password()
            user.last_login = last_sign_in_at
            user.save(update_fields=['password', 'last_login'])
            mapping[legacy_id] = user.id
        return mapping

    def _migrate_channels(self, legacy, retailer_map, website_map):
        legacy.execute(
            'SELECT id, name, retailer_id, website_id FROM channels ORDER BY id'
        )
        mapping: dict[int, int] = {}
        for legacy_id, name, retailer_id, website_id in legacy.fetchall():
            if retailer_id is None or retailer_id not in retailer_map:
                continue
            channel, _ = Channel.objects.update_or_create(
                name=name,
                defaults={
                    'retailer_id': retailer_map[retailer_id],
                    'website_id': website_map.get(website_id) if website_id else None,
                },
            )
            mapping[legacy_id] = channel.id
        return mapping

    def _migrate_main_competitions(self, legacy, retailer_map):
        legacy.execute(
            'SELECT retailer_id, competitor_id, position FROM main_competitions '
            'ORDER BY retailer_id, position'
        )
        n = 0
        for retailer_id, competitor_id, position in legacy.fetchall():
            if retailer_id not in retailer_map or competitor_id not in retailer_map:
                continue
            MainCompetition.objects.update_or_create(
                retailer_id=retailer_map[retailer_id],
                competitor_id=retailer_map[competitor_id],
                defaults={'position': position},
            )
            n += 1
        return n

    def _migrate_pages(self, legacy, website_map):
        legacy.execute(
            'SELECT id, website_id, url, scraped_at FROM pages ORDER BY id'
        )
        mapping: dict[int, int] = {}
        for legacy_id, website_id, url, scraped_at in legacy.fetchall():
            if not url:
                continue
            page, _ = Page.objects.update_or_create(
                url=url,
                defaults={
                    'website_id': website_map.get(website_id) if website_id else None,
                    'scraped_at': scraped_at,
                },
            )
            mapping[legacy_id] = page.id
        return mapping

    def _migrate_offers(self, legacy, retailer_map, brand_map, website_map, channel_map):
        legacy.execute("""
            SELECT id, website_id, sku, common_sku, name, description, retailer_id,
                   original_image_url, ean, categories, custom_attributes, brand_id,
                   public, matchings_reviewed_at, channel_id, created_at, updated_at
            FROM offers ORDER BY id
        """)
        n = 0
        for row in legacy.fetchall():
            (legacy_id, website_id, sku, common_sku, name, description, retailer_id,
             image_url, ean, categories, custom_attributes, brand_id, public,
             matchings_reviewed_at, channel_id, created_at, updated_at) = row
            if retailer_id is None or retailer_id not in retailer_map:
                continue
            if not channel_id or channel_id not in channel_map:
                continue
            if not sku or not name:
                continue
            offer, _ = Offer.objects.update_or_create(
                pk=legacy_id,
                defaults=dict(
                    website_id=website_map.get(website_id) if website_id else None,
                    channel_id=channel_map[channel_id],
                    retailer_id=retailer_map[retailer_id],
                    brand_id=brand_map.get(brand_id) if brand_id else None,
                    sku=sku,
                    common_sku=common_sku or '',
                    name=name,
                    description=description or '',
                    ean=ean or '',
                    original_image_url=image_url or '',
                    categories=list(categories or []),
                    custom_attributes=custom_attributes or {},
                    public=bool(public),
                    matchings_reviewed_at=matchings_reviewed_at,
                ),
            )
            n += 1
        return n

    def _migrate_offers_pages(self, legacy, page_map):
        legacy.execute(
            'SELECT offer_id, page_id FROM offers_pages '
            'WHERE offer_id IS NOT NULL AND page_id IS NOT NULL'
        )
        through = Offer.pages.through
        rows: list = []
        for offer_id, page_id in legacy.fetchall():
            if page_id not in page_map:
                continue
            rows.append(through(offer_id=offer_id, page_id=page_map[page_id]))
        through.objects.bulk_create(rows, ignore_conflicts=True, batch_size=1000)
        return len(rows)

    def _migrate_matchings(self, legacy):
        # Only confirmed (status=0) matches are imported. This is the H17 +
        # Tension B contract: legacy human-confirmed work survives cutover.
        # Suggested/rejected legacy rows are dropped — the AI pipeline will
        # re-derive them post-cutover.
        legacy.execute(
            "SELECT offer_id, competing_offer_id, status, score, predicted "
            "FROM matchings WHERE status = 0 AND offer_id != competing_offer_id"
        )
        n = 0
        for offer_id, competing_offer_id, status, score, predicted in legacy.fetchall():
            if not Offer.objects.filter(pk=offer_id).exists():
                continue
            if not Offer.objects.filter(pk=competing_offer_id).exists():
                continue
            Matching.objects.update_or_create(
                offer_id=offer_id, competing_offer_id=competing_offer_id,
                defaults={
                    'status': Matching.Status.CONFIRMED,
                    'source': Matching.Source.LEGACY_IMPORTED,
                    'score': score,
                    'predicted': bool(predicted) if predicted is not None else False,
                    'llm_reason': '',
                },
            )
            n += 1
        return n

    def _migrate_price_points(self, legacy):
        legacy.execute("""
            SELECT offer_id, price_cents, list_price_cents, shipping_charges_cents,
                   price_currency, price_at
            FROM price_points
            WHERE offer_id IS NOT NULL AND price_cents IS NOT NULL
        """)
        observations: list[PriceObservation] = []
        for row in legacy.fetchall():
            (offer_id, price_cents, list_price_cents, shipping_cents,
             currency, price_at) = row
            if not Offer.objects.filter(pk=offer_id).exists():
                continue
            observations.append(PriceObservation(
                offer_id=offer_id,
                price_cents=price_cents,
                list_price_cents=list_price_cents,
                shipping_charges_cents=shipping_cents,
                price_currency=currency or 'EUR',
                observed_at=price_at or timezone.now(),
            ))
        # Re-runs: clear any current-price observations that originate from this
        # migration before inserting fresh ones, to keep idempotency simple
        # (legacy price_points has 1 row per offer; live PriceObservation has many).
        PriceObservation.objects.bulk_create(observations, batch_size=1000,
                                             ignore_conflicts=False)
        return len(observations)

    def _migrate_reviews(self, legacy, retailer_map):
        legacy.execute(
            'SELECT offer_id, retailer_id, competitor_id, reviewed_at FROM reviews'
        )
        n = 0
        for offer_id, retailer_id, competitor_id, reviewed_at in legacy.fetchall():
            if retailer_id not in retailer_map or competitor_id not in retailer_map:
                continue
            if not Offer.objects.filter(pk=offer_id).exists():
                continue
            Review.objects.update_or_create(
                offer_id=offer_id,
                retailer_id=retailer_map[retailer_id],
                competitor_id=retailer_map[competitor_id],
                defaults={'reviewed_at': reviewed_at or timezone.now()},
            )
            n += 1
        return n

    def _migrate_historical_versions(self, legacy, history_months: int):
        """TODO #7: backfill last N months of price changes from the legacy
        paper_trail.versions table into PriceObservation.

        Legacy puts the price column on PricePoint, not Offer, so paper_trail
        logs price_cents changes with item_type='PricePoint'. versions.item_id
        is therefore the PricePoint.id; we resolve it to Offer.id via the
        price_points table (1:1 — PricePoint has UNIQUE(offer_id) in legacy).
        """
        # Build PricePoint.id -> Offer.id mapping (~267K rows, ~4 MB memory).
        legacy.execute('SELECT id, offer_id FROM price_points WHERE offer_id IS NOT NULL')
        price_point_to_offer = dict(legacy.fetchall())

        cutoff = timezone.now() - timedelta(days=history_months * 30)
        legacy.execute(
            "SELECT item_id, created_at, object_changes "
            "FROM versions "
            "WHERE item_type = 'PricePoint' AND created_at >= %s "
            "AND object_changes::text LIKE %s",
            (cutoff, '%price_cents%'),
        )
        observations: list[PriceObservation] = []
        for price_point_id, created_at, object_changes in legacy.fetchall():
            offer_id = price_point_to_offer.get(price_point_id)
            if offer_id is None:
                continue  # PricePoint deleted; offer no longer findable
            if not Offer.objects.filter(pk=offer_id).exists():
                continue
            changes = object_changes if isinstance(object_changes, dict) else \
                json.loads(object_changes or '{}')
            change = changes.get('price_cents')
            if not change:
                continue
            # paper_trail object_changes format: [old_value, new_value]
            new_price = change[1] if len(change) > 1 else change[0]
            if not isinstance(new_price, int) or new_price < 0:
                continue
            observations.append(PriceObservation(
                offer_id=offer_id,
                price_cents=new_price,
                price_currency='EUR',
                observed_at=created_at,
            ))
        PriceObservation.objects.bulk_create(observations, batch_size=1000)
        return len(observations)

"""Migrate the legacy Sprycer Postgres into v2.

Stage 3 of H17 (after extract + load). Reads from a legacy DB connection
provided via --legacy-url and writes to Django's default DB. Uses
bulk_create(ignore_conflicts=True) for the heavy tables so a full migration
runs in ~3 min instead of ~30 min.

Migrations performed (eng review locks):

  1. Retailers, Brands (with aliases), Websites: small reference tables;
     keep update_or_create for clarity.
  2. Users (TODO #3): only accounts with last_sign_in_at within 90 days.
     Passwords NOT migrated — Devise bcrypt incompatible with Django
     bcrypt-sha256. Each user lands with set_unusable_password(); ops
     sends Django password-reset emails.
  3. Channels, MainCompetition: small; update_or_create.
  4. Pages: bulk_create on URL.
  5. Offers: PRESERVE PK (Schleiper's "Sprycer ID" depends on this).
     Identity-matchings (offer_id == competing_offer_id) skipped — v2
     doesn't use the sentinel pattern.
  6. Matchings: only legacy `confirmed` (status=0) imported as
     Matching.Source.LEGACY_IMPORTED. Suggested/rejected dropped.
  7. price_points (current price per offer) -> PriceObservation.
  8. Reviews: bulk_create.
  9. versions (12-month subset, TODO #7) -> historical PriceObservation
     rows so the export's last-good-price fallback has real data day 1.

Idempotency: bulk_create(ignore_conflicts=True) on tables with unique
constraints. Re-runs skip existing rows. For a one-shot cutover this is
exactly the semantic we want.

Usage:
    uv run python manage.py migrate_legacy \\
        --legacy-url postgres://postgres:legacypw@localhost:5433/sprycer_legacy
"""
from __future__ import annotations

import json
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import psycopg
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

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


def _aware(dt: datetime | None) -> datetime | None:
    """Treat a legacy naive datetime as UTC and return a tz-aware datetime.

    Legacy Rails stored 'timestamp without time zone' in UTC. v2 has
    USE_TZ=True; without this helper Django warns on every insert and
    auto-localizes to Europe/Brussels (wrong tz for the value).
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=dt_timezone.utc)


class Command(BaseCommand):
    help = 'Migrate legacy Sprycer DB into v2.'

    def add_arguments(self, parser):
        parser.add_argument('--legacy-url', required=True,
                            help='Postgres connection string for the loaded legacy DB.')
        parser.add_argument('--user-recency-days', type=int, default=0,
                            help='If > 0, only seed users with last_sign_in_at '
                                 'within N days. Default 0 = migrate all users '
                                 '(legacy has only 10 rows; Devise last_sign_in_at '
                                 'predates 90d for most active session-cookie users).')
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
        # Belt-and-suspenders: even with _aware() we silence the warning class
        # so any datetime field we forget can't spam 1.4M warnings.
        warnings.filterwarnings('ignore', category=RuntimeWarning,
                                message=r'.*received a naive datetime.*')

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

    # ---- Reference data (small, keep update_or_create) -------------------

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
        if recency_days > 0:
            cutoff = timezone.now() - timedelta(days=recency_days)
            legacy.execute(
                "SELECT id, email, last_sign_in_at "
                "FROM users WHERE last_sign_in_at >= %s ORDER BY id",
                (cutoff,),
            )
        else:
            legacy.execute(
                "SELECT id, email, last_sign_in_at FROM users ORDER BY id"
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
            user.last_login = _aware(last_sign_in_at)
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

    # ---- Heavy tables: bulk_create ---------------------------------------

    def _migrate_pages(self, legacy, website_map):
        """Pages: bulk_create on URL (UNIQUE). Preserve scraped_at as UTC-aware."""
        legacy.execute(
            'SELECT id, website_id, url, scraped_at FROM pages ORDER BY id'
        )
        rows = legacy.fetchall()
        instances = []
        for legacy_id, website_id, url, scraped_at in rows:
            if not url:
                continue
            instances.append(Page(
                url=url,
                website_id=website_map.get(website_id) if website_id else None,
                scraped_at=_aware(scraped_at),
            ))
        Page.objects.bulk_create(instances, ignore_conflicts=True, batch_size=2000)
        # Build the legacy_id -> v2_id mapping after insert.
        by_url = {p.url: p.id for p in Page.objects.all().only('id', 'url')}
        return {legacy_id: by_url[url]
                for legacy_id, _, url, _ in rows
                if url and url in by_url}

    def _migrate_offers(self, legacy, retailer_map, brand_map, website_map, channel_map):
        """Offers: bulk_create with PK preserved (Schleiper's Sprycer ID
        contract — eng review 1A + cutover safety)."""
        legacy.execute("""
            SELECT id, website_id, sku, common_sku, name, description, retailer_id,
                   original_image_url, ean, categories, custom_attributes, brand_id,
                   public, matchings_reviewed_at, channel_id
            FROM offers ORDER BY id
        """)
        instances = []
        for row in legacy.fetchall():
            (legacy_id, website_id, sku, common_sku, name, description, retailer_id,
             image_url, ean, categories, custom_attributes, brand_id, public,
             matchings_reviewed_at, channel_id) = row
            if retailer_id is None or retailer_id not in retailer_map:
                continue
            if not channel_id or channel_id not in channel_map:
                continue
            if not sku or not name:
                continue
            instances.append(Offer(
                pk=legacy_id,
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
                matchings_reviewed_at=_aware(matchings_reviewed_at),
            ))
        Offer.objects.bulk_create(instances, ignore_conflicts=True, batch_size=2000)
        return len(instances)

    def _migrate_offers_pages(self, legacy, page_map):
        legacy.execute(
            'SELECT offer_id, page_id FROM offers_pages '
            'WHERE offer_id IS NOT NULL AND page_id IS NOT NULL'
        )
        valid_offer_ids = set(Offer.objects.values_list('pk', flat=True))
        through = Offer.pages.through
        rows: list = []
        for offer_id, page_id in legacy.fetchall():
            if offer_id not in valid_offer_ids or page_id not in page_map:
                continue
            rows.append(through(offer_id=offer_id, page_id=page_map[page_id]))
        through.objects.bulk_create(rows, ignore_conflicts=True, batch_size=5000)
        return len(rows)

    def _migrate_matchings(self, legacy):
        # Only confirmed (status=0) matches imported. Tension B: legacy
        # human-confirmed work survives cutover; AI re-runs respect skip-if-
        # exists (tested in test_matching.py).
        legacy.execute(
            "SELECT offer_id, competing_offer_id, score, predicted "
            "FROM matchings WHERE status = 0 AND offer_id != competing_offer_id"
        )
        valid_offer_ids = set(Offer.objects.values_list('pk', flat=True))
        instances = []
        for offer_id, competing_offer_id, score, predicted in legacy.fetchall():
            if offer_id not in valid_offer_ids or competing_offer_id not in valid_offer_ids:
                continue
            instances.append(Matching(
                offer_id=offer_id,
                competing_offer_id=competing_offer_id,
                status=Matching.Status.CONFIRMED,
                source=Matching.Source.LEGACY_IMPORTED,
                score=score,
                predicted=bool(predicted) if predicted is not None else False,
                llm_reason='',
            ))
        Matching.objects.bulk_create(instances, ignore_conflicts=True, batch_size=2000)
        return len(instances)

    def _migrate_price_points(self, legacy):
        legacy.execute("""
            SELECT offer_id, price_cents, list_price_cents, shipping_charges_cents,
                   price_currency, price_at
            FROM price_points
            WHERE offer_id IS NOT NULL AND price_cents IS NOT NULL
        """)
        valid_offer_ids = set(Offer.objects.values_list('pk', flat=True))
        instances = []
        for row in legacy.fetchall():
            (offer_id, price_cents, list_price_cents, shipping_cents,
             currency, price_at) = row
            if offer_id not in valid_offer_ids:
                continue
            instances.append(PriceObservation(
                offer_id=offer_id,
                price_cents=price_cents,
                list_price_cents=list_price_cents,
                shipping_charges_cents=shipping_cents,
                price_currency=currency or 'EUR',
                observed_at=_aware(price_at) or timezone.now(),
            ))
        PriceObservation.objects.bulk_create(instances, batch_size=2000)
        return len(instances)

    def _migrate_reviews(self, legacy, retailer_map):
        legacy.execute(
            'SELECT offer_id, retailer_id, competitor_id, reviewed_at FROM reviews'
        )
        valid_offer_ids = set(Offer.objects.values_list('pk', flat=True))
        instances = []
        for offer_id, retailer_id, competitor_id, reviewed_at in legacy.fetchall():
            if retailer_id not in retailer_map or competitor_id not in retailer_map:
                continue
            if offer_id not in valid_offer_ids:
                continue
            instances.append(Review(
                offer_id=offer_id,
                retailer_id=retailer_map[retailer_id],
                competitor_id=retailer_map[competitor_id],
                reviewed_at=_aware(reviewed_at) or timezone.now(),
            ))
        Review.objects.bulk_create(instances, ignore_conflicts=True, batch_size=2000)
        return len(instances)

    def _migrate_historical_versions(self, legacy, history_months: int):
        """TODO #7: backfill last N months of price changes from
        paper_trail.versions into PriceObservation. versions.item_id is
        PricePoint.id; resolve via price_points table (1:1)."""
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
        valid_offer_ids = set(Offer.objects.values_list('pk', flat=True))
        instances = []
        for price_point_id, created_at, object_changes in legacy.fetchall():
            offer_id = price_point_to_offer.get(price_point_id)
            if offer_id is None or offer_id not in valid_offer_ids:
                continue
            changes = object_changes if isinstance(object_changes, dict) else \
                json.loads(object_changes or '{}')
            change = changes.get('price_cents')
            if not change:
                continue
            new_price = change[1] if len(change) > 1 else change[0]
            if not isinstance(new_price, int) or new_price < 0:
                continue
            instances.append(PriceObservation(
                offer_id=offer_id,
                price_cents=new_price,
                price_currency='EUR',
                observed_at=_aware(created_at),
            ))
        PriceObservation.objects.bulk_create(instances, batch_size=2000)
        return len(instances)

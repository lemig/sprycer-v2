"""Sanity-check pgvector + HNSW after migrate.

Usage: uv run python manage.py verify_pgvector

Checks:
  1. pgvector extension is installed and version >= 0.5 (HNSW requires it)
  2. core_offer.offer_embedding_hnsw_idx exists
  3. Round-trip: insert + cosine-nearest-neighbor query + cleanup

Exits non-zero on any failure so it's safe to run in CI / deploy gates.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = 'Verify pgvector extension + HNSW index are working.'

    def handle(self, *args, **opts):
        with connection.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector';")
            row = cur.fetchone()
            if not row:
                raise CommandError(
                    'pgvector extension is NOT installed. Did `migrate` run?'
                )
            version = row[0]
            self.stdout.write(self.style.SUCCESS(f'pgvector extension installed: v{version}'))

            try:
                major, minor, *_ = version.split('.')
                if int(major) == 0 and int(minor) < 5:
                    raise CommandError(
                        f'pgvector v{version} is too old for HNSW. Need >= 0.5.'
                    )
            except (ValueError, IndexError):
                self.stdout.write(self.style.WARNING(
                    f'Could not parse pgvector version "{version}"; skipping version check.'
                ))

            cur.execute("""
                SELECT indexname, indexdef FROM pg_indexes
                WHERE tablename = 'core_offer' AND indexname = 'offer_embedding_hnsw_idx';
            """)
            idx = cur.fetchone()
            if not idx:
                raise CommandError(
                    'offer_embedding_hnsw_idx is missing on core_offer. '
                    'Re-run `migrate` or check the migration ran cleanly.'
                )
            self.stdout.write(self.style.SUCCESS(
                f'HNSW index present: {idx[0]}'
            ))
            self.stdout.write(f'  definition: {idx[1]}')

            self.stdout.write('Running roundtrip smoke test...')
            cur.execute("CREATE TEMP TABLE _vec_smoke (id int, v vector(3));")
            cur.execute("INSERT INTO _vec_smoke VALUES (1, '[1,0,0]'), (2, '[0,1,0]'), (3, '[0,0,1]');")
            cur.execute("SELECT id FROM _vec_smoke ORDER BY v <=> '[1,0,0]' LIMIT 1;")
            (nearest_id,) = cur.fetchone()
            if nearest_id != 1:
                raise CommandError(
                    f'Cosine-nearest-neighbor returned id={nearest_id}, expected 1. '
                    'pgvector ops are not behaving correctly.'
                )
            self.stdout.write(self.style.SUCCESS(
                f'Cosine-nearest-neighbor returned id={nearest_id} (expected 1)'
            ))

        self.stdout.write(self.style.SUCCESS('All checks passed.'))

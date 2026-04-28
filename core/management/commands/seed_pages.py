"""Seed Page rows from a list of URLs (one per line).

    uv run python manage.py seed_pages --file ../sprycer/urls.csv
    uv run python manage.py seed_pages --file urls.txt --dry-run

Idempotent: re-running with the same file produces no new rows.

Bootstraps Website rows on the fly. Unknown hosts (no matching ScraperSpec
in core.scrapers.REGISTRY) are skipped with a warning so a stray Gerstaeker
URL or a typo doesn't poison the queue.

H17 migration loads the full Page table from the prod DB dump; this command
exists for fresh-dev-DB seeding and for one-off ops "add a few URLs" runs.
"""
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError

from core.models import Page, Website
from core.scrapers import REGISTRY, get_spec


class Command(BaseCommand):
    help = 'Seed Page rows from a newline-delimited URL file.'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True,
                            help='Path to a file with one URL per line.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be created without writing.')

    def handle(self, *args, **opts):
        try:
            with open(opts['file'], encoding='utf-8') as fh:
                urls = [line.strip() for line in fh if line.strip()]
        except OSError as exc:
            raise CommandError(f'Could not read URL file: {exc}')

        counters = self._seed(urls, dry_run=opts['dry_run'])
        prefix = 'DRY-RUN: ' if opts['dry_run'] else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}created={counters["created"]} '
            f'existing={counters["existing"]} '
            f'skipped_unknown_host={counters["skipped_unknown_host"]}'
        ))
        if counters['skipped_hosts']:
            self.stdout.write(self.style.WARNING(
                f'Hosts with no registered scraper (skipped): '
                f'{sorted(counters["skipped_hosts"])}'
            ))

    def _seed(self, urls, *, dry_run: bool):
        counters = {
            'created': 0, 'existing': 0,
            'skipped_unknown_host': 0,
            'skipped_hosts': set(),
        }
        # Cache Website lookups so we don't hit the DB once per URL.
        website_cache: dict[str, Website] = {}

        for url in urls:
            host = urlparse(url).hostname or ''
            spec = get_spec(host)
            if spec is None:
                counters['skipped_unknown_host'] += 1
                counters['skipped_hosts'].add(host or '<no-host>')
                continue

            if dry_run:
                # Don't bootstrap, don't insert, just count what would happen.
                if Page.objects.filter(url=url).exists():
                    counters['existing'] += 1
                else:
                    counters['created'] += 1
                continue

            website = website_cache.get(spec.website_host)
            if website is None:
                website, _ = Website.objects.get_or_create(
                    host=spec.website_host, defaults={'scrapable': True}
                )
                website_cache[spec.website_host] = website

            _, created = Page.objects.get_or_create(
                url=url, defaults={'website': website}
            )
            counters['created' if created else 'existing'] += 1

        return counters

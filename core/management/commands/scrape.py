"""Run the scraper from the CLI.

    uv run python manage.py scrape --url https://www.geant-beaux-arts.be/...html
    uv run python manage.py scrape --queue                       # walk Page table
    uv run python manage.py scrape --queue --limit 50 --delay 2  # gentler

H10 will wrap `--queue` in a Fly scheduled machine running twice/day, with the
counter dict piped to a Slack webhook on `no_offers > 0` or `failures > 0`.
"""
from django.core.management.base import BaseCommand, CommandError

from core.scrapers.runner import (
    DEFAULT_DELAY_SECONDS,
    NoOffersFound,
    UnsupportedHost,
    scrape_queue,
    scrape_url,
)


class Command(BaseCommand):
    help = 'Scrape one URL or drain the Page queue.'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--url', help='Scrape a single URL.')
        group.add_argument('--queue', action='store_true',
                           help='Walk the Page table for stale pages.')
        parser.add_argument('--limit', type=int, default=100,
                            help='Max pages per queue run (default: 100).')
        parser.add_argument('--delay', type=float, default=DEFAULT_DELAY_SECONDS,
                            help=f'Seconds between requests (default: {DEFAULT_DELAY_SECONDS}).')
        parser.add_argument('--max-age-hours', type=int, default=12,
                            help='Re-scrape pages older than N hours (default: 12).')

    def handle(self, *args, **opts):
        if opts['url']:
            try:
                written = scrape_url(opts['url'])
            except UnsupportedHost as exc:
                raise CommandError(str(exc))
            except NoOffersFound as exc:
                self.stderr.write(self.style.WARNING(str(exc)))
                return
            self.stdout.write(self.style.SUCCESS(
                f'Scraped {opts["url"]}: {written} offer(s) written.'
            ))
            return

        counters = scrape_queue(
            limit=opts['limit'],
            delay=opts['delay'],
            max_age_hours=opts['max_age_hours'],
        )
        self.stdout.write(self.style.SUCCESS(
            f'Queue run: scraped={counters["pages_scraped"]} '
            f'offers={counters["offers_written"]} '
            f'no_offers={counters["no_offers"]} '
            f'failures={counters["failures"]}'
        ))

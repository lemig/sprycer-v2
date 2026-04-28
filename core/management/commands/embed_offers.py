"""Backfill OpenAI embeddings for offers whose hash is stale or missing.

    uv run python manage.py embed_offers
    uv run python manage.py embed_offers --retailer Schleiper
    uv run python manage.py embed_offers --only-missing       # NULL embeddings only
    uv run python manage.py embed_offers --chunk-size 1000

Wired into the H17/H18 cron so transient failures self-heal: any offer that
failed mid-import or mid-scrape will be picked up on the next backfill run.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from core.embeddings import embed_offers_bulk, is_enabled
from core.models import Offer, Retailer


class Command(BaseCommand):
    help = 'Compute / refresh embeddings for offers.'

    def add_arguments(self, parser):
        parser.add_argument('--retailer', default=None,
                            help='Limit to one retailer (by name).')
        parser.add_argument('--only-missing', action='store_true',
                            help='Only embed offers with NULL embedding.')
        parser.add_argument('--chunk-size', type=int, default=500,
                            help='Inputs per OpenAI batch call (default: 500).')

    def handle(self, *args, **opts):
        if not is_enabled():
            raise CommandError(
                'OPENAI_API_KEY is not set. Set it in .env (or Fly secrets).'
            )

        qs = Offer.objects.all()
        if opts['retailer']:
            try:
                retailer = Retailer.objects.get(name=opts['retailer'])
            except Retailer.DoesNotExist:
                raise CommandError(f"Retailer not found: {opts['retailer']!r}")
            qs = qs.filter(retailer=retailer)
        if opts['only_missing']:
            qs = qs.filter(Q(embedding__isnull=True) | Q(embedding_input_hash=''))

        qs = qs.order_by('id')
        total = qs.count()
        self.stdout.write(f'Considering {total} offer(s)...')

        counters = embed_offers_bulk(qs.iterator(chunk_size=opts['chunk_size']),
                                     chunk_size=opts['chunk_size'])
        self.stdout.write(self.style.SUCCESS(
            f'embedded={counters["embedded"]} '
            f'skipped_unchanged={counters["skipped_unchanged"]} '
            f'failed={counters["failed"]}'
        ))

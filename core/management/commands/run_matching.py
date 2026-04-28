"""Run the AI matching pipeline.

    uv run python manage.py run_matching --retailer Schleiper
    uv run python manage.py run_matching --offer-id 124397
    uv run python manage.py run_matching --retailer Schleiper --k 10

CRITICAL invariant: skips offer pairs that already have a Matching row,
regardless of status. Re-running this command must NEVER overwrite human
confirmations (Tension B + TODO #4).
"""
from django.core.management.base import BaseCommand, CommandError

from core.matching import run_matching_for_offer, run_matching_for_queryset
from core.models import Offer, Retailer


class Command(BaseCommand):
    help = 'Run the AI matching pipeline.'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--retailer', help='Run for all offers in this retailer.')
        group.add_argument('--offer-id', type=int, help='Run for one offer.')
        parser.add_argument('--k', type=int, default=5,
                            help='Top-K candidates per offer (default: 5).')

    def handle(self, *args, **opts):
        if opts['offer_id']:
            try:
                offer = Offer.objects.get(pk=opts['offer_id'])
            except Offer.DoesNotExist:
                raise CommandError(f"Offer #{opts['offer_id']} not found.")
            counters = run_matching_for_offer(offer, k=opts['k'])
            self.stdout.write(self.style.SUCCESS(
                f'offer={offer.pk} suggested={counters["suggested"]} '
                f'rejected={counters["rejected"]} '
                f'errored={counters["errored"]} '
                f'skipped_existing={counters["skipped_existing"]}'
            ))
            return

        try:
            retailer = Retailer.objects.get(name=opts['retailer'])
        except Retailer.DoesNotExist:
            raise CommandError(f"Retailer not found: {opts['retailer']!r}")

        # public is intentionally NOT filtered: migrated Schleiper rows preserve
        # legacy `public=False`. Filtering on public=True would make this command
        # process zero offers on cutover day. Match candidacy is gated on
        # embedding presence + retailer membership only.
        offers = Offer.objects.filter(
            retailer=retailer, embedding__isnull=False
        ).order_by('id')
        totals = run_matching_for_queryset(offers, k=opts['k'])
        self.stdout.write(self.style.SUCCESS(
            f'retailer={retailer.name} processed={totals["offers_processed"]} '
            f'suggested={totals["suggested"]} '
            f'rejected={totals["rejected"]} '
            f'errored={totals["errored"]} '
            f'skipped_existing={totals["skipped_existing"]}'
        ))

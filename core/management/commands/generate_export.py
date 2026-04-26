"""Generate an offer export from the CLI.

    uv run python manage.py generate_export --retailer Schleiper --format csv
    uv run python manage.py generate_export --retailer Schleiper --format xlsx \\
                                            --user miguel

Used for ops + automated exports + dogfooding before the H16 admin action lands.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.exporters import generate_offer_export
from core.models import Export, Retailer


class Command(BaseCommand):
    help = 'Generate an offer export for a retailer (CSV or XLSX).'

    def add_arguments(self, parser):
        parser.add_argument('--retailer', required=True,
                            help='Retailer.name (e.g. "Schleiper").')
        parser.add_argument('--format', choices=('csv', 'xlsx'), default='csv',
                            help='Output format (default: csv).')
        parser.add_argument('--user', default=None,
                            help='Username to attribute the export to '
                                 '(default: first superuser).')

    def handle(self, *args, **opts):
        try:
            retailer = Retailer.objects.get(name=opts['retailer'])
        except Retailer.DoesNotExist:
            raise CommandError(f"Retailer not found: {opts['retailer']!r}")

        User = get_user_model()
        if opts['user']:
            try:
                user = User.objects.get(username=opts['user'])
            except User.DoesNotExist:
                raise CommandError(f"User not found: {opts['user']!r}")
        else:
            user = User.objects.filter(is_superuser=True).order_by('id').first()
            if user is None:
                raise CommandError('No superuser exists. Pass --user explicitly.')

        export = Export.objects.create(user=user, model=Export.Model.OFFER, count=0)
        generate_offer_export(export, retailer, fmt=opts['format'])
        self.stdout.write(self.style.SUCCESS(
            f'Export #{export.pk} ({export.count} rows) -> {export.file.name}'
        ))

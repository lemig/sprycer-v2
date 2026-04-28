"""Drain pending Import rows.

Synchronous for now. The H10/H18 wiring runs this on a Fly scheduled machine
that polls every 60s; for local dev / tests run it manually:

    uv run python manage.py process_imports
    uv run python manage.py process_imports --once
    uv run python manage.py process_imports --import-id 42

The command picks up Imports whose status is one of {Unprocessed, Enqueued,
Importing} (Importing is included so a crashed/restarted run resumes). Each
Import is dispatched to its registered importer class.
"""
import time

from django.core.management.base import BaseCommand

from core.importers import get_importer
from core.models import Import


PROCESSABLE_STATUSES = (
    Import.Status.UNPROCESSED,
    Import.Status.ENQUEUED,
    Import.Status.IMPORTING,
)


class Command(BaseCommand):
    help = 'Process pending Imports synchronously.'

    def add_arguments(self, parser):
        parser.add_argument('--import-id', type=int, default=None,
                            help='Process a single Import by id (overrides --once).')
        parser.add_argument('--once', action='store_true',
                            help='Drain queue once then exit (default behavior).')
        parser.add_argument('--watch', action='store_true',
                            help='Loop forever, polling every --interval seconds.')
        parser.add_argument('--interval', type=int, default=60,
                            help='Poll interval in seconds when --watch is set.')

    def handle(self, *args, **opts):
        if opts['import_id']:
            self._process_one(opts['import_id'])
            return

        if not opts['watch']:
            self._drain()
            return

        interval = opts['interval']
        self.stdout.write(f'Watch mode: polling every {interval}s')
        while True:
            self._drain()
            time.sleep(interval)

    def _drain(self):
        qs = Import.objects.filter(status__in=PROCESSABLE_STATUSES).order_by('created_at')
        count = qs.count()
        if count == 0:
            self.stdout.write('No pending imports.')
            return
        self.stdout.write(f'Processing {count} import(s)...')
        for imp in qs:
            self._process_one(imp.pk)

    def _process_one(self, import_id: int):
        try:
            imp = Import.objects.get(pk=import_id)
        except Import.DoesNotExist:
            self.stderr.write(self.style.ERROR(f'Import #{import_id} not found.'))
            return

        imp.status = Import.Status.IMPORTING
        imp.save(update_fields=['status', 'updated_at'])

        importer_cls = get_importer(imp.importer_class_name)
        importer = importer_cls()

        try:
            result = importer.run(imp)
        except Exception as exc:
            imp.status = Import.Status.ERROR
            imp.failures = (imp.failures or 0) + 1
            imp.failure_info = list(imp.failure_info or []) + [f'fatal: {type(exc).__name__}: {exc}']
            imp.save(update_fields=['status', 'failures', 'failure_info', 'updated_at'])
            self.stderr.write(self.style.ERROR(
                f'Import #{imp.pk} FAILED: {type(exc).__name__}: {exc}'
            ))
            return

        self.stdout.write(self.style.SUCCESS(
            f'Import #{imp.pk} {imp.status}: '
            f'total={result.total} failures={result.failures}'
        ))

"""Tests for the seed_pages management command (H8)."""
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from core.models import Page, Website


@pytest.fixture
def url_file(tmp_path):
    f = tmp_path / 'urls.txt'
    f.write_text(
        'https://www.geant-beaux-arts.fr/page-1.html\n'
        'https://www.geant-beaux-arts.fr/page-2.html\n'
        'https://www.geant-beaux-arts.be/page-3.html\n'
        'https://www.rougier-ple.fr/aquarelle.r.html\n'
        'https://www.gerstaecker.de/historical.html\n'  # unsupported -> skipped
        '\n'  # blank line tolerated
        'https://example.com/something.html\n'  # unknown -> skipped
    )
    return str(f)


@pytest.mark.django_db
class TestSeedPages:
    def test_creates_pages_for_known_hosts_only(self, url_file):
        out = StringIO()
        call_command('seed_pages', '--file', url_file, stdout=out)
        urls = set(Page.objects.values_list('url', flat=True))
        assert urls == {
            'https://www.geant-beaux-arts.fr/page-1.html',
            'https://www.geant-beaux-arts.fr/page-2.html',
            'https://www.geant-beaux-arts.be/page-3.html',
            'https://www.rougier-ple.fr/aquarelle.r.html',
        }

    def test_bootstraps_websites_for_seeded_pages(self, url_file):
        call_command('seed_pages', '--file', url_file, stdout=StringIO())
        hosts = set(Website.objects.values_list('host', flat=True))
        assert hosts == {
            'www.geant-beaux-arts.fr',
            'www.geant-beaux-arts.be',
            'www.rougier-ple.fr',
        }

    def test_unknown_host_warning(self, url_file):
        out = StringIO()
        call_command('seed_pages', '--file', url_file, stdout=out)
        text = out.getvalue()
        assert 'skipped_unknown_host=2' in text  # gerstaecker.de + example.com
        assert 'www.gerstaecker.de' in text
        assert 'example.com' in text

    def test_idempotent_re_run(self, url_file):
        call_command('seed_pages', '--file', url_file, stdout=StringIO())
        before = Page.objects.count()
        out = StringIO()
        call_command('seed_pages', '--file', url_file, stdout=out)
        assert Page.objects.count() == before
        assert 'created=0' in out.getvalue()
        assert 'existing=4' in out.getvalue()

    def test_dry_run_does_not_write(self, url_file):
        out = StringIO()
        call_command('seed_pages', '--file', url_file, '--dry-run', stdout=out)
        assert Page.objects.count() == 0
        assert 'DRY-RUN' in out.getvalue()
        assert 'created=4' in out.getvalue()

    def test_blank_lines_skipped(self, url_file):
        call_command('seed_pages', '--file', url_file, stdout=StringIO())
        # 4 valid URLs (the blank line and unsupported lines are skipped)
        assert Page.objects.count() == 4

    def test_seeded_pages_have_no_scraped_at(self, url_file):
        call_command('seed_pages', '--file', url_file, stdout=StringIO())
        # Fresh seeds should be queue-eligible (scraped_at IS NULL)
        assert Page.objects.filter(scraped_at__isnull=True).count() == 4

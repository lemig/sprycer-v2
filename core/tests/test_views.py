"""View-layer tests for /imports, /exports, /matchings (H15 + H16).

Verifies route paths preserved (Tension A: Schleiper bookmarks survive),
auth gating, HTMX confirm/reject endpoints, and the view-layer side of the
human-corrections invariant (Tension B / TODO #4).
"""
import pytest
from django.contrib.auth import get_user_model

from core.models import Channel, Matching, Offer, PriceObservation, Retailer, Website


@pytest.fixture
def user(db):
    User = get_user_model()
    u = User.objects.create_user(username='miguel', password='x', is_staff=True)
    return u


@pytest.fixture
def client_in(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def world(db):
    sch = Retailer.objects.create(name='Schleiper')
    rp = Retailer.objects.create(name='Rougier & Plé')
    sch_w = Website.objects.create(host='www.schleiper.com')
    rp_w = Website.objects.create(host='www.rougier-ple.fr')
    sch_c = Channel.objects.create(name='schleiper.com/eshopexpress', retailer=sch, website=sch_w)
    rp_c = Channel.objects.create(name='rougier-ple.fr', retailer=rp, website=rp_w)
    sch_offer = Offer.objects.create(retailer=sch, channel=sch_c, website=sch_w,
                                     sku='S1', name='Schleiper item', public=True)
    rp_offer = Offer.objects.create(retailer=rp, channel=rp_c, website=rp_w,
                                    sku='RP1', name='R&P item', public=True)
    PriceObservation.objects.create(offer=sch_offer, price_cents=298)
    PriceObservation.objects.create(offer=rp_offer, price_cents=300)
    return {'sch': sch, 'rp': rp, 'sch_offer': sch_offer, 'rp_offer': rp_offer}


# ---- Auth gating (Tension A: route parity) ------------------------------


@pytest.mark.django_db
class TestAuthRedirects:
    @pytest.mark.parametrize('url', [
        '/', '/imports/', '/imports/new', '/exports/', '/exports/new',
        '/matchings/',
    ])
    def test_anonymous_redirected_to_login(self, client, url):
        r = client.get(url)
        assert r.status_code == 302
        assert '/accounts/login/' in r['Location']


# ---- /imports + /exports route + columns --------------------------------


@pytest.mark.django_db
class TestImportsViews:
    def test_imports_list_renders_with_legacy_headers(self, client_in, user):
        # Seed an Import row so the table (with legacy column headers) renders.
        from django.core.files.uploadedfile import SimpleUploadedFile
        from core.models import Import
        Import.objects.create(
            user=user, file=SimpleUploadedFile('x.csv', b'a,b\n1,2\n'),
            importer_class_name='SchleiperImporter',
        )
        r = client_in.get('/imports/')
        assert r.status_code == 200
        body = r.content.decode()
        for col in ('ID', 'User', 'Model', 'Created at', 'Status', 'Total', 'Pending', 'Failures'):
            assert col in body

    def test_imports_list_empty_state(self, client_in):
        r = client_in.get('/imports/')
        assert r.status_code == 200
        assert b'No imports yet' in r.content

    def test_imports_new_renders_form(self, client_in):
        r = client_in.get('/imports/new')
        assert r.status_code == 200
        assert b'<input type="file"' in r.content
        assert b'SchleiperImporter' in r.content


@pytest.mark.django_db
class TestExportsViews:
    def test_exports_list_renders_with_legacy_headers(self, client_in, user):
        from core.models import Export
        Export.objects.create(user=user, model=Export.Model.OFFER, count=1)
        r = client_in.get('/exports/')
        assert r.status_code == 200
        body = r.content.decode()
        for col in ('ID', 'User', 'Model', 'Created at', 'Records', 'File'):
            assert col in body

    def test_exports_list_empty_state(self, client_in):
        r = client_in.get('/exports/')
        assert b'No exports yet' in r.content

    def test_exports_new_renders_form_with_retailers(self, client_in, world):
        r = client_in.get('/exports/new')
        assert r.status_code == 200
        assert b'Schleiper' in r.content


# ---- /matchings list + HTMX confirm / reject (Tension A + B) -----------


@pytest.fixture
def suggested_matching(db, world):
    return Matching.objects.create(
        offer=world['sch_offer'], competing_offer=world['rp_offer'],
        status=Matching.Status.SUGGESTED,
        source=Matching.Source.AI_SUGGESTED,
        score=0.87, llm_reason='same brand and size',
    )


@pytest.mark.django_db
class TestMatchingsList:
    def test_renders_suggested_matching(self, client_in, suggested_matching):
        r = client_in.get('/matchings/')
        assert r.status_code == 200
        body = r.content.decode()
        assert 'Schleiper item' in body
        # Django escapes & to &amp; in HTML
        assert 'R&amp;P item' in body or 'R&P item' in body
        assert 'same brand and size' in body  # llm_reason visible
        assert 'Confirm' in body and 'Reject' in body

    def test_search_by_sku(self, client_in, suggested_matching):
        r = client_in.get('/matchings/?q=S1')
        assert r.status_code == 200
        assert b'Schleiper item' in r.content

    def test_search_misses_returns_no_match(self, client_in, suggested_matching):
        r = client_in.get('/matchings/?q=NOPE')
        body = r.content.decode()
        assert 'Schleiper item' not in body or 'No suggested matchings' in body

    def test_only_suggested_status_listed(self, client_in, suggested_matching):
        # Add a CONFIRMED matching; it should not appear in the suggested list
        Matching.objects.create(
            offer=suggested_matching.offer,
            competing_offer=Offer.objects.create(
                retailer=suggested_matching.competing_offer.retailer,
                channel=suggested_matching.competing_offer.channel,
                website=suggested_matching.competing_offer.website,
                sku='ALREADY-CONFIRMED', name='Already confirmed item', public=True,
            ),
            status=Matching.Status.CONFIRMED,
            source=Matching.Source.HUMAN_CONFIRMED,
        )
        r = client_in.get('/matchings/')
        assert b'Already confirmed item' not in r.content


@pytest.mark.django_db
class TestMatchingsHtmx:
    def test_confirm_swaps_to_resolved_card(self, client_in, suggested_matching):
        r = client_in.post(f'/matchings/{suggested_matching.pk}/confirm')
        assert r.status_code == 200
        assert b'Confirmed' in r.content
        suggested_matching.refresh_from_db()
        assert suggested_matching.status == Matching.Status.CONFIRMED
        assert suggested_matching.source == Matching.Source.HUMAN_CONFIRMED

    def test_reject_swaps_to_resolved_card(self, client_in, suggested_matching):
        r = client_in.post(f'/matchings/{suggested_matching.pk}/reject')
        assert r.status_code == 200
        assert b'Rejected' in r.content
        suggested_matching.refresh_from_db()
        assert suggested_matching.status == Matching.Status.REJECTED
        assert suggested_matching.source == Matching.Source.HUMAN_REJECTED

    def test_get_method_not_allowed(self, client_in, suggested_matching):
        r = client_in.get(f'/matchings/{suggested_matching.pk}/confirm')
        assert r.status_code == 405

    def test_unknown_matching_returns_404(self, client_in):
        r = client_in.post('/matchings/99999/confirm')
        assert r.status_code == 404


@pytest.mark.django_db
class TestExportEndToEnd:
    def test_post_creates_export_with_file(self, client_in, world):
        # The default export filter only includes offers with at least one
        # confirmed Matching. Add one so the test offer survives the filter.
        from core.models import Matching
        Matching.objects.create(
            offer=world['sch_offer'], competing_offer=world['rp_offer'],
            status=Matching.Status.CONFIRMED,
            source=Matching.Source.LEGACY_IMPORTED,
        )
        r = client_in.post('/exports/new', {
            'retailer_id': world['sch'].pk, 'format': 'csv',
        })
        assert r.status_code == 302
        # The created Export has a file attached
        from core.models import Export
        ex = Export.objects.latest('created_at')
        assert ex.file
        assert ex.count >= 1


@pytest.mark.django_db
class TestHealthz:
    """Fly health probe. Unauthenticated, DB-pinging, plain-text response."""

    def test_healthy_when_db_reachable(self, client):
        r = client.get('/healthz')
        assert r.status_code == 200
        assert r.content == b'ok'

    def test_no_login_required(self, client):
        # Probe runs without credentials. If we accidentally login_required'd
        # this, Fly would always see 302 and never know the app was healthy.
        r = client.get('/healthz')
        assert r.status_code != 302

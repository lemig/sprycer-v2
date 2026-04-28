"""User-facing views for /imports, /exports, /matchings.

Mirrors legacy Rails routes + columns visible in the production
screenshots (eng review Tension A: UI parity is part of byte-identical
cutover, not just I/O).

Match review uses HTMX for confirm/reject — the buttons swap the card in
place so Schleiper-side review feels fast.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import connection, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .exporters import generate_offer_export
from .models import Export, Import, Matching, Retailer, Review


# ---- /healthz (Fly probe) ---------------------------------------------


def healthz(request):
    """Liveness + readiness probe for Fly's health check.

    Pings the DB so an unreachable Neon flips the machine to unhealthy and
    Fly holds traffic until ready. Unauthenticated by design — Fly's probe
    has no credentials, and the response carries no sensitive data.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception as exc:
        return HttpResponse(f'unhealthy: {exc.__class__.__name__}', status=503)
    return HttpResponse('ok')


# ---- /imports ----------------------------------------------------------


@login_required
def imports_list(request):
    imports = Import.objects.select_related('user').order_by('-created_at')[:200]
    return render(request, 'imports/list.html', {'imports': imports})


@login_required
@require_http_methods(['GET', 'POST'])
def imports_new(request):
    if request.method == 'POST':
        importer = request.POST.get('importer_class_name', 'SchleiperImporter')
        upload = request.FILES.get('file')
        if upload is None:
            messages.error(request, 'Please choose a file to upload.')
            return render(request, 'imports/new.html')
        imp = Import.objects.create(
            user=request.user,
            importer_class_name=importer,
            file=upload,
            status=Import.Status.UNPROCESSED,
        )
        messages.info(request, f'Import #{imp.pk} queued. Run `manage.py process_imports` (or wait for the scheduled machine).')
        return redirect('imports:show', import_id=imp.pk)
    return render(request, 'imports/new.html')


@login_required
def imports_show(request, import_id: int):
    imp = get_object_or_404(Import, pk=import_id)
    return render(request, 'imports/show.html', {'import': imp})


# ---- /exports ----------------------------------------------------------


@login_required
def exports_list(request):
    exports = Export.objects.select_related('user').order_by('-created_at')[:200]
    return render(request, 'exports/list.html', {'exports': exports})


@login_required
@require_http_methods(['GET', 'POST'])
def exports_new(request):
    retailers = Retailer.objects.order_by('name')
    if request.method == 'POST':
        try:
            retailer = Retailer.objects.get(pk=request.POST.get('retailer_id'))
        except (Retailer.DoesNotExist, ValueError, TypeError):
            messages.error(request, 'Pick a retailer.')
            return render(request, 'exports/new.html', {'retailers': retailers})
        fmt = request.POST.get('format', 'csv')
        if fmt not in ('csv', 'xlsx'):
            messages.error(request, 'Invalid format.')
            return render(request, 'exports/new.html', {'retailers': retailers})
        export = Export.objects.create(
            user=request.user, model=Export.Model.OFFER, count=0,
        )
        generate_offer_export(export, retailer, fmt=fmt)
        messages.info(request, f'Export #{export.pk} generated ({export.count} rows).')
        return redirect('exports:list')
    return render(request, 'exports/new.html', {'retailers': retailers})


# ---- /matchings (HTMX confirm/reject) ----------------------------------


def _matching_queryset(q: str = '', order: str = 'score-desc'):
    qs = (
        Matching.objects
        .filter(status=Matching.Status.SUGGESTED)
        .select_related('offer__retailer', 'competing_offer__retailer')
        .prefetch_related('offer__price_observations', 'competing_offer__price_observations')
    )
    if q:
        qs = qs.filter(
            Q(offer__sku__icontains=q) | Q(competing_offer__sku__icontains=q)
            | Q(offer__pages__url__icontains=q) | Q(competing_offer__pages__url__icontains=q)
        ).distinct()
    if order == 'score-asc':
        qs = qs.order_by('score', '-id')
    elif order == 'name-asc':
        qs = qs.order_by('offer__name')
    elif order == 'name-desc':
        qs = qs.order_by('-offer__name')
    else:  # score-desc default (most-confident first)
        qs = qs.order_by('-score', '-id')
    return qs


@login_required
def matchings_list(request):
    q = request.GET.get('q', '').strip()
    order = request.GET.get('order', 'score-desc')
    qs = _matching_queryset(q, order)
    total = qs.count()
    matchings = qs[:50]  # paginate later if needed; legacy used 25/page
    return render(request, 'matchings/list.html', {
        'matchings': matchings, 'q': q, 'order': order, 'total': total,
    })


@login_required
@require_POST
def matchings_confirm(request, matching_id: int):
    """Human confirms an AI-suggested matching.

    State machine: only SUGGESTED rows can transition. CONFIRMED/REJECTED rows
    return 409 — protects legacy-imported and previously-reviewed matchings
    from being overwritten by a direct POST.

    Side effects (atomic with the status transition):
      - Upsert a Review(offer, retailer, competitor) row stamped with now —
        this is what flips the export's "Reviewed" column. Without it,
        confirming a matching would change the Competitor N cells but leave
        "Competitors offers not yet reviewed" stuck on the row.
      - Stamp `offer.matchings_reviewed_at` for legacy parity.
    """
    m = get_object_or_404(Matching, pk=matching_id)
    if m.status != Matching.Status.SUGGESTED:
        return HttpResponse(
            f'Matching #{m.pk} is already {m.status} — cannot transition.',
            status=409,
        )
    with transaction.atomic():
        m.status = Matching.Status.CONFIRMED
        m.source = Matching.Source.HUMAN_CONFIRMED
        m.save(update_fields=['status', 'source', 'updated_at'])
        Review.objects.update_or_create(
            offer=m.offer,
            retailer_id=m.offer.retailer_id,
            competitor_id=m.competing_offer.retailer_id,
            defaults={'reviewed_at': timezone.now()},
        )
        m.offer.matchings_reviewed_at = timezone.now()
        m.offer.save(update_fields=['matchings_reviewed_at', 'updated_at'])
    return render(request, 'matchings/_card_resolved.html', {'m': m})


@login_required
@require_POST
def matchings_reject(request, matching_id: int):
    """Human rejects an AI-suggested matching. Same state-machine guard as
    confirm. No Review row written — rejection means "not the same product",
    which is not equivalent to "I reviewed this competitor's pricing."
    """
    m = get_object_or_404(Matching, pk=matching_id)
    if m.status != Matching.Status.SUGGESTED:
        return HttpResponse(
            f'Matching #{m.pk} is already {m.status} — cannot transition.',
            status=409,
        )
    m.status = Matching.Status.REJECTED
    m.source = Matching.Source.HUMAN_REJECTED
    m.save(update_fields=['status', 'source', 'updated_at'])
    return render(request, 'matchings/_card_resolved.html', {'m': m})


# ---- root --------------------------------------------------------------


@login_required
def index(request):
    return redirect('imports:list')

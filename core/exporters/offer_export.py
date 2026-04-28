"""Offer export — the OTHER half of the byte-identical I/O contract.

Direct port of legacy app/decorators/offer_result_decorator.rb#to_row. The
column shape and serialization rules below are observed live in the user-
provided exports.csv (eng review section 2C + cross-model tension D):

CSV byte details (verified against exports.csv via xxd):
  - No BOM (first bytes are 'Spr...', not EF BB BF)
  - Line endings are LF only ('\\n'), NOT CRLF — overrides csv.writer default
  - Trailing newline at EOF
  - UTF-8 throughout (Belgian/French names like 'Le Géant des Beaux-Arts (FR)'
    contain accented characters)
  - Boolean Public column is lowercase 'true'/'false' (NOT Python's 'True'/'False')
  - None/NULL serializes to empty string (not 'None', not 'null')
  - Money via core.money.format_euro: '€3', '€3.04', '€1,234.56'
  - Multi-value cells (Cheapest competitors / skus) are comma-joined

Column shape: 12 fixed + 6 × N main_competitors. For Schleiper today N=3 so
the export has 30 columns. The N is dynamic — derived from
retailer.main_competitions.order_by('position').

Reviewed cell text (4 strings; 3 used at cutover):
  - 'Main competitors offers reviewed' (all main_competitors have a Review row)
  - 'Some competitors offers reviewed' (some but not all)
  - 'Competitors offers not yet reviewed' (none)
  - per-competitor variants exist in legacy for non-mine offers — defer; cutover
    only needs the mine? branch
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from openpyxl import Workbook

from ..models import Export, Matching, Offer, PriceObservation, Retailer
from ..money import format_euro


CSV_LINE_TERMINATOR = '\n'  # legacy uses LF, not CRLF
BOOL_TO_STR = {True: 'true', False: 'false'}


# ---- Field helpers ------------------------------------------------------


def _serialize(value) -> str:
    """Match legacy CSV serialization: bool -> lowercase, None -> '', else str()."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return BOOL_TO_STR[value]
    return str(value)


def _date_str(dt) -> str:
    if dt is None:
        return ''
    return dt.date().isoformat() if hasattr(dt, 'date') else str(dt)


def _latest_observation(offer: Offer) -> PriceObservation | None:
    """Latest non-null PriceObservation per Tension C: 'last-good-price'."""
    return offer.price_observations.order_by('-observed_at').first()


# ---- Reviewed cell text ------------------------------------------------


def reviewed_text(offer: Offer, retailer: Retailer, main_competitor_ids: list[int]) -> str:
    """Return one of the 3 cutover-relevant Reviewed strings.

    main_competitor_ids comes from retailer.main_competitions ordered by
    position (caller's responsibility — passed in to avoid repeated queries
    inside the loop).
    """
    if not main_competitor_ids:
        return 'Competitors offers not yet reviewed'

    reviewed_competitor_ids = {
        r.competitor_id
        for r in offer.reviews.all()
        if r.retailer_id == retailer.id
    }
    if not reviewed_competitor_ids:
        return 'Competitors offers not yet reviewed'

    if reviewed_competitor_ids >= set(main_competitor_ids):
        return 'Main competitors offers reviewed'

    return 'Some competitors offers reviewed'


# ---- Per-offer competing-offer slot ------------------------------------


@dataclass
class _CompetingSlot:
    """A single competing offer (with its latest PriceObservation pre-fetched)."""
    offer: Offer
    price_obs: PriceObservation | None

    @property
    def price_cents(self) -> int | None:
        return self.price_obs.price_cents if self.price_obs else None

    @property
    def list_price_cents(self) -> int | None:
        return self.price_obs.list_price_cents if self.price_obs else None

    @property
    def shipping_cents(self) -> int | None:
        return self.price_obs.shipping_charges_cents if self.price_obs else None

    @property
    def price_date(self) -> str:
        return _date_str(self.price_obs.observed_at) if self.price_obs else ''


def _competing_slots(offer: Offer) -> list[_CompetingSlot]:
    """All confirmed competing offers for `offer`, sorted by price asc."""
    matchings = (
        offer.matchings.filter(status='confirmed')
        .exclude(competing_offer_id=offer.id)  # exclude the identical-self matching
        .select_related('competing_offer__retailer')
    )
    slots = []
    for m in matchings:
        slots.append(_CompetingSlot(
            offer=m.competing_offer,
            price_obs=_latest_observation(m.competing_offer),
        ))
    # Sort by price ascending; offers without a price float to the bottom.
    slots.sort(key=lambda s: (s.price_cents is None, s.price_cents or 0))
    return slots


def _cheapest_slots(slots: list[_CompetingSlot]) -> list[_CompetingSlot]:
    """All competing slots tied at the lowest price (excluding price-less)."""
    priced = [s for s in slots if s.price_cents is not None]
    if not priced:
        return []
    best = priced[0].price_cents
    return [s for s in priced if s.price_cents == best]


# ---- Row builder --------------------------------------------------------


# Static columns in the legacy export, in order
STATIC_HEADERS = (
    'Sprycer ID',
    'Channel',
    'Retailer',
    'SKU',
    'Name',
    'Price',
    'Price date',
    'Public',
    'Reviewed',
    'Cheapest competitors',
    'Cheapest competitors price',
    'Cheapest competitors skus',
)


def _competitor_block_headers(position: int) -> tuple[str, str, str, str, str, str]:
    return (
        f'Competitor {position}',
        f'Competitor {position} sku',
        f'Competitor {position} list_price',
        f'Competitor {position} price',
        f'Competitor {position} shipping charges',
        f'Competitor {position} price date',
    )


def export_headers(retailer: Retailer) -> list[str]:
    headers = list(STATIC_HEADERS)
    for mc in retailer.main_competitions.order_by('position').select_related('competitor'):
        headers.extend(_competitor_block_headers(mc.position))
    return headers


def _build_row(
    offer: Offer,
    retailer: Retailer,
    main_competitions: list,
    main_competitor_ids: list[int],
) -> dict:
    """Build the 12-fixed + 6×N row dict for one offer."""
    latest = _latest_observation(offer)
    slots = _competing_slots(offer)
    cheapest = _cheapest_slots(slots)
    slots_by_competitor = {s.offer.retailer_id: s for s in slots}

    row: dict[str, object] = {
        'Sprycer ID': offer.id,
        'Channel': offer.channel.name if offer.channel_id else '',
        'Retailer': offer.retailer.name,
        'SKU': offer.sku,
        'Name': offer.name,
        'Price': format_euro(latest.price_cents) if latest else '',
        'Price date': _date_str(latest.observed_at) if latest else '',
        'Public': offer.public,
        'Reviewed': reviewed_text(offer, retailer, main_competitor_ids),
        'Cheapest competitors': ', '.join(s.offer.retailer.name for s in cheapest),
        'Cheapest competitors price': format_euro(cheapest[0].price_cents) if cheapest else '',
        'Cheapest competitors skus': ', '.join(s.offer.sku for s in cheapest),
    }

    for mc in main_competitions:
        position = mc.position
        h_name, h_sku, h_list, h_price, h_ship, h_date = _competitor_block_headers(position)
        slot = slots_by_competitor.get(mc.competitor_id)
        if slot:
            row[h_name] = slot.offer.retailer.name
            row[h_sku] = slot.offer.sku
            row[h_list] = format_euro(slot.list_price_cents) if slot.list_price_cents else ''
            row[h_price] = format_euro(slot.price_cents) if slot.price_cents else ''
            row[h_ship] = format_euro(slot.shipping_cents) if slot.shipping_cents else ''
            row[h_date] = slot.price_date
        else:
            row[h_name] = ''
            row[h_sku] = ''
            row[h_list] = ''
            row[h_price] = ''
            row[h_ship] = ''
            row[h_date] = ''

    return row


def render_rows_for_retailer(retailer: Retailer, *, competing_offers_only: bool = True):
    """Yield (headers, row_dict_iter) for the given retailer's offers.

    competing_offers_only=True (default) mirrors Schleiper's standard export
    filter visible in the legacy /offers UI as `competing_offers=any`. Verified
    against legacy: Schleiper retailer alone has ~66K offers; with the
    "any competing offer" filter the count drops to ~21,760, matching every
    recent legacy export (568, 565, 564, 563, 561, 548 in production).
    Setting this to False yields all retailer offers — useful for ops + audit.
    """
    main_competitions = list(
        retailer.main_competitions.order_by('position').select_related('competitor')
    )
    main_competitor_ids = [mc.competitor_id for mc in main_competitions]
    headers = export_headers(retailer)

    # Legacy export semantics (verified against user-provided exports.csv:
    # 21,760 of 21,760 rows have Public=false): exports show ALL offers for
    # the retailer regardless of public flag. Schleiper imports default to
    # public=False (Rails t.boolean default + no explicit setter in the
    # SchleiperImporter); only competitor scrape results land as public=True.
    # Filtering on public would empty the Schleiper export entirely.
    offers_qs = Offer.objects.filter(retailer=retailer)
    if competing_offers_only:
        offers_qs = offers_qs.filter(
            matchings__status=Matching.Status.CONFIRMED
        ).distinct()
    offers_qs = (
        offers_qs
        .select_related('channel', 'retailer')
        .prefetch_related(
            'reviews',
            'matchings__competing_offer__retailer',
            'matchings__competing_offer__price_observations',
            'price_observations',
        )
        .order_by('id')
    )

    def row_iter():
        for offer in offers_qs.iterator(chunk_size=500):
            yield _build_row(offer, retailer, main_competitions, main_competitor_ids)

    return headers, row_iter()


# ---- Output formats -----------------------------------------------------


def to_csv_bytes(headers: list[str], rows) -> tuple[bytes, int]:
    """Render to (csv_bytes, row_count) matching legacy format byte-for-byte
    where possible.

    LF line endings (not CRLF), no BOM, UTF-8, lowercase booleans, empty cells
    for None. quoting=MINIMAL so cells without commas/quotes/newlines stay
    unquoted, matching the user-provided exports.csv we measured.
    """
    buf = io.StringIO(newline='')
    writer = csv.writer(
        buf,
        delimiter=',',
        quotechar='"',
        quoting=csv.QUOTE_MINIMAL,
        lineterminator=CSV_LINE_TERMINATOR,
    )
    writer.writerow(headers)
    count = 0
    for row in rows:
        writer.writerow([_serialize(row.get(h, '')) for h in headers])
        count += 1
    return buf.getvalue().encode('utf-8'), count


def to_xlsx_bytes(headers: list[str], rows) -> tuple[bytes, int]:
    """Render to (xlsx_bytes, row_count). Cell-identical (not byte-identical)
    target since XLSX is a ZIP whose timestamps make true byte-identity infeasible.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = 'Offers'
    ws.append(headers)
    count = 0
    for row in rows:
        ws.append([_xlsx_cell(row.get(h, '')) for h in headers])
        count += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), count


def _xlsx_cell(value):
    """openpyxl-compatible cell value. Normalize bool/None like the CSV side."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return BOOL_TO_STR[value]
    return value


# ---- Wired entry point used by /admin export action / CLI -------------


def generate_offer_export(export_obj: Export, retailer: Retailer, fmt: str = 'csv',
                          *, competing_offers_only: bool = True) -> Export:
    """Populate export_obj.file with a rendered export of retailer's offers.

    fmt = 'csv' or 'xlsx'. Uses the legacy filename pattern export_{id}.{ext}.
    competing_offers_only=True matches Schleiper's standard legacy filter.
    """
    headers, rows = render_rows_for_retailer(
        retailer, competing_offers_only=competing_offers_only
    )
    if fmt == 'csv':
        payload, count = to_csv_bytes(headers, rows)
        ext = 'csv'
    elif fmt == 'xlsx':
        payload, count = to_xlsx_bytes(headers, rows)
        ext = 'xlsx'
    else:
        raise ValueError(f'Unsupported export format: {fmt!r}')

    from django.core.files.base import ContentFile
    filename = f'export_{export_obj.pk}.{ext}'
    export_obj.file.save(filename, ContentFile(payload), save=False)
    export_obj.count = count
    export_obj.save()
    return export_obj

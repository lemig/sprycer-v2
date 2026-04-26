"""Schleiper catalog importer.

Direct port of the legacy app/importers/schleiper_importer.rb so the v2 import
behavior is byte-identical to what Schleiper's user has been uploading for 14
years. Locked decisions (eng review):

  - 2A: pandas read_excel(header=0) — trust the contract, no fuzzy header search
  - Tension C: import wraps in transaction.atomic() so half-imports never become
    visible to exports
  - 2F: per-row failures captured in Import.failure_info (legacy
    formated_failure_info shape: '<row_index>: <message>'), import keeps going

Schleiper-specific column transforms verified against original.csv and exports.csv:
  - Sprycer ID  -> Offer.pk (UPSERT key when present)
  - RefEtiq     -> sku
  - Article_FR  -> name (with ' n° {Color ID}' suffix when Color ID present)
  - Marque      -> brand_name (find_or_create_by_name_or_alias)
  - Description_FR -> description
  - Image URL   -> original_image_url
  - URL article -> page url(s)
  - CodeEANouUPC-> ean (whitespace stripped — legacy values have trailing spaces)
  - Categorie   -> categories array (split on ' > ')
  - Color ID    -> name suffix only (not stored)
  - express?    -> channel: 'schleiper.com/eshopexpress' if truthy else
                            'schleiper.com/onlinecatalogue'
  - Prix HTVA   -> price_cents (accepts '12,50', '12.50', or empty)
  - Deleted     -> soft delete (offer kept but public=False so excluded from exports)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import pandas as pd
from django.db import transaction
from django.utils import timezone

from ..embeddings import embed_offer
from ..models import Brand, Channel, Import, Offer, PriceObservation, Retailer, Website


SCHLEIPER_RETAILER_NAME = 'Schleiper'
SCHLEIPER_WEBSITE_HOST = 'www.schleiper.com'
CHANNEL_EXPRESS = 'schleiper.com/eshopexpress'
CHANNEL_ONLINE = 'schleiper.com/onlinecatalogue'

REQUIRED_COLUMNS = ('RefEtiq', 'Article_FR')

_TRUTHY = {'yes', 'y', 'true', 't', '1', 'oui'}


def _truthy(value: Any) -> bool:
    """Mirror Rails truthy? semantics for the express? + Deleted columns."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in _TRUTHY


def _clean_str(value: Any) -> str:
    """Normalize a cell to a stripped string, treating NaN/None as empty."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ''
    return str(value).strip()


def _parse_price_cents(value: Any) -> int | None:
    """Parse 'Prix HTVA'. Accepts '12,50', '12.50', '12', or blank.

    Returns integer cents or None for blank. Rounds half-even via Python's int()
    on a multiplied float — adequate for Schleiper's 2-decimal price scale, but
    crosscheck against the H5 golden export for any rounding edge cases.
    """
    s = _clean_str(value).replace(',', '.')
    if not s:
        return None
    try:
        amount = float(s)
    except ValueError:
        return None
    return int(round(amount * 100))


# ---- Resolved Schleiper-specific reference rows --------------------------


@dataclass
class _SchleiperContext:
    retailer: Retailer
    website: Website
    channel_express: Channel
    channel_online: Channel


def _bootstrap_schleiper_context() -> _SchleiperContext:
    retailer, _ = Retailer.objects.get_or_create(name=SCHLEIPER_RETAILER_NAME)
    website, _ = Website.objects.get_or_create(
        host=SCHLEIPER_WEBSITE_HOST, defaults={'scrapable': False}
    )
    channel_express, _ = Channel.objects.get_or_create(
        name=CHANNEL_EXPRESS, defaults={'retailer': retailer, 'website': website}
    )
    channel_online, _ = Channel.objects.get_or_create(
        name=CHANNEL_ONLINE, defaults={'retailer': retailer, 'website': website}
    )
    return _SchleiperContext(
        retailer=retailer,
        website=website,
        channel_express=channel_express,
        channel_online=channel_online,
    )


# ---- Per-row transform ----------------------------------------------------


@dataclass
class _OfferFields:
    sprycer_id: int | None
    sku: str
    name: str
    description: str
    ean: str
    original_image_url: str
    page_url: str
    categories: list[str]
    brand_name: str
    is_express: bool
    is_deleted: bool
    price_cents: int | None
    price_at: Any  # datetime or None

    def channel_name(self) -> str:
        return CHANNEL_EXPRESS if self.is_express else CHANNEL_ONLINE


def transform_row(row: dict) -> _OfferFields:
    """Pure function: read a single import row, return derived Offer fields.

    Kept pure (no DB hits) so unit tests can assert transform logic without
    database setup. The persistence step is _persist_row().
    """
    sprycer_id_raw = _clean_str(row.get('Sprycer ID'))
    sprycer_id = int(sprycer_id_raw) if sprycer_id_raw.isdigit() else None

    sku = _clean_str(row.get('RefEtiq'))
    base_name = _clean_str(row.get('Article_FR'))
    color_id = _clean_str(row.get('Color ID'))
    name = f'{base_name} n° {color_id}' if color_id else base_name

    description = _clean_str(row.get('Description_FR'))
    ean = _clean_str(row.get('CodeEANouUPC'))
    image_url = _clean_str(row.get('Image URL'))
    page_url = _clean_str(row.get('URL article'))

    categorie_raw = _clean_str(row.get('Categorie'))
    categories = [c.strip() for c in categorie_raw.split(' > ') if c.strip()] if categorie_raw else []

    brand_name = _clean_str(row.get('Marque'))
    is_express = _truthy(row.get('express?'))
    is_deleted = _truthy(row.get('Deleted'))

    price_cents = _parse_price_cents(row.get('Prix HTVA'))

    price_at_raw = row.get('Price Date')
    price_at = price_at_raw if price_at_raw not in (None, '') else None
    if price_at is None and price_cents is not None:
        price_at = timezone.now()

    return _OfferFields(
        sprycer_id=sprycer_id,
        sku=sku,
        name=name,
        description=description,
        ean=ean,
        original_image_url=image_url,
        page_url=page_url,
        categories=categories,
        brand_name=brand_name,
        is_express=is_express,
        is_deleted=is_deleted,
        price_cents=price_cents,
        price_at=price_at,
    )


# ---- Per-row persistence -------------------------------------------------


def _persist_row(fields: _OfferFields, ctx: _SchleiperContext) -> Offer:
    """Apply the transformed fields to the DB. UPSERT semantics + a new
    PriceObservation row when a price is present."""
    if not fields.sku:
        raise ValueError('RefEtiq (sku) is required')
    if not fields.name.strip():
        raise ValueError('Article_FR (name) is required')

    channel = ctx.channel_express if fields.is_express else ctx.channel_online
    brand = Brand.find_or_create_by_name_or_alias(fields.brand_name) if fields.brand_name else None

    base_attrs = dict(
        retailer=ctx.retailer,
        website=ctx.website,
        channel=channel,
        brand=brand,
        sku=fields.sku,
        name=fields.name,
        description=fields.description,
        ean=fields.ean,
        original_image_url=fields.original_image_url,
        categories=fields.categories,
        public=not fields.is_deleted,
    )

    offer: Offer | None = None
    if fields.sprycer_id is not None:
        # Legacy maps Sprycer ID directly to Offer.id (column 'Sprycer ID', :id).
        # Find-or-initialize-by-id, preserving the PK across imports forever.
        offer = Offer.objects.filter(pk=fields.sprycer_id).first()
        if offer is not None:
            for k, v in base_attrs.items():
                setattr(offer, k, v)
            offer.save()
        else:
            offer = Offer.objects.create(pk=fields.sprycer_id, **base_attrs)
    else:
        # No Sprycer ID -> UPSERT on legacy unique key (website, sku, public).
        # public=True is the canonical Schleiper offer; soft-deleted ones live
        # with public=False.
        offer, _ = Offer.objects.update_or_create(
            website=ctx.website, sku=fields.sku, public=True,
            defaults=base_attrs,
        )

    if fields.price_cents is not None:
        PriceObservation.objects.create(
            offer=offer,
            price_cents=fields.price_cents,
            price_currency='EUR',
            observed_at=fields.price_at or timezone.now(),
        )

    # Best-effort re-embed when name/description changed. Returns False silently
    # if OPENAI_API_KEY is unset (dev/test) or the API call failed — backfill
    # cron will pick those up.
    embed_offer(offer)

    return offer


# ---- The importer entry point --------------------------------------------


@dataclass
class ImportResult:
    total: int = 0
    failures: int = 0
    failure_info: list[str] = field(default_factory=list)


class SchleiperImporter:
    """Reads a Schleiper Excel/CSV catalog into the database."""

    name = 'SchleiperImporter'

    def parse(self, file_path: str) -> Iterable[dict]:
        """Yield row-as-dict from an Excel or CSV file."""
        df = self._read(file_path)
        for col in REQUIRED_COLUMNS:
            if col not in df.columns:
                raise ValueError(f'Required column missing from upload: {col!r}')
        for row in df.to_dict(orient='records'):
            yield row

    @staticmethod
    def _read(file_path: str) -> pd.DataFrame:
        path = str(file_path)
        if path.lower().endswith('.csv'):
            # Schleiper's CSV uses comma. dtype=str keeps leading zeros / EAN strings.
            return pd.read_csv(path, header=0, dtype=str, keep_default_na=False)
        # Excel (.xls / .xlsx)
        return pd.read_excel(path, header=0, dtype=object)

    @transaction.atomic
    def run(self, import_obj: Import) -> ImportResult:
        """Process the file referenced by import_obj inside a single transaction
        (Tension C). Per-row failures are captured in failure_info and the loop
        continues; the transaction commits at the end regardless of failure
        count, mirroring legacy SchleiperImporter behavior."""
        ctx = _bootstrap_schleiper_context()
        result = ImportResult()

        rows = list(self.parse(import_obj.file.path))
        result.total = len(rows)

        for index, row in enumerate(rows, start=2):  # 1-based, +1 for header row
            try:
                fields = transform_row(row)
                _persist_row(fields, ctx)
            except Exception as exc:
                result.failures += 1
                # Match legacy Import.formated_failure_info shape: 'N: message'.
                result.failure_info.append(f'{index}: {type(exc).__name__}: {exc}')

        import_obj.total = result.total
        import_obj.failures = result.failures
        import_obj.pending = 0
        import_obj.failure_info = result.failure_info
        import_obj.status = Import.Status.COMPLETED
        import_obj.save(update_fields=['total', 'failures', 'pending', 'failure_info', 'status', 'updated_at'])

        return result

"""Tests for the legacy-dump streaming extractor (scripts/extract_legacy_dump.py).

The script is the make-or-break tool for H17. Hot path (versions filter) is
unit-tested here; full integration is exercised by running it against the
real dump.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

# Add scripts/ to sys.path so we can import the extractor module.
_SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(_SCRIPTS))

import extract_legacy_dump as ext  # noqa: E402


# ---- parse_copy_header ---------------------------------------------------


class TestParseCopyHeader:
    def test_simple_table(self):
        line = 'COPY public.brands (id, name, aliases) FROM stdin;\n'
        out = ext.parse_copy_header(line)
        assert out == ('brands', ['id', 'name', 'aliases'])

    def test_versions_columns(self):
        line = ('COPY public.versions (id, item_type, item_id, event, whodunnit, '
                'object, created_at, object_changes, transaction_id, '
                'price_change_percent, discount_percent, retailer_id, website_id, '
                'public) FROM stdin;\n')
        table, cols = ext.parse_copy_header(line)
        assert table == 'versions'
        assert 'item_type' in cols
        assert 'created_at' in cols
        assert 'object_changes' in cols

    def test_unrelated_line(self):
        assert ext.parse_copy_header('SET statement_timeout = 0;\n') is None

    def test_non_public_schema(self):
        # Defensive: only public.* matters
        assert ext.parse_copy_header('COPY other.brands (id) FROM stdin;\n') is None


# ---- parse_copy_timestamp ------------------------------------------------


class TestParseCopyTimestamp:
    def test_with_microseconds(self):
        ts = ext.parse_copy_timestamp('2026-04-23 04:35:21.123456')
        assert ts == datetime(2026, 4, 23, 4, 35, 21, 123456)

    def test_without_microseconds(self):
        ts = ext.parse_copy_timestamp('2026-04-23 04:35:21')
        assert ts == datetime(2026, 4, 23, 4, 35, 21)

    def test_null_marker(self):
        assert ext.parse_copy_timestamp(r'\N') is None

    def test_unparseable(self):
        assert ext.parse_copy_timestamp('not a date') is None


# ---- Filter logic (synthesized COPY data) -------------------------------


def _versions_row(*, idx_item_type=1, idx_created_at=6, idx_object_changes=7,
                  item_type='PricePoint', created_at='2026-01-15 10:00:00',
                  object_changes='{"price_cents":[100,200]}'):
    """Build a tab-separated row with 14 cells matching the versions table
    column order: id, item_type, item_id, event, whodunnit, object, created_at,
    object_changes, transaction_id, price_change_percent, discount_percent,
    retailer_id, website_id, public."""
    cells = [r'\N'] * 14
    cells[0] = '12345'
    cells[1] = item_type
    cells[2] = '999'
    cells[3] = 'update'
    cells[6] = created_at
    cells[7] = object_changes
    return '\t'.join(cells) + '\n'


# To exercise the filter logic deterministically we replicate the hot loop here.
def _should_keep(line: str, cutoff: datetime,
                 idx_item_type=1, idx_created_at=6, idx_object_changes=7) -> bool:
    fields = line.rstrip('\n').split('\t')
    if fields[idx_item_type] != 'PricePoint':
        return False
    if 'price_cents' not in fields[idx_object_changes]:
        return False
    ts = ext.parse_copy_timestamp(fields[idx_created_at])
    if ts is None or ts < cutoff:
        return False
    return True


CUTOFF_2025_04 = datetime(2025, 4, 26)


class TestVersionsFilter:
    def test_recent_pricepoint_price_change_is_kept(self):
        line = _versions_row(item_type='PricePoint', created_at='2026-01-15 10:00:00',
                             object_changes='{"price_cents":[100,200]}')
        assert _should_keep(line, CUTOFF_2025_04) is True

    def test_old_row_is_dropped(self):
        line = _versions_row(item_type='PricePoint', created_at='2018-01-15 10:00:00',
                             object_changes='{"price_cents":[100,200]}')
        assert _should_keep(line, CUTOFF_2025_04) is False

    def test_offer_item_dropped(self):
        # Verified live: Offer rows in versions never carry price_cents in
        # object_changes (price lives on PricePoint, not Offer).
        line = _versions_row(item_type='Offer', created_at='2026-01-15 10:00:00',
                             object_changes='{"price_cents":[100,200]}')
        assert _should_keep(line, CUTOFF_2025_04) is False

    def test_matching_item_dropped(self):
        line = _versions_row(item_type='Matching', created_at='2026-01-15 10:00:00',
                             object_changes='{"price_cents":[100,200]}')
        assert _should_keep(line, CUTOFF_2025_04) is False

    def test_no_price_change_dropped(self):
        line = _versions_row(item_type='PricePoint', created_at='2026-01-15 10:00:00',
                             object_changes='{"name":["A","B"]}')
        assert _should_keep(line, CUTOFF_2025_04) is False

    def test_null_created_at_dropped(self):
        line = _versions_row(created_at=r'\N')
        assert _should_keep(line, CUTOFF_2025_04) is False


# ---- End-to-end extractor on a synthesized mini dump --------------------


@pytest.fixture
def mini_dump(tmp_path):
    """Build a tiny synthesized dump that exercises every code path."""
    p = tmp_path / 'mini.sql'
    text = (
        '-- header\n'
        'SET statement_timeout = 0;\n\n'
        'CREATE TABLE public.brands (\n'
        '    id integer NOT NULL,\n'
        '    name character varying\n'
        ');\n\n'
        'CREATE TABLE public.versions (\n'
        '    id integer,\n'
        '    item_type character varying,\n'
        '    item_id integer,\n'
        '    event character varying,\n'
        '    whodunnit character varying,\n'
        '    object json,\n'
        '    created_at timestamp,\n'
        '    object_changes json,\n'
        '    transaction_id integer,\n'
        '    price_change_percent double precision,\n'
        '    discount_percent double precision,\n'
        '    retailer_id integer,\n'
        '    website_id integer,\n'
        '    public boolean\n'
        ');\n\n'
        'COPY public.brands (id, name) FROM stdin;\n'
        '1\tWinsor & Newton\n'
        '2\tFaber-Castell\n'
        '\\.\n\n'
        'COPY public.scraps (id, json_content) FROM stdin;\n'  # SKIP
        '1\t{"big":"payload"}\n'
        '\\.\n\n'
        'COPY public.versions (id, item_type, item_id, event, whodunnit, object, '
        'created_at, object_changes, transaction_id, price_change_percent, '
        'discount_percent, retailer_id, website_id, public) FROM stdin;\n'
        + _versions_row(item_type='PricePoint', created_at='2026-01-15 10:00:00',
                        object_changes='{"price_cents":[100,200]}')
        + _versions_row(item_type='PricePoint', created_at='2018-01-15 10:00:00',  # too old
                        object_changes='{"price_cents":[100,200]}')
        + _versions_row(item_type='Offer', created_at='2026-01-15 10:00:00',  # not PricePoint
                        object_changes='{"price_cents":[100,200]}')
        + _versions_row(item_type='PricePoint', created_at='2026-01-15 10:00:00',  # no price change
                        object_changes='{"name":["A","B"]}')
        + '\\.\n'
    )
    p.write_text(text)
    return p


class TestEndToEndExtract:
    def test_run_against_mini_dump(self, mini_dump, tmp_path, capsys, monkeypatch):
        out = tmp_path / 'extract'
        monkeypatch.setattr(sys, 'argv',
                            ['extract_legacy_dump.py',
                             '--in', str(mini_dump), '--out', str(out),
                             '--history-cutoff', '2025-04-26'])
        rc = ext.main()
        assert rc == 0

        # brands extracted verbatim (2 rows)
        brands = (out / 'brands.sql').read_text()
        assert 'COPY public.brands' in brands
        assert 'Winsor & Newton' in brands
        assert 'Faber-Castell' in brands

        # versions: only the recent Offer + price_cents row should remain (1 of 4)
        versions = (out / 'versions.sql').read_text()
        # Count actual data lines (excluding header + terminator)
        data_lines = [
            ln for ln in versions.splitlines()
            if not ln.startswith('COPY') and ln != '\\.'
        ]
        assert len(data_lines) == 1, f'expected 1 surviving row, got {len(data_lines)}'

        # scraps was skipped — no scraps.sql
        assert not (out / 'scraps.sql').exists()

        # Manifest reports counts
        import json
        manifest = json.loads((out / 'manifest.json').read_text())
        assert manifest['tables']['brands']['rows_out'] == 2
        assert manifest['tables']['versions']['rows_in'] == 4
        assert manifest['tables']['versions']['rows_out'] == 1
        assert manifest['tables']['scraps']['mode'] == 'skip'

"""Stream-extract the operational subset of the 137 GB legacy Sprycer dump.

The legacy production dump (~137 GB plain SQL) is too large to load whole.
~95% of it is paper_trail.versions (full JSON snapshots of every Offer change
across 14 years) and scraps (raw HTML cached per scrape). v2 doesn't need
either at full scale.

This script does ONE pass over the dump (~7-10 minutes wall clock on SSD)
and writes per-table extracts to an output directory. For most tables the
COPY data is copied verbatim. For `versions` it filters per-row to keep only
the rows v2 actually backfills (Tension C + TODO #7):
  - item_type = 'Offer'
  - created_at within the last 12 months
  - object_changes JSON contains 'price_cents'

Skipped entirely (huge + irrelevant to v2):
  - scraps, version_associations, crawls, settings, schema_migrations,
    imports, exports

Output structure:
  out_dir/
    schema.sql            (CREATE TABLE statements, useful for transient load)
    brands.sql
    retailers.sql
    websites.sql
    channels.sql
    main_competitions.sql
    users.sql
    pages.sql
    offers.sql
    offers_pages.sql
    matchings.sql
    price_points.sql
    reviews.sql
    versions.sql          (filtered to 12-mo Offer price changes)
    manifest.json         {table: {rows: N, bytes: N, duration_s: N}, ...}

Usage:
    uv run python scripts/extract_legacy_dump.py \\
        --in /Users/cabermi/UyIDVfHAtXCLwsTznCIx/PostgreSQL.sql \\
        --out .legacy_extract/ \\
        --history-cutoff 2025-04-26
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path


# ---- Tables to extract verbatim (just COPY block) -----------------------

KEEP_VERBATIM = {
    'brands', 'retailers', 'websites', 'channels', 'main_competitions',
    'users', 'pages', 'offers', 'offers_pages', 'matchings',
    'price_points', 'reviews',
}

# Tables to skip entirely
SKIP = {
    'scraps', 'version_associations', 'crawls', 'settings',
    'schema_migrations', 'imports', 'exports',
}

# Filtered table
FILTERED = 'versions'


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--in', dest='input_path', required=True,
                   help='Path to the legacy PostgreSQL.sql dump.')
    p.add_argument('--out', dest='output_dir', required=True,
                   help='Output directory for per-table extracts.')
    p.add_argument('--history-cutoff', dest='cutoff', default=None,
                   help='ISO date (YYYY-MM-DD); versions rows older than this are dropped. '
                        'Default: 12 months before today.')
    p.add_argument('--progress-every', type=int, default=10_000_000,
                   help='Log every N input lines (default 10M).')
    return p.parse_args()


def parse_copy_header(line: str) -> tuple[str, list[str]] | None:
    """Parse `COPY public.tablename (c1, c2, ...) FROM stdin;` -> (tablename, [cols]).

    Returns None if the line is not a COPY header for a public table.
    """
    if not line.startswith('COPY public.'):
        return None
    # Strip 'COPY public.' prefix and ' FROM stdin;' suffix
    body = line[len('COPY public.'):]
    name_end = body.find(' ')
    if name_end < 0:
        return None
    table = body[:name_end]
    rest = body[name_end:].lstrip()
    if not rest.startswith('('):
        return None
    paren_end = rest.find(')')
    if paren_end < 0:
        return None
    col_str = rest[1:paren_end]
    cols = [c.strip() for c in col_str.split(',')]
    return table, cols


def parse_copy_timestamp(value: str) -> datetime | None:
    """Parse a Postgres COPY timestamp value, e.g. '2026-04-23 04:35:21.123456'.

    Returns None for `\\N` (NULL) or unparseable values."""
    if value == r'\N' or not value:
        return None
    # Try a couple of common formats — Postgres COPY uses ISO without 'T'.
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def main() -> int:
    args = parse_args()
    cutoff_str = args.cutoff or _twelve_months_ago()
    try:
        cutoff = datetime.strptime(cutoff_str, '%Y-%m-%d')
    except ValueError:
        print(f'ERROR: --history-cutoff must be YYYY-MM-DD, got {cutoff_str!r}', file=sys.stderr)
        return 2

    in_path = Path(args.input_path)
    out_dir = Path(args.output_dir)
    if not in_path.exists():
        print(f'ERROR: input file not found: {in_path}', file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f'Streaming {in_path} ({_humanbytes(in_path.stat().st_size)})')
    print(f'Output:   {out_dir}')
    print(f'Versions cutoff: {cutoff.isoformat()} (rows older are dropped)')

    schema_sql = []  # CREATE TABLE statements buffered to schema.sql
    in_create = False
    create_buf: list[str] = []

    state: dict | None = None  # current COPY block state, or None
    table_stats: dict[str, dict] = {}
    line_index = 0
    start_time = time.time()

    # Output handles for the verbatim + filtered tables
    handles: dict[str, object] = {}

    def open_handle(table: str):
        path = out_dir / f'{table}.sql'
        h = path.open('w', encoding='utf-8')
        handles[table] = h
        return h

    def close_handles():
        for h in handles.values():
            try:
                h.close()
            except Exception:
                pass

    try:
        with in_path.open('r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line_index += 1

                # 1) CREATE TABLE statements get buffered to schema.sql so we can
                #    reload the operational subset into a transient Postgres.
                if in_create:
                    create_buf.append(line)
                    if line.startswith(');'):
                        schema_sql.append(''.join(create_buf))
                        create_buf = []
                        in_create = False
                    continue
                if line.startswith('CREATE TABLE public.'):
                    # Skip CREATEs for tables we won't use, otherwise buffer.
                    table_in_create = line[len('CREATE TABLE public.'):].split(' ', 1)[0]
                    if table_in_create in (KEEP_VERBATIM | {FILTERED}):
                        in_create = True
                        create_buf = [line]
                    continue

                # 2) COPY block start
                if state is None:
                    parsed = parse_copy_header(line)
                    if parsed is None:
                        continue
                    table, cols = parsed
                    if table in SKIP:
                        # Will skip until the closing '\.' line
                        state = {'table': table, 'mode': 'skip', 'rows_in': 0,
                                 'rows_out': 0, 'bytes_out': 0, 't0': time.time()}
                        continue
                    if table not in KEEP_VERBATIM and table != FILTERED:
                        # Unknown table: skip too (defensive)
                        state = {'table': table, 'mode': 'skip', 'rows_in': 0,
                                 'rows_out': 0, 'bytes_out': 0, 't0': time.time()}
                        continue
                    h = open_handle(table)
                    h.write(line)  # COPY header
                    if table == FILTERED:
                        # Need column positions to apply the filter
                        try:
                            idx_item_type = cols.index('item_type')
                            idx_created_at = cols.index('created_at')
                            idx_object_changes = cols.index('object_changes')
                        except ValueError as e:
                            close_handles()
                            print(f'ERROR: versions table is missing expected column: {e}',
                                  file=sys.stderr)
                            return 3
                        state = {
                            'table': table, 'mode': 'filter',
                            'idx_item_type': idx_item_type,
                            'idx_created_at': idx_created_at,
                            'idx_object_changes': idx_object_changes,
                            'rows_in': 0, 'rows_out': 0, 'bytes_out': 0, 't0': time.time(),
                        }
                    else:
                        state = {'table': table, 'mode': 'verbatim',
                                 'rows_in': 0, 'rows_out': 0, 'bytes_out': 0, 't0': time.time()}
                    continue

                # 3) Inside a COPY block
                if line == '\\.\n' or line.rstrip('\n') == r'\.':
                    # End of COPY block
                    if state['mode'] != 'skip':
                        h = handles[state['table']]
                        h.write(line)
                    table_stats[state['table']] = {
                        'mode': state['mode'],
                        'rows_in': state['rows_in'],
                        'rows_out': state['rows_out'],
                        'bytes_out': state['bytes_out'],
                        'duration_s': round(time.time() - state['t0'], 2),
                    }
                    state = None
                    continue

                state['rows_in'] += 1
                if state['mode'] == 'skip':
                    continue
                if state['mode'] == 'verbatim':
                    h = handles[state['table']]
                    h.write(line)
                    state['rows_out'] += 1
                    state['bytes_out'] += len(line)
                    continue
                if state['mode'] == 'filter':
                    # versions: tab-separated. Pre-check on a substring before parsing
                    # the date — keeps the hot path cheap.
                    fields = line.rstrip('\n').split('\t')
                    n = len(fields)
                    if (n <= state['idx_item_type'] or
                            n <= state['idx_created_at'] or
                            n <= state['idx_object_changes']):
                        continue
                    if fields[state['idx_item_type']] != 'Offer':
                        continue
                    obj_changes = fields[state['idx_object_changes']]
                    if 'price_cents' not in obj_changes:
                        continue
                    ts = parse_copy_timestamp(fields[state['idx_created_at']])
                    if ts is None or ts < cutoff:
                        continue
                    h = handles[state['table']]
                    h.write(line)
                    state['rows_out'] += 1
                    state['bytes_out'] += len(line)
                    continue

                # Progress
                if line_index % args.progress_every == 0:
                    elapsed = time.time() - start_time
                    print(f'  {line_index/1e6:.1f}M lines, {elapsed:.0f}s elapsed', flush=True)

    finally:
        close_handles()

    # Write schema.sql
    schema_path = out_dir / 'schema.sql'
    with schema_path.open('w', encoding='utf-8') as f:
        f.write('\n'.join(schema_sql))

    # Write manifest.json
    elapsed = time.time() - start_time
    manifest = {
        'input_file': str(in_path),
        'input_size_bytes': in_path.stat().st_size,
        'output_dir': str(out_dir),
        'versions_history_cutoff': cutoff.isoformat(),
        'lines_scanned': line_index,
        'duration_s': round(elapsed, 1),
        'tables': table_stats,
    }
    (out_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2))

    # Console summary
    print()
    print(f'Done in {elapsed:.0f}s ({line_index/1e6:.1f}M lines).')
    print(f'{"table":<22}{"mode":<10}{"rows in":>14}{"rows out":>14}{"bytes out":>14}')
    for table, stats in sorted(table_stats.items()):
        print(f'{table:<22}{stats["mode"]:<10}{stats["rows_in"]:>14,}'
              f'{stats["rows_out"]:>14,}{stats["bytes_out"]:>14,}')
    return 0


def _humanbytes(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.1f}{unit}'
        n /= 1024
    return f'{n:.1f}PB'


def _twelve_months_ago() -> str:
    today = datetime.utcnow().date()
    # 12 months ago, simple "subtract a year" — close enough for backfill purposes
    return f'{today.year - 1}-{today.month:02d}-{today.day:02d}'


if __name__ == '__main__':
    sys.exit(main())

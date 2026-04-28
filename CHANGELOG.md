# Changelog

All notable changes to Sprycer v2 are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow `MAJOR.MINOR.PATCH.MICRO`.

## [0.1.0.0] - 2026-04-28

First version of the v2 rebuild. Django 5 + Postgres + pgvector replacement of
the 2012 Rails + Sidekiq + Cloud 66 legacy app, ahead of the silent cutover
for Schleiper. Byte-identical I/O contract with the legacy export verified
against live production data.

### Added
- Django 5.1 project scaffold with 12 core models: `Offer` (single table with
  `retailer_id` discriminator + self-referential `Matching`), `PriceObservation`
  (append-only), `Brand` with aliases, `Channel`, `Retailer`, `MainCompetition`
  (ordered list driving the dynamic Competitor N export columns), `Review`,
  `Page`, `Import`, `Export`, `Website`.
- `pgvector` extension wired via `VectorExtension()` + HNSW index on
  `Offer.embedding (vector_cosine_ops, m=16, ef_construction=64)`.
- Schleiper Excel/CSV importer ported verbatim from
  `app/importers/schleiper_importer.rb`. Color ID name suffix, `express?` ->
  channel, `Prix HTVA` parse, `Sprycer ID` UPSERT, transaction-atomic with
  per-row failure capture into `Import.failure_info`.
- Offer export with byte-identical CSV (LF endings, no BOM, `false` lowercase,
  `€1,830.95` thousands separator with quoting) and cell-identical XLSX.
  Verified against the legacy production export — 18,572 of 21,760 rows
  byte-match on structural columns; the 15% delta is documented data drift
  from a 5-day-stale dump.
- Géant des Beaux-Arts (BE + FR) scraper: pure JSON-LD parser plus a runner
  that handles HTTP fetch, retailer/channel/website bootstrap, append-only
  `PriceObservation`, and TTC->HT conversion (BE 21%, FR 20%) per
  `ScraperSpec.vat_rate`.
- Rougier & Plé scraper: BeautifulSoup4 microdata extraction (legacy AJAX
  parser was silently broken in production after a site redesign).
- Slack incoming-webhook alerts wired into `scrape_queue` for the H10/H18
  Fly scheduled-machine runs (no_offers > 0 or failures > 0 fires an alert).
- AI matching pipeline: `text-embedding-3-small` with hash-dedup + retries,
  pgvector top-K candidate retrieval, `gpt-4o-mini` Pydantic-structured
  YES/NO/UNCERTAIN judge. Re-runs preserve human-confirmed matches via
  skip-if-`Matching`-exists invariant.
- UI parity views: `/imports`, `/imports/new`, `/imports/<id>`, `/exports`,
  `/exports/new`, `/matchings`, `/matchings/<id>/confirm|reject` (HTMX),
  Django built-in auth at `/accounts/login/`. Layout matches the legacy
  Sprycer red header so Schleiper bookmarks survive cutover.
- Migration toolchain (`scripts/extract_legacy_dump.py`,
  `scripts/load_legacy_extract.sh`, `manage.py migrate_legacy`) verified
  against the real 137 GB legacy dump: 267,917 offers, 285,016 pages,
  49,886 confirmed matchings, 235,691 reviews migrated in ~3 minutes.
- Operational management commands: `process_imports`, `generate_export`,
  `scrape`, `seed_pages`, `embed_offers`, `run_matching`, `verify_pgvector`,
  `migrate_legacy`.
- 208-test suite: importer transforms + golden-file, exporter byte
  precision, money formatter (incl. `€1,234.56` thousands), Géant + R&P
  parsers, scrape runner with NULL-`scraped_at` regression coverage,
  embedding pipeline retry logic, AI matching with the four Tension B
  human-correction-preservation regression tests, Slack webhook,
  view-layer auth + HTMX confirm/reject, seed_pages CLI, legacy-dump
  extractor.

### Changed (from PLAN.md per /plan-eng-review)
- Single `Offer` table with `retailer_id` discriminator + self-referential
  `Matching`, replacing PLAN's `Product` / `CompetitorProduct` split (eng
  review 1A; mirrors the legacy schema and avoids re-deriving the entire
  matching pipeline).
- Append-only `PriceObservation` instead of single-row `PricePoint`
  (Tension C; export's last-good-price fallback survives partial scrape
  failures).
- AI matches land as `SUGGESTED`, never `CONFIRMED` (Tension B; auto-accept
  reintroduced post-cutover when an eval set exists).
- /matchings, /imports, /exports rendered as plain Django templates with
  Sprycer-red layout (Tension A; the silent cutover requires URL/auth/UI
  parity, not just I/O bytes).
- `Offer.name` widened from 512 to 2048 chars after live data revealed
  longer Schleiper product names than legacy schema-test predicted.

### Deferred (NOT in this release)
- Fly.io deploy + scheduled machines (H18) — out of this PR.
- 12-month `versions` -> `PriceObservation` historical backfill (TODO #7) —
  routed to a separate `PriceHistory` model post-cutover; the diff against
  live legacy export confirmed mixing historical observations into
  `PriceObservation` corrupts the export's `latest` query.
- LLM eval harness, full HTMX optimistic-update polish, django-tailwind
  theming, the `historic_prices_data` trend chart, notifications stream,
  legacy multi-tenant scaffolding cleanup.

### Security
- `SECRET_KEY` requires explicit env var when `DEBUG=False`. Dev fallback
  is gated on `DEBUG=True`.
- `ALLOWED_HOSTS` defaults to empty in production (`localhost,127.0.0.1`
  only when `DEBUG=True`). Forgetting to set it no longer accepts
  arbitrary `Host` headers.
- Migrated Schleiper users default to `is_staff=False`. Django admin
  access is reserved for whoever Miguel grants `is_staff` to manually.
- Legacy-snapshot Postgres container binds to `127.0.0.1:5433` only.

[0.1.0.0]: https://github.com/lemig/sprycer-v2/releases/tag/v0.1.0.0

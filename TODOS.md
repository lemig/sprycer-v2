# TODOs

Items deferred from PLAN.md and the 2026-04-26 `/plan-eng-review` session.
Format: skill/component group, then priority (P0 top → P4 bottom), then
Completed at the bottom.

## Cutover

**12-month versions backfill (TODO #7 from eng review)**
- **Priority:** P1
- **What:** route `versions.item_type='PricePoint'` rows from the last 12 months
  into a separate `PriceHistory` model so the export's "latest PriceObservation
  per offer" query is unaffected.
- **Why:** the v0.1.0.0 migration command supports `--history-months N` but it
  defaults to 0 because mixing partial (price-only, no list_price) historical
  rows into `PriceObservation` corrupted the export's "Cheapest competitors
  price" column on offers with frequent price changes.
- **How:** add `PriceHistory` model with `(offer, observed_at, price_cents)`
  schema; update `migrate_legacy._migrate_historical_versions` to write there
  instead. Export query stays on `PriceObservation`.
- **Where:** `core/models.py`, `core/management/commands/migrate_legacy.py`.

**R&P silent-failure verification (TODO #6 from eng review)**
- **Priority:** P1
- **What:** confirm whether the legacy R&P scraper has been silently returning
  zero offers in production (the legacy AJAX endpoints are gone after the site
  redesign).
- **Why:** if confirmed, R&P prices in legacy production are stale; v2's
  microdata-based scraper will produce the first fresh R&P data in months.
  Worth knowing before cutover so Miguel can frame it as a feature, not a
  regression, if Schleiper notices.
- **How:** run on the loaded legacy DB:
  ```sql
  SELECT date_trunc('day', created_at) AS day,
         count(*) FILTER (WHERE offer_count > 0) AS ok,
         count(*) FILTER (WHERE offer_count = 0) AS empty
  FROM scraps JOIN pages USING (page_id) JOIN websites ON websites.id = pages.website_id
  WHERE websites.host = 'www.rougier-ple.fr' AND created_at > NOW() - INTERVAL '30 days'
  GROUP BY 1 ORDER BY 1 DESC;
  ```

**Pre-cutover dump rehearsal**
- **Priority:** P0
- **What:** on cutover day, take a fresh dump of the live legacy DB, run the
  full extract -> load -> migrate -> export chain end-to-end, diff against a
  legacy export taken seconds later. With no time gap, the diff should be
  byte-identical.
- **Why:** the 5-day-old dump rehearsal proved 18,572 of 21,760 rows
  byte-identical; the remaining 15% is documented data drift. A fresh dump
  closes the gap.
- **Where:** `scripts/extract_legacy_dump.py`, `scripts/load_legacy_extract.sh`,
  `manage.py migrate_legacy`.

## Deploy (H18)

**Fly.io deploy configs**
- **Priority:** P0
- **What:** Dockerfile, fly.toml, Fly Managed Postgres setup, scheduled machines
  for `scrape --queue`, `process_imports --watch`, `embed_offers --only-missing`,
  `run_matching` post-scrape. Secrets list (`SECRET_KEY`, `OPENAI_API_KEY`,
  `SLACK_WEBHOOK_URL`, all `POSTGRES_*`).
- **Why:** v0.1.0.0 ships the code + tests + migration tooling; H18 of the
  original plan was deploy. Out of scope for this PR.
- **Where:** new top-level `Dockerfile`, `fly.toml`.

**DNS swap UX**
- **Priority:** P2
- **What:** active Schleiper sessions during DNS propagation will see "site
  moved" briefly. Communicate the cutover window or schedule it at 2am
  Belgian time.
- **Where:** runbook, not code.

## Matching pipeline

**LLM eval harness**
- **Priority:** P1 (blocking the auto-accept threshold flip)
- **What:** a labeled-pair eval suite that scores `gpt-4o-mini`'s YES/NO/
  UNCERTAIN decisions against ground truth (legacy confirmed matchings as
  positives, legacy rejected matchings as negatives).
- **Why:** Tension B locked auto-accept disabled until an eval set exists.
  The pipeline ships in suggest-only mode; flipping the threshold without
  an eval is "fake precision."
- **How:** seed from legacy. Track precision and recall on every prompt
  change. Block CI on regressions below baseline.

**R&P variant page handling**
- **Priority:** P3
- **What:** color-variant + discriminant-choice pages on R&P don't expose
  prices in microdata at the listing level (prices load per-color via JS).
  The current parser returns `[]` for those; H10's `NoOffersFound` Slack
  alert fires.
- **Where:** `core/scrapers/rougier.py`. Either reverse-engineer the per-
  variant AJAX or use a headless browser for those URLs only.

## Imports

**Friendly error message on malformed Excel**
- **Priority:** P3
- **What:** `pandas.read_excel` on a malformed/empty/preamble-row file
  currently raises into the import view as a 500. Wrap in try/except and
  render a readable error in the `Import.failure_info` table on `/imports/<id>`.
- **Where:** `core/importers/schleiper.py`.

## UI polish

**HTMX optimistic-update polish**
- **Priority:** P3
- **What:** `/matchings/<id>/confirm` does a server round-trip and swaps
  the card. Optimistic update would feel snappier.
- **Where:** `templates/matchings/_card.html`, `templates/matchings/list.html`.

**django-tailwind theming**
- **Priority:** P4
- **What:** templates use a single inline `<style>` block in
  `layouts/base.html` to keep the Sprycer-red look without dragging in
  Tailwind for v0.1. Worth converting once the cutover is stable.
- **Where:** `templates/`.

**`historic_prices_data` trend chart**
- **Priority:** P4
- **What:** legacy admin had a per-offer historical price chart driven by
  `paper_trail.versions`. Once the `PriceHistory` model lands (P1 above),
  rebuild the chart from those rows.
- **Where:** `core/admin.py`, new chart partial.

## Cleanup

**Multi-tenant scaffolding cleanup**
- **Priority:** P4
- **What:** PLAN.md notes ~50% of the legacy 2012 codebase was multi-tenant
  scaffolding for a startup vision that never materialized. v2 is single-
  retailer (Schleiper-only). Audit unused retailer-scope helpers and
  trim them.
- **Where:** scan for unused `current_retailer` references, etc.

**Brand abbreviation autocomplete**
- **Priority:** P4
- **What:** legacy `Retailer.abbrev` feature drove a Sprycer-style brand
  search autocomplete. Nice for admin search; defer.

**Notifications model**
- **Priority:** P4
- **What:** legacy had a notification stream for price-change events.
  Schleiper hasn't asked for it; defer until they do.

## Completed

(Empty — this is the first version. v0.1.0.0 is the initial release.)

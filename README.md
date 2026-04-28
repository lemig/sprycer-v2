# Sprycer v2

Price-monitoring app for **Schleiper**, a Belgian art-supplies retailer. v2 is a 2026 weekend
rebuild of a 2012 Rails + Sidekiq + Cloud 66 app. Single-tenant. Internal tool. No public
deployment.

The hard contract: **byte-identical I/O with the legacy app on cutover.** Schleiper's weekly
catalog upload and competitor-price export must match exactly so the cutover is silent.

> Long-form context lives in [`PLAN.md`](./PLAN.md) (the original weekend plan, dated
> 2026-04-25). Release history is in [`CHANGELOG.md`](./CHANGELOG.md). Deferred work is in
> [`TODOS.md`](./TODOS.md).

---

## Stack

- **Django 5.1** — admin, auth, ORM, migrations, templates, management commands
- **Postgres + pgvector** — single Offer table with `retailer_id` discriminator, append-only
  `PriceObservation`, HNSW index on `Offer.embedding (vector_cosine_ops, m=16, ef_construction=64)`
- **OpenAI** — `text-embedding-3-small` for product embeddings, `gpt-4o-mini` for the final
  YES/NO/UNCERTAIN judge on close pairs
- **Scraping** — plain `httpx` + JSON-LD parsing (Géant) and BeautifulSoup4 microdata (Rougier &
  Plé). No Playwright. No anti-bot service.
- **UI** — Django templates + HTMX. No SPA. Sprycer-red layout matches legacy so bookmarks
  survive cutover.
- **Spreadsheets** — `pandas` + `openpyxl` for the Schleiper Excel/CSV importer and the offer
  export

Total monthly cost target: ~$10 (Fly.io) + ~$1 (OpenAI at this scale).

---

## Architecture at a glance

```
Schleiper Excel  ─►  process_imports  ─►  Offer (retailer=Schleiper)
                                              │
Competitor URLs ─►  scrape  ─►  Offer + PriceObservation (append-only)
                                              │
                                    embed_offers (post_save signal)
                                              │
                                    run_matching ─► pgvector top-K
                                              │     │
                                              │     └► gpt-4o-mini judge
                                              │
                                    Matching (status: SUGGESTED)
                                              │
                          /matchings (HTMX confirm/reject)
                                              │
                                    Matching (status: CONFIRMED)
                                              │
                                    generate_export ─► byte-identical CSV/XLSX
```

Twelve core models in `core/models.py`: `Offer`, `Matching` (self-referential),
`PriceObservation`, `Brand`, `Channel`, `Retailer`, `MainCompetition`, `Review`, `Page`,
`Import`, `Export`, `Website`. AI matches are never auto-confirmed in v0.1 — they land as
`SUGGESTED` and require human review at `/matchings`.

---

## Dev quickstart

Requires Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and a local Postgres with the
`vector` extension.

```bash
# Install deps
uv sync

# Configure env (copy + edit)
cat > .env <<'EOF'
DEBUG=True
SECRET_KEY=dev-only-not-for-prod
POSTGRES_DB=sprycer
POSTGRES_USER=sprycer
POSTGRES_PASSWORD=
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
OPENAI_API_KEY=sk-...
SLACK_WEBHOOK_URL=
EOF

# Create DB + run migrations
createdb sprycer
uv run python manage.py migrate
uv run python manage.py verify_pgvector

# Create a superuser, then run the dev server
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Open `http://127.0.0.1:8000/` and log in. URLs: `/imports`, `/exports`, `/matchings`,
`/admin`.

Run the test suite:

```bash
uv run pytest          # 208 tests, ~5s
uv run pytest -k offer_export   # one file
```

---

## Commands cheat sheet

All commands run via `uv run python manage.py <name>`.

| Command            | What it does                                                    |
|--------------------|-----------------------------------------------------------------|
| `process_imports`  | Drain pending `Import` rows (Schleiper Excel/CSV upload).       |
| `generate_export`  | Generate an offer export (CSV or XLSX) for a given retailer.    |
| `scrape`           | Scrape a single URL or walk the `Page` queue (`--queue`).       |
| `seed_pages`       | Seed `Page` rows from a list of URLs (one per line).            |
| `embed_offers`     | Backfill OpenAI embeddings where the offer hash changed.        |
| `run_matching`     | Run the AI matching pipeline (pgvector top-K + LLM judge).      |
| `verify_pgvector`  | Sanity-check pgvector + HNSW after migrate.                     |
| `migrate_legacy`   | One-shot migration from the legacy Postgres into v2.            |

Two helper scripts live in `scripts/`:

- `extract_legacy_dump.py` — streams the 137 GB legacy SQL dump, filters versions to
  PricePoint changes only, writes a ~500 MB extract.
- `load_legacy_extract.sh` — spins a transient Postgres container on `127.0.0.1:5433`,
  loads the extract.

---

## Cutover runbook

The cutover is silent: legacy stays up, v2 takes over without Schleiper noticing. The export
they email weekly must match the legacy export byte-for-byte on structural columns. Sequence:

1. **Take a fresh dump** of the live legacy Postgres on cutover morning.
2. **Extract the operational subset:**
   ```bash
   uv run python scripts/extract_legacy_dump.py /path/to/legacy.sql .legacy_extract/
   ```
   Streams ~137 GB once, writes per-table CSVs, filters `versions` to PricePoint+price_cents
   rows. ~2–3 min wall clock on SSD.
3. **Load into a transient Postgres:**
   ```bash
   bash scripts/load_legacy_extract.sh .legacy_extract/
   ```
   Spins up Docker Postgres on `127.0.0.1:5433` (network-isolated). Loads in ~3 min.
4. **Migrate into v2:**
   ```bash
   uv run python manage.py migrate_legacy \
     --legacy-url postgres://postgres:legacypw@localhost:5433/sprycer_legacy
   ```
   Migrates retailers, websites, channels, brands, offers, pages, matchings, reviews, users.
   `--history-months 0` by default (TODO #7 deferred — see `TODOS.md`).
5. **Generate v2 export and diff against a fresh legacy export:**
   ```bash
   uv run python manage.py generate_export --retailer Schleiper --format csv > v2.csv
   diff <(sort legacy.csv) <(sort v2.csv)
   ```
   With no time gap between the dumps, the diff should be empty on structural columns. The
   5-day-old rehearsal showed 18,572 of 21,760 rows byte-identical (15% delta = documented
   data drift).
6. **DNS swap.** Active sessions briefly see "site moved" during propagation. Schedule for 2am
   Belgian time.

---

## Schleiper-cutover specifics

- **Catalog upload route stays the same:** `/imports/new` with the same Excel/CSV columns
  the legacy app expected. The transform logic was ported verbatim from
  `app/importers/schleiper_importer.rb`.
- **Export columns stay the same:** dynamic `Competitor N` columns driven by the
  `MainCompetition.position` ordering. Money formatting matches `humanized_money_with_symbol`
  English-locale output (`€3`, `€3.04`, `"€1,830.95"` with quoted thousands separator).
- **`/matchings` is new** — replaces the legacy fuzzy-matching UI Schleiper hated. AI
  suggests, human confirms or rejects. Human-confirmed matches survive every re-run of the
  matching pipeline (Tension B invariant — see `TODOS.md` for the LLM eval harness work that
  unlocks auto-confirm).
- **R&P prices may look "fresh" on first export** — the legacy R&P scraper has been silently
  returning zero offers since the site redesign (TODO #6 in `TODOS.md` covers verification).
  Frame as a feature, not a regression, if Schleiper notices.

---

## Security & secrets

Required env vars in production:

- `SECRET_KEY` — Django session/CSRF secret. Setting `DEBUG=False` without `SECRET_KEY`
  raises `ImproperlyConfigured` at startup (no silent dev fallback).
- `ALLOWED_HOSTS` — comma-separated list. Defaults to empty in production.
- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`,
  `POSTGRES_SSLMODE` (default `prefer`).
- `OPENAI_API_KEY` — for embeddings + matching.
- `SLACK_WEBHOOK_URL` — incoming webhook for scrape anomaly alerts (optional but
  recommended).

Migrated Schleiper users from the legacy DB land with `is_staff=False`. Django admin access
is granted manually.

---

## Where to look next

- [`CHANGELOG.md`](./CHANGELOG.md) — what shipped in v0.1.0.0 and why each PLAN.md item
  shifted.
- [`PLAN.md`](./PLAN.md) — original weekend plan, eng review report, locked stack decisions.
- [`TODOS.md`](./TODOS.md) — deferred work, prioritized P0–P4. Read this before deploying
  (H18 Fly.io configs are P0).

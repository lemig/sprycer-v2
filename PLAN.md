# Sprycer v2 — Weekend Rebuild Plan

> Generated 2026-04-25 from `/office-hours` session
> User: Miguel Cabero (miguel.cabero@gmail.com)
> Old repo: `/Users/cabermi/conductor/repos/sprycer/` (Rails + Sidekiq + Cloud 66)
> New repo: `/Users/cabermi/conductor/repos/sprycer-v2/` (Django + Postgres + Fly.io)

## How to use this doc

A fresh Claude Code session in this directory should read this file end-to-end and then say "ready to start H1." Everything you need to begin is here.

---

## Project context

Sprycer is a price-monitoring app for one paying client: **Schleiper**, a Belgian art supplies retailer. Originally a 2012 abandoned startup, repurposed for Schleiper's specific use case shortly after. Schleiper has paid monthly on autopilot for 10+ years.

**What it does today:**
- Scrapes 2 competitor sites (`geant-beaux-arts.be`, `geant-beaux-arts.fr`) for product prices
- Schleiper's employee uploads their product catalog weekly via Excel/CSV
- A manual fuzzy-matching UI links Schleiper products to competitor products (this is the user's most-hated feature)
- Export feature produces one-row-per-product spreadsheet showing Schleiper's price next to competitor prices

**Why rebuild now:**
- Bare metal hosting + Cloud 66 + 2012-era Rails stack. Failures a few times a year. Last failure: 2026-04-24 (Sidekiq workers died silently).
- Manual product matching was hard in 2012; trivial in 2026 with embeddings + LLM.
- Roughly 50% of existing codebase is dead weight from the original 2012 multi-tenant startup vision.
- Miguel has a meeting with Schleiper next week (week of 2026-04-27) about modernizing Schleiper's broader infrastructure — separate, larger deal. Legacy Sprycer reliability has been a credibility drag. Rebuild eliminates that as a concern.

**Constraint: ONE WEEKEND of Claude Code time.** Roughly 20 hours focused, 4 hours reserve. Plan respects this.

---

## Stack decisions (LOCKED)

- **Backend:** Django 5.x (built-in admin, auth, forms, ORM, migrations — boring stuff is free)
- **Database:** Postgres on Neon (free tier covers this scale, branching, no ops)
- **Vector search:** `pgvector` extension on Neon
- **Hosting:** Fly.io (Dockerfile + scheduled machines, ~$5-10/mo)
- **Scraping:** Plain `httpx` — NOT Playwright (see "Scraping discovery" below)
- **AI matching:** OpenAI `text-embedding-3-small` for product embeddings + `gpt-4o-mini` for final yes/no on close pairs. Total cost <$1/month at this scale.
- **Spreadsheets:** `pandas` + `openpyxl` for Excel/CSV
- **UI:** Django templates + HTMX + Tailwind via `django-tailwind`. No SPA, no React, no JS framework.
- **Job scheduling:** Fly.io scheduled machines (NOT Sidekiq, NOT Celery, NOT Redis). Scrapers wake up, run, exit. No persistent worker process to babysit.

---

## Scraping discovery (READ THIS — it's the biggest finding)

Tested 3 sample URLs on 2026-04-25:
1. `https://www.geant-beaux-arts.be/peinture-a-l-huile-extra-fine-puro-maimeri.html`
2. `https://www.geant-beaux-arts.be/peinture-acrylique-darwi-for-you.html`
3. `https://www.geant-beaux-arts.fr/pastel-sec-carre-cretacolor.html`

**Findings:**
- Platform: **Oxid eShop** (German PHP commerce platform)
- HTTP 200 with full HTML (~700KB–860KB) on plain `curl` with generic Mozilla user-agent
- **Zero bot protection.** No Cloudflare challenge, no captcha, no rate limiting on test fetches.
- **Prices and SKUs are server-rendered as schema.org JSON-LD** in `<script type='application/ld+json'>` blocks
- Each page contains a `ProductGroup` with `hasVariant` array of `Product` objects. Each variant has: `sku`, `gtin13`, `weight`, `height`, `width`, `name`, `image`, `url`, plus nested `offers` with `price` + `priceCurrency` + `availability`

**Implication:** No Playwright. No JS execution. No anti-bot service. The scraper is **~30 lines of Python**: `httpx.get` → regex/parse JSON-LD blocks → walk variants → write to DB. Dramatically simpler than the existing CSS-selector-based Rails parser, AND more reliable (schema.org is a stable W3C standard; CSS selectors break on every redesign).

**Note on the legacy parser:** The Rails app uses CSS selectors. Recent commits in `../sprycer/` confirm: "Adjust Legeant variante sku css selector", "Fix assuming LG default page has a single offer", "Update LG parser for name and description". The recurring "site changed, parser broke" problem disappears with JSON-LD parsing.

---

## What to read from `../sprycer/` (the old Rails repo) before writing code

In order:
1. `db/schema.rb` — source of truth for current data model
2. `app/models/*.rb` — every model file
3. `app/parsers/` or similar — search for "legeant" / "LG" to find existing parser
4. `urls.csv` (47KB) — master list of competitor product URLs. **This is the seed for v2's URL queue.**
5. Excel/CSV import service — search `app/services/` and `app/controllers/` for `import` / `upload`
6. Export service — same, search for `export`
7. `config/database.yml` — confirm old DB type for migration script (likely MySQL or Postgres)

**Do NOT** read: views, helpers, JS, controllers (mostly CRUD routing). Model + parser + import/export logic is all that matters for v2.

---

## Hour-by-hour Saturday plan

Target: 20 hours focused work. H21–H24 in reserve. **You will need them.**

### Saturday morning (H1–H5): Foundation
- **H1:** `django-admin startproject sprycer`. Models: `Product`, `CompetitorProduct`, `PriceObservation`, `Match`, `Import`, `ScrapeRun`, `ScrapeQueue`. Map fields from old `db/schema.rb`.
- **H2:** Neon Postgres + `pgvector` setup. `vector(1536)` field on `Product` and `CompetitorProduct`. Initial migration.
- **H3:** Django admin customization (free CRUD UI). Register all models with list displays, search fields, filters.
- **H4:** Import view: upload CSV/Excel → `pandas` parse → write `Product` rows → embed name+description with OpenAI on save.
- **H5:** Export view: select products → query latest `PriceObservation` per matched `CompetitorProduct` → write Excel with `openpyxl`. **Match the old export format EXACTLY** (column order, headers).

### Saturday afternoon (H6–H10): Scrapers
- **H6:** First scraper. `scrape_geant_be(url)`: `httpx.get` → extract `<script type='application/ld+json'>` blocks → parse JSON → walk `hasVariant` → write `CompetitorProduct` + `PriceObservation`. ~30 lines.
- **H7:** Second scraper for `.fr` (likely identical, same Oxid platform — probably a domain-only change).
- **H8:** `ScrapeQueue` model. Seed from old `urls.csv`. Scrapers walk the queue.
- **H9:** Cache raw HTML for every scrape (`media/scrape_cache/{date}/{hash}.html`). Future-proofs debugging.
- **H10:** Schedule scrapers as Fly.io scheduled machines, twice/day. Slack/email webhook on "0 products scraped" anomaly.

### Sunday morning (H11–H16): AI matching (the feature that justifies the rebuild)
- **H11:** Match pipeline scaffold. On `Product.save`, trigger embedding generation. Sync call is fine at this scale — no queue needed.
- **H12:** For each new Product, query top-5 nearest `CompetitorProduct` via pgvector cosine distance.
- **H13:** For each candidate pair, call `gpt-4o-mini`: prompt with name + description + price + image URL, ask YES/NO/UNCERTAIN with reason. Save to `Match` with `confidence` + `llm_reason`.
- **H14:** Auto-accept matches where LLM=YES and confidence > 0.85. Queue UNCERTAIN for manual review. Drop NO. **Initially set auto-accept threshold high — only enable lower thresholds after a few weeks of trust-building.**
- **H15:** Match review UI: HTMX-driven, two-column side-by-side (Schleiper product / candidate), one-click confirm/reject, optimistic update.
- **H16:** Backfill: run pipeline against all imported Products × scraped CompetitorProducts to seed v2's `Match` table.

### Sunday afternoon (H17–H20): Migration, deploy, dogfood
- **H17:** Data migration script. Read old DB, transform, write to Neon. **CRITICAL: preserve historical `PriceObservation` rows.** That's years of data.
- **H18:** Deploy to Fly.io. Custom domain. SSL.
- **H19:** Run a real export from v2. Compare cell-by-cell with same export from legacy app. Numbers must match.
- **H20:** Wire up alerts. Verify scheduled scrapes ran. Victory lap.

### Reserve (H21–H24): YOU WILL NEED THESE

---

## What we're deliberately cutting

- Cloud 66 (replaced by Fly.io)
- Continuous Sidekiq workers (replaced by scheduled jobs that exit cleanly)
- Redis (unnecessary without Sidekiq)
- ~50% of the 2012 multi-tenant code (admin, billing, signup, multi-tenant data scoping)
- The manual-only matching UI (replaced by AI auto-match + manual review for uncertain cases)
- CSS-selector parsing (replaced by JSON-LD parsing)

---

## Risks (in likelihood order)

1. **Data migration loses historical price observations.** Mitigation: keep old DB read-only as cold backup. Decommission only after v2 has a clean month in production.
2. **AI matching misses edge cases the manual UI handled.** Mitigation: do NOT auto-confirm at first. AI suggests, human confirms. Build trust over weeks. Then enable auto-accept gradually.
3. **Django ORM mistakes burn hours.** Mitigation: trust Claude on ORM layer; push back when something feels wrong.
4. **Schleiper notices behavior changes during cutover.** Mitigation: cut over silently. Old export and new export must produce identical numbers for first month. If they don't, there's a bug — find it before declaring done.
5. **Anti-scraping defenses appear between plan date and Saturday.** Mitigation: low probability (no defenses exist 2026-04-25). If they do appear, fallback is ScrapingBee or Scrapfly (~$30/mo).

---

## Bridge runbook (if legacy Sprycer fails before Saturday)

SSH into the Cloud 66 box. Restart Sidekiq workers. Same procedure as 2026-04-24. The permanent fix is hours away on Saturday — don't panic.

---

## Starting the Saturday session

1. `cd /Users/cabermi/conductor/repos/sprycer-v2`
2. Confirm `git init` has been run (it likely has)
3. Open Claude Code in this directory
4. Say: **"Read PLAN.md and start with H1"**

Claude reads this file, then reads from `../sprycer/` (`db/schema.rb`, models, parsers, `urls.csv`, import/export services), then begins implementation.

---

## Open items (decide Saturday morning before H1)

- [ ] OpenAI API key location (env var in Fly.io secrets, `.env` locally — `.env` in `.gitignore`)
- [ ] Old DB type + connection (read `../sprycer/config/database.yml`)
- [ ] Cutover strategy: hard cutover Sunday vs parallel run for 1 week
- [ ] Schleiper-facing URL: keep current domain, point DNS to Fly.io after deploy
- [ ] Whether to involve Schleiper's IT during cutover or do it silently

---

## Tone for the Saturday session

Miguel is a solo developer who shipped this app in 2012, has been maintaining it for 14 years, and is rebuilding because:
1. The 2012 stack is brittle on bare metal
2. AI changes what's possible (auto-matching)
3. He wants to feel confident maintaining this for the next decade

He has a real meeting with the client next week. Don't waste hours. Move fast. When in doubt, pick the simpler option. Ship something working by Sunday night, polish in the following weeks if needed.

The default is to TRUST Miguel's judgment on product behavior (he knows Schleiper's workflows from 14 years of operating this) and lean on Claude for stack mechanics.

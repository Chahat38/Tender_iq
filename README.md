# TenderScout AI

Collects public Pakistani (and international) tender notices, stores
them locally, finds duplicates across sources, classifies them with a
deterministic AI stage (optionally LLM-assisted), scores relevance per
user, raises alerts, and serves everything through a read-only JSON
API.

Three cooperating pieces:

| Piece | What it is |
| --- | --- |
| **Collector** (`collector/`) | Python 3.12 CLI: polite crawlers → SQLite (default) or Postgres → dedup → analysis → matching → alerts |
| **API** (`collector/api.py`) | Read-only FastAPI over the same database (JSON, FTS5 search, no writes) |
| **Frontend** (`frontend/`) | Vite + React + TypeScript app (see `frontend/README.md`) |

## Live demo

* Frontend: <https://tenderiq-frontend-ebon.vercel.app>
* API: <https://tenderiq-api-two.vercel.app> (`/health`, `/docs`)

The current deployment serves the bundled SQLite snapshot (1,284 notices
from 9 sources), so it is a read-only demo rather than a live database.
For live collection, run the collector against Supabase/Postgres and set
`DATABASE_URL` on the Vercel API project.

UNDP is shown on the Sources page as **researched but not collected**:
its procurement portal's `robots.txt` disallows automated access, so the
app does not scrape it. See `DECISIONS.md` for the blocker and the
official-feed/manual-export options.

## Quickstart

```bash
python -m pip install -r collector/requirements.txt

# Offline demo run (fixture HTML, no network, no secrets):
python collector/main.py --offline --adapter kppra_active_tenders
python collector/main.py --offline --adapter epads

# Tests (offline by default; network/Postgres tests are opt-in):
python -m pytest

# Read-only API:
python collector/api.py --port 8000
# → http://127.0.0.1:8000/docs
```

Live collection needs a contact address (crawler User-Agent; the
compliance gate refuses to run without it):

```bash
export CONTACT_EMAIL=you@example.org
python collector/main.py --adapter ppra_epms --enrich-details
```

### Local demo through XAMPP (Windows)

The app is FastAPI + React, so XAMPP's Apache only serves the built
frontend and reverse-proxies API calls; MySQL is not used (the demo reads
SQLite). Build for a subpath, seed the demo database, and start the API:

```powershell
cd frontend
$env:VITE_API_BASE="/ts-api"
npx vite build --base=/tenderscout/
cd ..
New-Item -ItemType Directory -Force var | Out-Null
Copy-Item collector\demo\db.tenderscout var\tenderscout.db
python collector\main.py --db var\tenderscout.db --backfill-taxonomy
python collector\api.py --db var\tenderscout.db --host 127.0.0.1 --port 8017
```

Enable `mod_proxy_http` in `C:\xampp\apache\conf\httpd.conf`, add the
following to `C:\xampp\apache\conf\extra\httpd-vhosts.conf`, then restart
Apache:

```apache
Alias /tenderscout "C:/path/to/Tender_iq/frontend/dist"
<Directory "C:/path/to/Tender_iq/frontend/dist">
    Require all granted
    Options -Indexes
</Directory>
ProxyPreserveHost On
ProxyPass /ts-api/ http://127.0.0.1:8017/
ProxyPassReverse /ts-api/ http://127.0.0.1:8017/
```

Open <http://localhost/tenderscout/>. Keep the trailing slashes on the
`ProxyPass` paths; without them Apache forwards `//health` and FastAPI
returns 404.

## Collector pipeline

Every collection run executes the full chain, each stage printing its
own report line:

```
fetch (compliance-gated) → store → dedup → AI analysis → alerts
```

| Stage | What it does | Standalone mode |
| --- | --- | --- |
| fetch/store | PoliteClient (robots.txt, 2.5 s delay, identifying UA), normalized upserts, change history | `--adapter KEY [--offline] [--enrich-details]` |
| dedup | Exact-reference + fuzzy cross-source duplicate merging with a human review queue | `--dedup-only` |
| documents | Content-addressed download (sha256), PDF/DOCX text extraction | `--documents [--docs-limit N] [--docs-dir DIR] [--docs-retry-failed]` |
| analysis | `opportunity_ai` rows: category, keywords, requirements, date conflicts (LLM optional) | `--analyze-only` |
| matching | `opportunity_matches`: deterministic 0–100 relevance per user | `--match-user ID [--match-limit N]` |
| alerts | `new_match`, `deadline_approaching`, `opportunity_updated`, `corrigendum` (at most once per user/opportunity/type) | `--alerts-only` |

Other modes: `--search QUERY`, `--rebuild-index`,
`--backfill-taxonomy` (fill city/province/sector/department on rows
collected before those fields existed; also runs after every scheduled
collection).

## Sources

| Adapter key | Source | Status |
| --- | --- | --- |
| `ppra_epms` | PPRA e-procurement (Tender/PQ/RFP/EOI) | live |
| `kppra_active_tenders` | KPPRA active tenders | live |
| `epads` | EPADS federal opportunities | live |
| `ajk_ppra` | AJ&K PPRA advertisements | live |
| `gb_ppra` | GB PPRA procurements | live |
| `pakrail` | Pakistan Railways tenders | live |
| `pia` | PIA tenders | live |

Skipped after research (never crawled): UNDP (`procurement-notices.undp.org`
and the `public-components.undp.org` notice widget embedded by
`www.undp.org/pakistan/procurement` both return `Disallow: /`; the main
site has no static notices, so its parser is kept offline-only in
`collector/adapters/undp.py` but deliberately not registered), ADB (bot
wall), NHA (robots 403), Sindh
portal (central e-portal is a 2010-2019 archive, no live listing), Punjab
eProcure (deferred until its TLS layer stops dropping automated
clients; Punjab entities already appear via the federal EPMS), KPT
(robots fetch fails in PoliteClient, so the robots gate never passes;
its tenders are mirrored on EPADS anyway), WAPDA
RSS (endpoint dead).

**Policy:** public pages only, no auth/CAPTCHA bypass, robots.txt is
authoritative, `CONTACT_EMAIL` in the User-Agent, sources whose terms
forbid automation are marked non-active and reported by
`python scripts/compliance_check.py` (exit 1 on any violation; the
collection workflow runs the same gate before crawling).

## Browse filters

The UI filters by city, province, sector and department. Provinces and
cities come from the closed vocabulary in `collector/taxonomy.py` (all
Pakistani districts and major cities), so the dropdowns are complete
even on a fresh database; sectors and departments are data-driven facets
(`/sectors`, `/departments`). The collector derives the fields from text
the source already states (title, organization, description, location)
and never overwrites an adapter-supplied value. City matching also
searches the organization, title and description, because several
portals leave `location` empty and name the city only in the title.

To fill the fields on rows stored before this feature:

```bash
python collector/main.py --backfill-taxonomy
```

## API (read-only)

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | liveness + coarse row counts |
| `GET /sources` | registered sources |
| `GET /opportunities` | newest-first page (filters, capped limit) |
| `POST /opportunities/query` | same, JSON body with tri-state deadline filter |
| `GET /opportunities/{id}` | one opportunity + documents + update history |
| `GET /opportunities/{id}/analysis` | AI analysis of one opportunity |
| `GET /search?q=` | FTS5 full-text search (bm25) |
| `GET /locations` | city facet (`?include_all=true` = complete city list) |
| `GET /provinces` | province facet (`?include_all=true` = canonical 7 provinces) |
| `GET /sectors` | sector facet |
| `GET /departments` | department facet |
| `GET /alerts?user_id=` | user alerts (optional `unread_only`) |
| `GET /matches?user_id=` | per-user relevance matches (`min_score`) |

Hardening: per-IP rate limit (`TENDERSCOUT_RATE_LIMIT`, default
300/min, `0` disables; over-limit → 429 + `Retry-After`), security
headers (`nosniff`, `X-Frame-Options`, `Referrer-Policy`), capped page
sizes, no internal columns (`raw_content`, `storage_path`, dedup
columns) in any response. Errors: 404 unknown ids, 422 bad params,
503 without FTS5 (search only), 500 without SQL/path leaks.

## Storage

* **SQLite** (default) — collector + API on one box; FTS5 search.
* **Postgres/Supabase** — set `DATABASE_URL`; the migrations under
  `supabase/migrations/` define the contract (tables, indexes, RLS).
  Apply them in filename order: `001_init.sql` is the initial schema
  (already includes `sector`/`department`), and `002`/`003` are
  additive, idempotent updates for databases created earlier.
  `python scripts/migrate_sqlite_to_postgres.py` copies a SQLite file
  over and applies every migration; `python scripts/validate_sql.py`
  parses them with PostgreSQL's own grammar (pglast) before CI ever
  ships them.

The table/field names are a fixed contract with the frontend and
Supabase — never renamed, only added to. See `DECISIONS.md`.

## Environment

All variables are documented in `.env.example`. Required for live
crawling: `CONTACT_EMAIL`. Optional: `DATABASE_URL`,
`TENDERSCOUT_DB_PATH`, `TENDERSCOUT_DOCUMENTS_DIR`, `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, `TENDERSCOUT_LLM_MODEL`, `SLACK_WEBHOOK_URL`,
`TENDERSCOUT_RATE_LIMIT`, `TENDERSCOUT_CORS_ORIGINS` (comma-separated
browser origins, default `*`), `TEST_DATABASE_URL` (Postgres tests only).

## Testing & CI

```bash
python -m pytest                 # offline suite (default; what CI runs)
python -m pytest -o addopts="-ra" -m network   # live source smoke tests
python -m pytest -m postgres     # RLS tests (needs TEST_DATABASE_URL)
```

The `network` suite drives the real pipeline (compliance gate →
polite listing pass → bookkeeping) once per registered source, so
`-o addopts` is needed to override the default exclusion in
`pytest.ini`.

* `.github/workflows/ci.yml` — lint (ruff), byte-compile, validate
  migration SQL, offline suite; a second job runs the `postgres`
  RLS/storage tests against a disposable Postgres 16 service
  container (no `TEST_DATABASE_URL` needed in CI).
* `.github/workflows/collect.yml` — scheduled per-source matrix:
  compliance gate → collect (`--enrich-details`) → document fetch →
  log/database artifacts; alerts via `SLACK_WEBHOOK_URL`.

## Deploy to Vercel

The demo runs as two Vercel projects: a static React frontend and the
FastAPI/collector as a Python serverless function (`api/index.py`,
`vercel.json`, `requirements.txt` at the repo root). With `DATABASE_URL`
set the API serves straight from Supabase/Postgres — `GET /search` is
then served by the ILIKE/GIN fallback in `PostgresStorage` instead of
SQLite FTS5 (`api.py` `/search` branch).

1. **Supabase** — create a project, open the SQL editor, and run the
   files in `supabase/migrations/` in filename order (`001_init.sql` is
   the full initial schema; `002`/`003` add fields to databases created
   before them and are safe to re-run). Grab the project's connection
   string from Settings → Database (session pooler works fine for a
   read-mostly demo).
2. **API project** — at the repo root: `vercel login`, then
   `vercel` (create project `tenderiq-api`, framework "Other"). Set the
   environment variable `DATABASE_URL=<connection string>` and re-deploy.
   The function is `api/index.py` (ASGI `app`); the current production
   alias is <https://tenderiq-api-two.vercel.app>, with routes such as
   `/opportunities`, `/opportunities/{id}`, `/search`, …
3. **Frontend project** — build with the API base set, then deploy from
   `frontend/`:
   ```bash
   cd frontend && npm install
   # cross-env or PowerShell: $env:VITE_API_BASE="https://tenderiq-api-two.vercel.app"
   VITE_API_BASE=https://tenderiq-api-two.vercel.app npm run build
   vercel                # create project tenderiq-frontend (framework: Vite)
   ```
   The app calls `${VITE_API_BASE}` for every endpoint; without it the
   built app assumes a same-origin `/api` proxy. The API allows the
   static frontend origin through CORS by default (`*`); set
   `TENDERSCOUT_CORS_ORIGINS` to a comma-separated allow-list to narrow
   it.

Optional hardening: a Supabase/RLS read-only role keyed to the frontend
doesn't fit the API-first design here, so keep `DATABASE_URL` server-side
secret and serve the UI as a plain static site.

## Layout

```
collector/            Python package (CLI entry: main.py / api.py)
  adapters/           source adapters (ppra_epms, kppra, epads, ajk_ppra, gb_ppra, pakrail, pia)
  fixtures/           saved HTML for offline runs
  tests/              pytest suite (offline default)
  documents.py        document download/hash/extraction
  dedup.py            cross-source duplicate detection
  ai.py               analysis + relevance matching
  alerts.py           user-facing alert worker
  compliance.py       crawl gate, ops.py operational alerting
scripts/              migration, SQL validation, fixtures, compliance check
supabase/migrations/  Postgres contract + RLS
frontend/             React app (see its README)
DECISIONS.md          architecture decisions and their trade-offs
```

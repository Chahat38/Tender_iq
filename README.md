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

Other modes: `--search QUERY`, `--rebuild-index`.

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

Skipped after research (never crawled): UNDP Pakistan (`Disallow: /`),
ADB (bot wall), NHA (robots 403), Sindh portal (central e-portal is a
2010-2019 archive, no live listing), Punjab
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
* **Postgres/Supabase** — set `DATABASE_URL`; the migration
  `supabase/migrations/001_init.sql` defines the contract (tables,
  indexes, RLS). `python scripts/migrate_sqlite_to_postgres.py` copies
  a SQLite file over; `python scripts/validate_sql.py` parses both SQL
  files with PostgreSQL's own grammar (pglast) before CI ever ships
  them.

The table/field names are a fixed contract with the frontend and
Supabase — never renamed, only added to. See `DECISIONS.md`.

## Environment

All variables are documented in `.env.example`. Required for live
crawling: `CONTACT_EMAIL`. Optional: `DATABASE_URL`,
`TENDERSCOUT_DB_PATH`, `TENDERSCOUT_DOCUMENTS_DIR`, `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, `TENDERSCOUT_LLM_MODEL`, `SLACK_WEBHOOK_URL`,
`TENDERSCOUT_RATE_LIMIT`, `TEST_DATABASE_URL` (Postgres tests only).

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

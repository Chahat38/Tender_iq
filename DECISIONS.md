# Architecture decisions

Running log of decisions that a future reader would otherwise have to
reverse-engineer. Format: decision, why, consequence.

## Storage: SQLite locally, Postgres in production

The collector writes through one `Storage` interface with two
implementations (`storage.py`, `storage_postgres.py`) sharing all
rules in `storage_common.py` (change detection, timestamps, document
helpers), so both backends behave identically by construction.

*Why:* tests and single-box installs must work with zero services;
Supabase gives the frontend RLS and auth. *Consequence:* the read-only
API is SQLite-backed (FTS5 search lives there); production Supabase
reads come from Postgres written by the collector. Live `postgres`/
RLS tests need `TEST_DATABASE_URL` and are deselected by default -
they were structurally validated (pglast parses the migration) in an
environment without Docker.

## The schema is a fixed contract

Table and column names in `supabase/migrations/001_init.sql` are
consumed by the frontend, the API and the migration script - never
renamed, only added to. Three tables exist beyond the original
contract and are intentional:

| Table | Why it exists |
| --- | --- |
| `dedup_review_queue` | uncertain duplicate pairs park for human review |
| `operational_alerts` | collector health alerts (per source), distinct from per-user `alerts` |
| `audit_log` | append-only activity trail |

SQLite keeps the internal name `sources` (vs Postgres
`procurement_sources`) to avoid churning existing test fixtures.

## Deterministic-first AI

`opportunity_ai` (category/keywords/requirements/date conflicts) is
produced by rules - taxonomy keyword scoring, token extraction,
sentence matching - and can be upgraded per-row by an LLM when
`OPENAI_API_KEY` is set: structured JSON, `temperature=0`, every call
journaled in `analysis_runs` (task/provider/model/tokens/duration/
status), any failure falls back to the rules with `status="error"`.

*Why:* scores shown to users must be reproducible and explainable
offline; LLMs are best kept where they add nuance (classification
text), never as the only path. *Consequence:* `opportunity_matches`
scores are always `deterministic-v1` with human-readable `reasons`;
tests run without keys or network.

## Deduplication: flag, never delete

Duplicates across sources are merged by stamping `duplicate_group_id`,
`is_primary` (earliest row wins) and `source_count` (distinct sources);
no row is ever removed. Exact reference-number matches (normalized,
deadlines compatible within 30 days) auto-merge at score 1.0; fuzzy
title/organization/deadline scoring auto-merges ≥ 0.85 and queues
0.60–0.85 into `dedup_review_queue`. Pairs are only formed
cross-source and through shared buckets (reference/organization/
deadline ±3 d), so the cross-product stays bounded.

*Why:* sources sometimes disagree about details; keeping both rows
preserves provenance and lets a human undo (`resolve(confirmed|
rejected)`). *Consequence:* search/API see grouped rows - consumers
filter `is_primary` when they want one row per tender.

## Alerts: at most one per (user, opportunity, type)

The schema's `UNIQUE (user_id, opportunity_id, type)` defines the
de-duplication rule: each alert type fires once per user/opportunity,
ever. The worker only inserts where no such row exists. `in_app` is
the only channel for now; other channels attach at the API/
notification layer. Evidence rules: `new_match` needs score ≥ the
profile's `alert_threshold`; deadline alerts need a watched
opportunity (matched or saved) closing within 3 days; `corrigendum`
means the document list changed, `opportunity_updated` means any other
field changed.

## Document fetching: one attempt, explicit retry

`storage_path IS NULL AND fetched_at IS NOT NULL` means "attempted,
failed" - visible in the database, never auto-retried (a 404 stays a
404). `--docs-retry-failed` re-queues. Files are content-addressed
(`sha256[0:2]/sha256`, identical bytes shared), capped at 25 MB,
stored outside any web root; extracted text (pypdf/python-docx, first
200 k chars) lands in `opportunity_documents.extracted_text`. OCR for
scanned PDFs is deliberately deferred (empty extraction is honest).

Documents are fetched by the `--documents` mode and by the scheduled
collect workflow, not automatically after every local collection -
downloading is the polite-crawl budget's most expensive part and must
stay an explicit decision.

## Compliance gate owns crawl decisions

`collector/registry.py` is the single registry of sources; each run it
overwrites the compliance columns (`active`, `terms_checked`,
`requires_authentication`, …) from code, never the runtime columns
(`last_crawled_at`, `last_status`, `consecutive_failures`,
`robots_allowed`). A source runs only if `active AND terms_checked AND
NOT requires_authentication AND robots_allowed is not False`;
violations raise `ComplianceError` → exit code 3, and
`scripts/compliance_check.py` reports the same as exit 1 in CI before
anything is crawled. Unknown robots verdicts (`None`, e.g. no
robots.txt) are allowed; explicit `Disallow` is not.

`PoliteClient.robots_allowed()` distinguishes "disallowed" (False)
from "unverifiable" (None) precisely so the source row can record
which one happened.

## Operational alerting: streak of 3

A source is "broken" after 3 consecutive failed runs
(`FAILURE_THRESHOLD` in `ops.py`) - transient 5xx/timeouts shouldn't
page anyone.
One `operational_alerts` row per open incident (no duplicates while
unacknowledged), notifier = log line + optional `SLACK_WEBHOOK_URL`
(no notifier can ever fail a collection run). Success resets the
counter but does not close the alert; humans acknowledge.

## Rate limiting in the API process

A fixed 60 s per-IP window in middleware (`TENDERSCOUT_RATE_LIMIT`,
default 300, 0 disables) instead of an external limiter: the API is
meant to run on a single box without infrastructure, and 429 +
`Retry-After` is enough to keep scrapers off a read-only endpoint.

## Sources researched but not built

| Source | Verdict | Reason |
| --- | --- | --- |
| UNDP Pakistan | skip | `robots.txt` `Disallow: /` |
| Asian Development Bank | skip | 403 bot wall (bypassing it = against terms) |
| NHA | skip | robots.txt returns 403 (`RobotsUnavailable`) |
| Sindh portal | skip | central `e.pprasindh.gov.pk` listing is a 2010-2019 archive (no live lists); HTTP-only, no separate live feed found |
| Punjab eProcure | deferred | ASP.NET ViewState postback scraping not worth the fragility yet |
| WAPDA RSS | dropped | `rssfile.xml?catid=3` returns 404 - the idea died with the endpoint |

Added 2026-10-07, researched and registered as live sources: AJ&K PPRA
(`www.ajkppra.gov.pk/advertisements.php`, robots.txt 404, server-rendered
table, notice PDFs) and GB PPRA (`www.gbppra.gov.pk/viewall`, robots.txt
allow-all, server-rendered cards, `?page=N` pagination). Both are
listing-only for now (no detail enrichment), like EPADS.

Same day, added Pakistan Railways (`www.pakrail.gov.pk/Tenders/index.aspx`,
robots.txt 404, server-rendered table, `?pno=N` pagination of 50, newest
first; identifies by the `TenderDetails.aspx?ID=` number as `PRL-*`).
Also researched but deferred: Punjab PPRA active-procurement portal
(`eproc.punjab.gov.pk/activetenders.aspx`) - the content is a plain
server-rendered, actively updated grid, but its TLS layer drops
automated clients (Python `requests` and native `curl` both fail with
SSLError / SEC_E_INVALID_TOKEN), so reliable CI collection is not
possible today; Punjab entities remain covered through the federal
PPRA EPMS. No public Balochistan PRA tender-listing portal was found;
Balochistan entities publish through the federal EPMS/EPADS.

Also added PIA (`piac.com.pk/corporate/business-with-pia/tenders`,
stock Joomla robots; single server-rendered table of current tenders,
no working pagination, id from the `tenders/{id}` view link). KPT
(`kpt.gov.pk/tender/show`) looked good (server-rendered, robots 404)
but PoliteClient cannot even fetch its robots.txt, so the robots gate
never passes and it is not registered - its tenders are mirrored on
EPADS anyway. Standalone portals checked during the nationwide sweep
and found to funnel through the already-covered PPRA EPMS/EPADS:
NLC, ANF, PNSC, Gwadar Port Authority, PSO, PAA, NBP, KPT internal.

## Quality tooling

CI runs three gates (`ci.yml`): `ruff check collector scripts`
(configured in `ruff.toml` - lint only, no reformatting, E4/E7/E9/F/I/B
with `combine-as-imports`; `E402` allowed where modules bootstrap
`sys.path`, `B017` allowed in the RLS tests because any psycopg error
proves the policy fired), byte-compilation, pglast SQL validation, and
the offline pytest suite. Live behavior is covered by the scheduled
`collect.yml` matrix plus the opt-in `network` suite
(`tests/test_live_sources.py`: one polite pipeline run per registered
source against the live sites - run with
`pytest -o addopts="-ra" -m network`, never from CI).

## Frontend

The React app (`frontend/`) is a separate build; this round of work
deliberately changed no table/field names it depends on, so existing
frontend code keeps working against the API.

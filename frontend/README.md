# TenderScout AI — frontend (Stage 8)

React + TypeScript + Vite + Tailwind single-page app over the read-only
FastAPI API from Stage 6. Browse, filter by source, full-text search and
inspect collected tenders (documents + update history). The UI only
calls `GET` endpoints — it can never write to the database.

## Run it

1. Put data in the database (offline demo from saved fixtures, ~100 rows):

   ```
   python collector/main.py --offline --enrich-details --db collector/tenderscout.db
   python collector/main.py --rebuild-index --db collector/tenderscout.db
   ```

   For real data, drop `--offline` (a polite crawl of PPRA EPMS; the
   bot honors the same rate limits as the collector).

2. Start the read-only API (terminal 1):

   ```
   python collector/api.py --port 8017 --db collector/tenderscout.db
   ```

3. Start the frontend dev server (terminal 2):

   ```
   cd frontend
   npm install
   npm run dev
   ```

   Open http://localhost:5173 — the Vite dev server proxies same-origin
   `/api/*` to `http://127.0.0.1:8017`, so no CORS setup is needed.

## Tests

```
npm test          # vitest unit tests (offline; fetch/API mocked)
npm run build     # tsc typecheck + production build into dist/
```

## Configuration

- `VITE_API_BASE` — API base for the built app (default `/api`).
- `TENDERSCOUT_API` — proxy target for the dev server
  (default `http://127.0.0.1:8017`).

## Notes

- Search uses the same strict FTS5 translation as the CLI (`GET
  /search`); a server whose SQLite build lacks FTS5 shows a friendly
  banner instead of results (HTTP 503 is handled explicitly).
- The source filter applies to browse mode only (`GET /search` has no
  source parameter on the API).
- All scraped text is rendered as plain text (React escaping); no
  `dangerouslySetInnerHTML` anywhere.

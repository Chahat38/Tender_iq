import { useEffect, useState, type ChangeEvent } from "react";
import {
  ApiError,
  listOpportunities,
  listSources,
  searchOpportunities,
} from "./api";
import type { Opportunity, SearchHit, Source } from "./types";
import { OpportunityDetail } from "./components/OpportunityDetail";
import { OpportunityTable, type TableRow } from "./components/OpportunityTable";
import { Pagination } from "./components/Pagination";
import { SearchBar } from "./components/SearchBar";

const PAGE_SIZE = 20;

function toRow(opportunity: Opportunity): TableRow {
  return {
    id: opportunity.id,
    title: opportunity.title,
    organization: opportunity.organization,
    referenceNumber: opportunity.reference_number,
    deadline: opportunity.deadline,
    externalId: opportunity.external_id,
    snippet: null,
  };
}

function hitToRow(hit: SearchHit): TableRow {
  return {
    id: hit.opportunity_id,
    title: hit.title,
    organization: hit.organization,
    referenceNumber: hit.reference_number,
    deadline: hit.deadline,
    externalId: hit.external_id,
    snippet: hit.snippet,
  };
}

/**
 * Single-page read-only UI over the TenderScout API: browse the
 * newest-first list, filter by source, run FTS5 searches and open a
 * detail view. The visible mode is derived from state: a non-empty
 * search query switches the list to search results, and a selected id
 * switches to the detail view (Back restores the exact previous list).
 */
export default function App() {
  const [sources, setSources] = useState<Source[]>([]);
  const [sourceId, setSourceId] = useState<number | null>(null);
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [rows, setRows] = useState<TableRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // The source filter is optional chrome: if /sources fails, browsing
  // still works (the main data effect surfaces real API errors).
  useEffect(() => {
    const controller = new AbortController();
    listSources(controller.signal)
      .then(setSources)
      .catch(() => setSources([]));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (selectedId !== null) {
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    const params = { limit: PAGE_SIZE, offset };
    const load = searchQuery
      ? searchOpportunities(searchQuery, params, controller.signal).then(
          (page) => {
            setRows(page.results.map(hitToRow));
            setTotal(page.total);
          },
        )
      : listOpportunities(
          { ...params, sourceId: sourceId ?? undefined },
          controller.signal,
        ).then((page) => {
          setRows(page.opportunities.map(toRow));
          setTotal(page.total);
        });
    load
      .catch((err: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setRows([]);
        setTotal(0);
        if (err instanceof ApiError && err.status === 503) {
          setError(
            "Full-text search is unavailable on this server (SQLite build without FTS5).",
          );
        } else if (err instanceof ApiError) {
          setError(`API error ${err.status}: ${err.detail}`);
        } else {
          setError(String(err));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [searchQuery, offset, sourceId, selectedId]);

  function handleSubmitSearch(query: string) {
    setSearchInput(query);
    setSearchQuery(query);
    setOffset(0);
    setSelectedId(null);
  }

  function handleClearSearch() {
    setSearchInput("");
    setSearchQuery("");
    setOffset(0);
  }

  function handleSourceChange(event: ChangeEvent<HTMLSelectElement>) {
    const value = event.target.value;
    setSourceId(value === "" ? null : Number(value));
    setOffset(0);
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 px-6 py-5">
        <div className="mx-auto max-w-6xl">
          <div className="flex items-baseline gap-3">
            <h1 className="text-2xl font-bold tracking-tight">
              TenderScout AI
            </h1>
            <span className="rounded-full border border-slate-700 px-2 py-0.5 text-xs text-slate-400">
              read-only
            </span>
          </div>
          <p className="mt-1 text-sm text-slate-400">
            Verified public procurement opportunities in Pakistan.
          </p>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-6">
        {selectedId !== null ? (
          <OpportunityDetail
            id={selectedId}
            onBack={() => setSelectedId(null)}
          />
        ) : (
          <>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
              <SearchBar
                value={searchInput}
                active={searchQuery !== ""}
                onValueChange={setSearchInput}
                onSubmit={handleSubmitSearch}
                onClear={handleClearSearch}
              />
              <select
                value={sourceId ?? ""}
                onChange={handleSourceChange}
                disabled={searchQuery !== ""}
                aria-label="Filter by source"
                title="Source filter applies to browse mode only"
                className="rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <option value="">All sources</option>
                {sources.map((source) => (
                  <option key={source.id} value={source.id}>
                    {source.name}
                  </option>
                ))}
              </select>
            </div>
            {searchQuery ? (
              <p className="mt-4 text-sm text-slate-400">
                Search results for “{searchQuery}”
              </p>
            ) : null}
            {error !== null ? (
              <div
                role="alert"
                className="mt-4 rounded-lg border border-rose-500/40 bg-rose-500/10 p-4 text-sm text-rose-200"
              >
                {error}
              </div>
            ) : (
              <div className="mt-4">
                <OpportunityTable
                  rows={rows}
                  loading={loading}
                  onSelect={setSelectedId}
                />
                <Pagination
                  offset={offset}
                  limit={PAGE_SIZE}
                  total={total}
                  onPage={setOffset}
                />
              </div>
            )}
          </>
        )}
      </main>
      <footer className="border-t border-slate-800 px-6 py-4 text-center text-xs text-slate-600">
        Data from public PPRA EPMS pages · TenderScoutBot/0.1 · read-only API
      </footer>
    </div>
  );
}

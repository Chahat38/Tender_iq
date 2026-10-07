import { useEffect, useState } from "react";
import {
  ApiError,
  listCategories,
  listLocations,
  listOpportunities,
  listProcurementTypes,
  listProvinces,
  listSources,
  searchOpportunities,
} from "./api";
import type { Opportunity, SearchHit, Source } from "./types";
import { OpportunityDetail } from "./components/OpportunityDetail";
import { OpportunityTable, type TableRow } from "./components/OpportunityTable";
import { Pagination } from "./components/Pagination";
import { SearchBar } from "./components/SearchBar";
import { FilterBar, type FilterValues } from "./components/FilterBar";
import { EMPTY_FILTERS } from "./components/FilterBar";
import { NavBar, type PageName } from "./components/NavBar";
import { NotificationsPage } from "./pages/NotificationsPage";
import { SourcesPage } from "./pages/SourcesPage";

const PAGE_SIZE = 20;

type Route =
  | { page: "tenders" }
  | { page: "detail"; id: number }
  | { page: "notifications" }
  | { page: "sources" };

/** Hash-based routing: #/tenders, #/tender/:id, #/notifications, #/sources. */
function parseHash(hash: string): Route {
  const path = hash.replace(/^#/, "") || "/";
  if (path === "/tenders" || path === "/") {
    return { page: "tenders" };
  }
  const detailMatch = path.match(/^\/tender\/(\d+)$/);
  if (detailMatch) {
    return { page: "detail", id: Number(detailMatch[1]) };
  }
  if (path.startsWith("/notifications")) {
    return { page: "notifications" };
  }
  if (path.startsWith("/sources")) {
    return { page: "sources" };
  }
  return { page: "tenders" };
}

function pageName(route: Route): PageName {
  if (route.page === "notifications") {
    return "notifications";
  }
  if (route.page === "sources") {
    return "sources";
  }
  return "home";
}

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
 * Multi-page read-only UI over the TenderScout API. Hash routes keep the
 * tenders list, its filters and pagination alive while the user peeks at a
 * detail page, the notification feeds or the sources list and then comes
 * back. A non-empty search query switches the list to FTS5 results
 * (filters pause; the /search endpoint has no filter parameters yet).
 */
export default function App() {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  const [sources, setSources] = useState<Source[]>([]);
  const [locations, setLocations] = useState<string[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [provinces, setProvinces] = useState<string[]>([]);
  const [procurementTypes, setProcurementTypes] = useState<string[]>([]);
  const [filters, setFilters] = useState<FilterValues>(EMPTY_FILTERS);
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [rows, setRows] = useState<TableRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onHashChange = () => {
      setRoute(parseHash(window.location.hash));
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  function navigate(hash: string) {
    if (window.location.hash === hash) {
      window.scrollTo(0, 0);
      return;
    }
    window.location.hash = hash;
  }

  function openDetail(id: number) {
    navigate(`#/tender/${id}`);
  }

  // Facets are optional chrome: if a facet endpoint fails, browsing still
  // works (the main data effect surfaces real API errors).
  useEffect(() => {
    const controller = new AbortController();
    listSources(controller.signal)
      .then(setSources)
      .catch(() => setSources([]));
    listLocations(controller.signal)
      .then(setLocations)
      .catch(() => setLocations([]));
    listCategories(controller.signal)
      .then(setCategories)
      .catch(() => setCategories([]));
    listProvinces(controller.signal)
      .then(setProvinces)
      .catch(() => setProvinces([]));
    listProcurementTypes(controller.signal)
      .then(setProcurementTypes)
      .catch(() => setProcurementTypes([]));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (route.page !== "tenders") {
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
          {
            ...params,
            sourceId: filters.sourceId ?? undefined,
            location: filters.location || undefined,
            category: filters.category || undefined,
            organization: filters.organization || undefined,
            province: filters.province || undefined,
            procurementType: filters.procurementType || undefined,
            sort: filters.sort,
            includeDuplicates: filters.includeDuplicates || undefined,
            deadlineFrom: filters.deadlineFrom || undefined,
            deadlineTo: filters.deadlineTo || undefined,
          },
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
  }, [route.page, searchQuery, offset, filters]);

  function handleFiltersChange(next: FilterValues) {
    setFilters(next);
    setOffset(0);
  }

  function handleSubmitSearch(query: string) {
    setSearchInput(query);
    setSearchQuery(query);
    setOffset(0);
  }

  function handleClearSearch() {
    setSearchInput("");
    setSearchQuery("");
    setOffset(0);
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <NavBar page={pageName(route)} onNavigate={navigate} />
      <main className="mx-auto max-w-6xl px-6 py-6">
        {route.page === "detail" ? (
          <OpportunityDetail
            id={route.id}
            onBack={() => navigate("#/tenders")}
          />
        ) : route.page === "notifications" ? (
          <NotificationsPage onSelectTender={openDetail} />
        ) : route.page === "sources" ? (
          <SourcesPage />
        ) : (
          <>
            <section className="mb-5 rounded-xl border border-slate-800 bg-gradient-to-br from-slate-900 via-slate-900 to-slate-950 p-6">
              <h1 className="text-2xl font-bold tracking-tight text-slate-100">
                Public tenders, one clear list
              </h1>
              <p className="mt-2 max-w-2xl text-sm leading-relaxed text-slate-400">
                TenderScout AI pulls verified public procurement notices from
                Pakistan&apos;s official portals, collapses duplicates onto one
                record, and watches deadlines for you. Search, filter, and
                draft bid documents from a tender&apos;s detail page.
              </p>
              <div className="mt-4 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => navigate("#/notifications")}
                  className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500"
                >
                  View notifications
                </button>
                <button
                  type="button"
                  onClick={() => navigate("#/sources")}
                  className="rounded-md border border-slate-700 px-4 py-2 text-sm text-slate-300 hover:bg-slate-800"
                >
                  See sources
                </button>
              </div>
            </section>
            <SearchBar
              value={searchInput}
              active={searchQuery !== ""}
              onValueChange={setSearchInput}
              onSubmit={handleSubmitSearch}
              onClear={handleClearSearch}
            />
            <div className="mt-3">
              <FilterBar
                values={filters}
                sources={sources}
                locations={locations}
                categories={categories}
                provinces={provinces}
                procurementTypes={procurementTypes}
                disabled={searchQuery !== ""}
                onChange={handleFiltersChange}
              />
            </div>
            {searchQuery ? (
              <p className="mt-4 text-sm text-slate-400">
                Search results for “{searchQuery}” · filters are paused in
                search mode
              </p>
            ) : null}
            {!filters.includeDuplicates && !error ? (
              <p className="mt-4 text-sm text-slate-500">
                Duplicates are collapsed — tick “Show mirror listings” to see
                every portal&apos;s copy.
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
                  onSelect={openDetail}
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
        Data from public PPRA EPMS, KPPRA, EPADS, AJK PPRA, GB PPRA, Pakistan
        Railways, PIA, SPPRA Sindh and Punjab eProcurement pages ·
        TenderScoutBot/0.1 · read-only API
      </footer>
    </div>
  );
}
/**
 * Typed read-only client for the TenderScout API (Stage 6/9 endpoints).
 *
 * Every function only performs GET requests; there are no write paths
 * in the UI, matching the API's read-only contract.
 */

import type {
  AiAnalysis,
  AiDedupResult,
  AiRfpDraft,
  DuplicateLink,
  NotificationsPage,
  OpportunityDetail,
  OpportunityPage,
  SearchPage,
  Source,
  SourceCoverage,
} from "./types";

/**
 * Base URL of the API. Under the Vite dev server this is same-origin
 * "/api", which vite.config.ts proxies to the FastAPI process.
 * Override with VITE_API_BASE when the built app is served elsewhere.
 */
const API_BASE: string = import.meta.env.VITE_API_BASE ?? "/api";

/** Error raised for any non-2xx API response. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`API error ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/**
 * True when a request was cancelled (the effect unmounted or its inputs
 * changed) rather than failing. Cancellations are routine in React's dev
 * double-mount and on every filter change, so callers must not surface
 * them as API errors.
 */
export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

async function requestJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new ApiError(response.status, await extractDetail(response));
  }
  return (await response.json()) as T;
}

async function requestJsonPost<T>(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    throw new ApiError(response.status, await extractDetail(response));
  }
  return (await response.json()) as T;
}

/** Best-effort extraction of FastAPI's {"detail": ...} error body. */
async function extractDetail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body !== null && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string") {
        return detail;
      }
    }
    return JSON.stringify(body);
  } catch {
    return response.statusText || `HTTP ${response.status}`;
  }
}

function queryString(
  params: Record<string, string | number | boolean | undefined>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") {
      search.set(key, String(value));
    }
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

/** Tender source family shown in the Source filter. */
export type SourceKind = "government" | "corporate";

export interface ListParams {
  limit?: number;
  offset?: number;
  /** Restrict to one registered source (browse mode only). */
  sourceId?: number;
  /** Restrict to source kinds: government portals and/or corporate
   * (state-owned enterprises publishing their own tenders). */
  sourceKinds?: SourceKind[];
  /** Keep rows whose location contains this text (city filter). */
  location?: string;
  /** Exact stored category, case-insensitive (facet value). */
  category?: string;
  /** Exact stored province, case-insensitive (facet value). */
  province?: string;
  /** Exact stored sector, case-insensitive (facet value). */
  sector?: string;
  /** Exact stored department, case-insensitive (facet value). */
  department?: string;
  /** Exact stored procurement type, case-insensitive (facet value). */
  procurementType?: string;
  /** newest = insertion order; closing = soonest deadline first. */
  sort?: "newest" | "closing";
  /** Show every duplicate listing instead of one row per notice. */
  includeDuplicates?: boolean;
  /** Only deadlines strictly before this ISO datetime. */
  deadlineBefore?: string;
  /** Only deadlines strictly after this ISO datetime. */
  deadlineAfter?: string;
  /** Inclusive lower bound on the deadline's date (YYYY-MM-DD). */
  deadlineFrom?: string;
  /** Inclusive upper bound on the deadline's date (YYYY-MM-DD). */
  deadlineTo?: string;
}

/** Newest-first page of opportunities. */
export function listOpportunities(
  params: ListParams = {},
  signal?: AbortSignal,
): Promise<OpportunityPage> {
  const query = queryString({
      limit: params.limit,
      offset: params.offset,
      source_id: params.sourceId,
      location: params.location,
      category: params.category,
      province: params.province,
      sector: params.sector,
      department: params.department,
      procurement_type: params.procurementType,
      sort: params.sort,
      include_duplicates: params.includeDuplicates,
      deadline_before: params.deadlineBefore,
      deadline_after: params.deadlineAfter,
      deadline_from: params.deadlineFrom,
      deadline_to: params.deadlineTo,
    });
  // Repeated params: source_kind=government&source_kind=corporate.
  const kinds = (params.sourceKinds ?? [])
    .map((kind) => `source_kind=${encodeURIComponent(kind)}`)
    .join("&");
  const search = [query.replace(/^\?/, ""), kinds].filter(Boolean).join("&");
  return requestJson<OpportunityPage>(
    `/opportunities${search ? `?${search}` : ""}`,
    signal,
  );
}

/** One opportunity with its documents and update history. */
export function getOpportunity(
  id: number,
  signal?: AbortSignal,
): Promise<OpportunityDetail> {
  return requestJson<OpportunityDetail>(`/opportunities/${id}`, signal);
}

/** Other sources carrying the same notice (siblings of one duplicate group). */
export function getDuplicates(
  id: number,
  signal?: AbortSignal,
): Promise<DuplicateLink[]> {
  return requestJson<DuplicateLink[]>(`/opportunities/${id}/duplicates`, signal);
}

/** FTS5 full-text search (bm25-ranked; lower rank = better). */
export function searchOpportunities(
  query: string,
  params: Pick<
    ListParams,
    "limit" | "offset" | "includeDuplicates"
  > = {},
  signal?: AbortSignal,
): Promise<SearchPage> {
  return requestJson<SearchPage>(
    `/search${queryString({
      q: query,
      limit: params.limit,
      offset: params.offset,
      include_duplicates: params.includeDuplicates,
    })}`,
    signal,
  );
}

/** "New tenders" and "closing soon" buckets for the notifications UI. */
export function getNotifications(
  windowDays: number,
  limit = 50,
  signal?: AbortSignal,
): Promise<NotificationsPage> {
  return requestJson<NotificationsPage>(
    `/notifications${queryString({ window_days: windowDays, limit })}`,
    signal,
  );
}

/** All registered procurement sources. */
export function listSources(signal?: AbortSignal): Promise<Source[]> {
  return requestJson<Source[]>("/sources", signal);
}

/** Requested-source coverage, including blocked/login-only entries. */
export function listCoverage(signal?: AbortSignal): Promise<SourceCoverage[]> {
  return requestJson<SourceCoverage[]>("/coverage", signal);
}

/**
 * City filter facet: distinct stored locations merged with the canonical
 * Pakistani city list, so every city is selectable even before a source
 * has supplied a location string.
 */
export function listLocations(
  province?: string,
  signal?: AbortSignal,
): Promise<string[]> {
  return requestJson<string[]>(
    `/locations${queryString({ include_all: true, province })}`,
    signal,
  );
}

/** Distinct stored categories (category filter facet). */
export function listCategories(signal?: AbortSignal): Promise<string[]> {
  return requestJson<string[]>("/categories", signal);
}

/**
 * Province filter facet: the seven Pakistani provinces (canonical
 * spelling) plus any stored values, always complete.
 */
export function listProvinces(signal?: AbortSignal): Promise<string[]> {
  return requestJson<string[]>("/provinces?include_all=true", signal);
}

/** Distinct stored sectors (sector filter facet). */
export function listSectors(signal?: AbortSignal): Promise<string[]> {
  return requestJson<string[]>("/sectors", signal);
}

/** Distinct stored departments (department filter facet). */
export function listDepartments(signal?: AbortSignal): Promise<string[]> {
  return requestJson<string[]>("/departments", signal);
}

/** Distinct stored procurement types (procurement_type filter facet). */
export function listProcurementTypes(signal?: AbortSignal): Promise<string[]> {
  return requestJson<string[]>("/procurement_types", signal);
}

/**
 * Filter options that still have matches under the other active filters.
 * Each list leaves its own filter out, so the current choice stays
 * selectable while every other dropdown only offers values that can
 * combine with it.
 */
export interface Facets {
  provinces: string[];
  locations: string[];
  categories: string[];
  sectors: string[];
  departments: string[];
  procurement_types: string[];
  source_ids: number[];
}

/** Facet options for the given filters (GET /facets). */
export function getFacets(
  params: ListParams = {},
  signal?: AbortSignal,
): Promise<Facets> {
  const query = queryString({
    source_id: params.sourceId,
    location: params.location,
    category: params.category,
    province: params.province,
    sector: params.sector,
    department: params.department,
    procurement_type: params.procurementType,
    deadline_from: params.deadlineFrom,
    deadline_to: params.deadlineTo,
    include_duplicates: params.includeDuplicates,
  });
  const kinds = (params.sourceKinds ?? [])
    .map((kind) => `source_kind=${encodeURIComponent(kind)}`)
    .join("&");
  const search = [query.replace(/^\?/, ""), kinds].filter(Boolean).join("&");
  return requestJson<Facets>(`/facets${search ? `?${search}` : ""}`, signal);
}

/** Deterministic analysis (+ LLM summary when a key is configured). */
export function aiAnalyze(
  opportunityId: number,
  signal?: AbortSignal,
): Promise<AiAnalysis> {
  return requestJsonPost<AiAnalysis>(`/ai/analyze/${opportunityId}`, {}, signal);
}

/** Verify a candidate duplicate group really is one tender. */
export function aiDedup(
  opportunityIds: number[],
  signal?: AbortSignal,
): Promise<AiDedupResult> {
  return requestJsonPost<AiDedupResult>(
    "/ai/dedup",
    { opportunity_ids: opportunityIds },
    signal,
  );
}

/** Draft a bid/RFP document for one tender (needs an LLM key). */
export function aiGenerateRfp(
  opportunityId: number,
  companyName: string,
  capabilities: string[],
  signal?: AbortSignal,
): Promise<AiRfpDraft> {
  return requestJsonPost<AiRfpDraft>(
    "/ai/rfp",
    {
      opportunity_id: opportunityId,
      company_name: companyName,
      capabilities,
    },
    signal,
  );
}

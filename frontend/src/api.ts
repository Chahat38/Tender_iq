/**
 * Typed read-only client for the TenderScout API (Stage 6 endpoints).
 *
 * Every function only performs GET requests; there are no write paths
 * in the UI, matching the API's read-only contract.
 */

import type {
  OpportunityDetail,
  OpportunityPage,
  SearchPage,
  Source,
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
  params: Record<string, string | number | undefined>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) {
      search.set(key, String(value));
    }
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export interface ListParams {
  limit?: number;
  offset?: number;
  /** Restrict to one registered source (browse mode only). */
  sourceId?: number;
}

/** Newest-first page of opportunities. */
export function listOpportunities(
  params: ListParams = {},
  signal?: AbortSignal,
): Promise<OpportunityPage> {
  return requestJson<OpportunityPage>(
    `/opportunities${queryString({
      limit: params.limit,
      offset: params.offset,
      source_id: params.sourceId,
    })}`,
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

/** FTS5 full-text search (bm25-ranked; lower rank = better). */
export function searchOpportunities(
  query: string,
  params: ListParams = {},
  signal?: AbortSignal,
): Promise<SearchPage> {
  return requestJson<SearchPage>(
    `/search${queryString({
      q: query,
      limit: params.limit,
      offset: params.offset,
    })}`,
    signal,
  );
}

/** All registered procurement sources. */
export function listSources(signal?: AbortSignal): Promise<Source[]> {
  return requestJson<Source[]>("/sources", signal);
}

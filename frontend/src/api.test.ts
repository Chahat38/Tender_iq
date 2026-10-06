/** Offline unit tests for the typed API client (global fetch is stubbed). */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  getOpportunity,
  listOpportunities,
  listSources,
  searchOpportunities,
} from "./api";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("listOpportunities", () => {
  it("requests the proxied endpoint with paging and source params", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ total: 0, limit: 20, offset: 40, opportunities: [] }),
    );
    const page = await listOpportunities({ limit: 20, offset: 40, sourceId: 2 });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/opportunities?limit=20&offset=40&source_id=2",
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
    expect(page.total).toBe(0);
  });

  it("omits absent filters from the query string", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ total: 0, limit: 20, offset: 0, opportunities: [] }),
    );
    await listOpportunities();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/opportunities",
      expect.anything(),
    );
  });
});

describe("searchOpportunities", () => {
  it("encodes the query for GET /search", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ query: "solar power", total: 0, limit: 5, offset: 0, results: [] }),
    );
    await searchOpportunities("solar power", { limit: 5 });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/search?q=solar+power&limit=5",
      expect.anything(),
    );
  });

  it("escapes query metacharacters so they cannot inject extra params", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ query: "a", total: 0, limit: 1, offset: 0, results: [] }),
    );
    await searchOpportunities("a&limit=999");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/search?q=a%26limit%3D999",
      expect.anything(),
    );
  });
});

describe("error handling", () => {
  it("raises ApiError carrying the server's detail on HTTP errors", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: "opportunity 99 not found" }, 404),
    );
    const error = await getOpportunity(99).then(
      () => {
        throw new Error("expected the promise to reject");
      },
      (err: unknown) => err,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(404);
    expect((error as ApiError).detail).toBe("opportunity 99 not found");
  });

  it("falls back to the status text for non-JSON error bodies", async () => {
    fetchMock.mockResolvedValue(
      new Response("Bad Gateway", { status: 502, statusText: "Bad Gateway" }),
    );
    const error = await listSources().then(
      () => {
        throw new Error("expected the promise to reject");
      },
      (err: unknown) => err,
    );
    expect((error as ApiError).detail).toBe("Bad Gateway");
  });
});

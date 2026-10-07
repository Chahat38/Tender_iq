/** Offline unit tests for the typed API client (global fetch is stubbed). */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  aiAnalyze,
  aiDedup,
  aiGenerateRfp,
  ApiError,
  getDuplicates,
  getNotifications,
  getOpportunity,
  listOpportunities,
  listProcurementTypes,
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

  it("encodes dedup, sort and procurement-type parameters", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ total: 0, limit: 20, offset: 0, opportunities: [] }),
    );
    await listOpportunities({
      procurementType: "works",
      sort: "closing",
      includeDuplicates: true,
      deadlineFrom: "2026-10-01",
      deadlineTo: "2026-10-31",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/opportunities?procurement_type=works&sort=closing&include_duplicates=true&deadline_from=2026-10-01&deadline_to=2026-10-31",
      expect.anything(),
    );
  });

  it("ignores date-only deadline bounds when the value is empty", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ total: 0, limit: 20, offset: 0, opportunities: [] }),
    );
    await listOpportunities({ deadlineFrom: "", deadlineTo: "" });
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

describe("getDuplicates", () => {
  it("loads sibling listings for one notice", async () => {
    fetchMock.mockResolvedValue(jsonResponse([{ id: 7, is_primary: false }]));
    const duplicates = await getDuplicates(7);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/opportunities/7/duplicates",
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
    expect(duplicates).toHaveLength(1);
    expect(duplicates[0].id).toBe(7);
  });
});

describe("getNotifications", () => {
  it("requests new and closing-soon buckets for a time window", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        generated_at: "2026-10-06T12:00:00+00:00",
        window_days: 7,
        new_tenders: [],
        new_total: 0,
        closing_soon: [],
        closing_total: 0,
      }),
    );
    await getNotifications(7, 20);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/notifications?window_days=7&limit=20",
      expect.anything(),
    );
  });
});

describe("listProcurementTypes", () => {
  it("loads the procurement-type facet", async () => {
    fetchMock.mockResolvedValue(jsonResponse(["goods", "works"]));
    const types = await listProcurementTypes();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/procurement_types",
      expect.anything(),
    );
    expect(types).toEqual(["goods", "works"]);
  });
});

describe("ai assist endpoints", () => {
  it("POSTs an analyze request with a JSON body", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ opportunity_id: 2, llm_available: false, keywords: [] }),
    );
    await aiAnalyze(2);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/ai/analyze/2");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({ "Content-Type": "application/json" });
    expect(JSON.parse(String(init.body))).toEqual({});
  });

  it("POSTs a dedup verification for the chosen opportunity ids", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ llm_available: false, same_tender: true }),
    );
    await aiDedup([4, 9]);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      opportunity_ids: [4, 9],
    });
  });

  it("POSTs an RFP draft request with company context", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ document_title: "Bid" }));
    await aiGenerateRfp(2, "Solar Works Pvt Ltd", ["Solar panels"]);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      opportunity_id: 2,
      company_name: "Solar Works Pvt Ltd",
      capabilities: ["Solar panels"],
    });
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

/** Component tests for the App shell (the ./api module is fully mocked). */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type {
  AiAnalysis,
  NotificationsPage as NotificationsData,
  Opportunity,
  OpportunityDetail,
  OpportunityPage,
  SearchPage,
  Source,
} from "./types";

vi.mock("./api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    detail: string;
    constructor(status: number, detail: string) {
      super(`API error ${status}: ${detail}`);
      this.name = "ApiError";
      this.status = status;
      this.detail = detail;
    }
  },
  listOpportunities: vi.fn(),
  listSources: vi.fn(),
  listCoverage: vi.fn(),
  listLocations: vi.fn(),
  listCategories: vi.fn(),
  listProvinces: vi.fn(),
  listSectors: vi.fn(),
  listDepartments: vi.fn(),
  listProcurementTypes: vi.fn(),
  getOpportunity: vi.fn(),
  getDuplicates: vi.fn(),
  getNotifications: vi.fn(),
  searchOpportunities: vi.fn(),
  aiAnalyze: vi.fn(),
  aiDedup: vi.fn(),
  aiGenerateRfp: vi.fn(),
}));

import * as api from "./api";
const mockedList = vi.mocked(api.listOpportunities);
const mockedListSources = vi.mocked(api.listSources);
const mockedListCoverage = vi.mocked(api.listCoverage);
const mockedLocations = vi.mocked(api.listLocations);
const mockedCategories = vi.mocked(api.listCategories);
const mockedProvinces = vi.mocked(api.listProvinces);
const mockedSectors = vi.mocked(api.listSectors);
const mockedDepartments = vi.mocked(api.listDepartments);
const mockedProcurementTypes = vi.mocked(api.listProcurementTypes);
const mockedSearch = vi.mocked(api.searchOpportunities);
const mockedGet = vi.mocked(api.getOpportunity);
const mockedGetDuplicates = vi.mocked(api.getDuplicates);
const mockedNotifications = vi.mocked(api.getNotifications);
const mockedAnalyze = vi.mocked(api.aiAnalyze);

// RTL cannot auto-register cleanup when vitest globals are off.
afterEach(() => {
  cleanup();
});

function makeOpportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: 1,
    source_id: 1,
    external_id: "TS0000015263E",
    reference_number: "P89833",
    title: "Re-tender for Maintenance Store Items SZHL FY 2026-27",
    organization: "Ministry of National Health Services",
    description: null,
    source_url:
      "https://epms.ppra.gov.pk/public/tenders/tender-details/TS0000015263E",
    published_at: "2026-09-30T11:00:00+05:00",
    deadline: "2026-10-21T11:00:00+05:00",
    location: "Islamabad",
    province: null,
    category: null,
    sector: null,
    department: null,
    procurement_type: null,
    content_hash: "abc123",
    first_seen_at: "2026-10-01T00:00:00+00:00",
    last_seen_at: "2026-10-01T00:00:00+00:00",
    last_verified_at: "2026-10-01T00:00:00+00:00",
    ...overrides,
  };
}

const page: OpportunityPage = {
  total: 2,
  limit: 20,
  offset: 0,
  opportunities: [
    makeOpportunity(),
    makeOpportunity({
      id: 2,
      external_id: "TS0000015253E",
      reference_number: null,
      title: "Supply of laboratory equipment for district hospital",
    }),
  ],
};

const sources: Source[] = [
  {
    id: 1,
    name: "PPRA EPMS",
    adapter_type: "PPRAEpmsAdapter",
    active: 1,
    robots_allowed: 1,
    terms_checked: 1,
    requires_authentication: 0,
    crawl_frequency: null,
    last_crawled_at: null,
    last_success_at: null,
    base_url: "https://epms.ppra.gov.pk",
    last_status: "ok",
    consecutive_failures: 0,
    opportunity_count: 2,
    last_error: null,
  },
];

const searchPage: SearchPage = {
  query: "medical",
  total: 1,
  limit: 20,
  offset: 0,
  results: [
    {
      opportunity_id: 1,
      external_id: "TS0000015263E",
      source_id: 1,
      title: "Re-tender for Maintenance Store Items SZHL FY 2026-27",
      organization: "Ministry of National Health Services",
      snippet: "supply of medical equipment and furniture",
      deadline: "2026-10-21T11:00:00+05:00",
      location: null,
      category: null,
      procurement_type: null,
      reference_number: null,
      source_url:
        "https://epms.ppra.gov.pk/public/tenders/tender-details/TS0000015263E",
      rank: -3.2,
    },
  ],
};

const detail: OpportunityDetail = {
  ...makeOpportunity({ id: 2, title: "Supply of laboratory equipment for district hospital" }),
  documents: [
    { id: 1, url: "https://epms.ppra.gov.pk/pdf?file=tender.pdf", filename: "tender.pdf" },
  ],
  updates: [],
};

const notifications: NotificationsData = {
  generated_at: "2026-10-06T12:00:00+00:00",
  window_days: 7,
  new_tenders: [makeOpportunity()],
  new_total: 1,
  closing_soon: [
    makeOpportunity({ id: 3, title: "Installation of solar panels at district court" }),
  ],
  closing_total: 1,
};

const analysis: AiAnalysis = {
  opportunity_id: 2,
  llm_available: false,
  provider: "deterministic",
  model: null,
  prompt_version: "p2",
  category: "goods",
  subcategory: null,
  keywords: ["laboratory", "equipment"],
  procurement_type: "goods",
  requirements: ["Valid NTN certificate"],
  date_conflicts: [],
  summary: "Supply of laboratory equipment for district hospital.",
  fit_hints: ["Renewable energy suppliers likely relevant"],
  red_flags: ["Short submission window"],
};

const defaultBrowseParams = {
  limit: 20,
  offset: 0,
  sourceId: undefined,
  location: undefined,
  category: undefined,
  province: undefined,
  sector: undefined,
  department: undefined,
  procurementType: undefined,
  sort: "newest" as const,
  includeDuplicates: undefined,
  deadlineFrom: undefined,
  deadlineTo: undefined,
};

beforeEach(() => {
  window.location.hash = "";
  vi.resetAllMocks();
  mockedList.mockResolvedValue(page);
  mockedListSources.mockResolvedValue(sources);
  mockedListCoverage.mockResolvedValue([]);
  mockedLocations.mockResolvedValue(["karachi, pakistan", "lahore, pakistan"]);
  mockedCategories.mockResolvedValue([]);
  mockedProvinces.mockResolvedValue([]);
  mockedSectors.mockResolvedValue([]);
  mockedDepartments.mockResolvedValue([]);
  mockedProcurementTypes.mockResolvedValue(["goods", "works", "services"]);
  mockedSearch.mockResolvedValue(searchPage);
  mockedGetDuplicates.mockResolvedValue([]);
  mockedNotifications.mockResolvedValue(notifications);
  mockedAnalyze.mockResolvedValue(analysis);
});

describe("App browse mode", () => {
  it("renders the opportunity list with paging metadata", async () => {
    render(<App />);
    expect(
      await screen.findByText(
        "Re-tender for Maintenance Store Items SZHL FY 2026-27",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Supply of laboratory equipment for district hospital"),
    ).toBeInTheDocument();
    expect(screen.getByText("Showing 1–2 of 2")).toBeInTheDocument();
    expect(mockedList).toHaveBeenCalledWith(
      defaultBrowseParams,
      expect.anything(),
    );
  });

  it("credits every registered source in the footer", async () => {
    render(<App />);
    await screen.findByText(
      "Re-tender for Maintenance Store Items SZHL FY 2026-27",
    );
    expect(
      screen.getByText(/Data from public PPRA EPMS pages\./),
    ).toBeInTheDocument();
  });

  it("filters by city in browse mode", async () => {
    render(<App />);
    await screen.findByText(
      "Re-tender for Maintenance Store Items SZHL FY 2026-27",
    );
    fireEvent.change(screen.getByLabelText("Filter by city"), {
      target: { value: "karachi, pakistan" },
    });
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(
        { ...defaultBrowseParams, location: "karachi, pakistan" },
        expect.anything(),
      ),
    );
  });

  it("filters by industry and department in browse mode", async () => {
    mockedSectors.mockResolvedValue(["it & telecom"]);
    mockedDepartments.mockResolvedValue(["information technology"]);
    render(<App />);
    await screen.findByText(
      "Re-tender for Maintenance Store Items SZHL FY 2026-27",
    );
    expect(
      await screen.findByRole("option", { name: "IT & Telecom" }),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Filter by industry"), {
      target: { value: "it & telecom" },
    });
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(
        { ...defaultBrowseParams, sector: "it & telecom" },
        expect.anything(),
      ),
    );

    fireEvent.change(screen.getByLabelText("Filter by department"), {
      target: { value: "information technology" },
    });
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(
        {
          ...defaultBrowseParams,
          sector: "it & telecom",
          department: "information technology",
        },
        expect.anything(),
      ),
    );
  });

  it("offers an industry filter instead of an organization filter", async () => {
    render(<App />);
    await screen.findByText(
      "Re-tender for Maintenance Store Items SZHL FY 2026-27",
    );
    expect(screen.getByLabelText("Filter by industry")).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Filter by organization"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByPlaceholderText("Contains…"),
    ).not.toBeInTheDocument();
  });

  it("filters by procurement type and sorts by closing soon", async () => {
    render(<App />);
    await screen.findByText(
      "Re-tender for Maintenance Store Items SZHL FY 2026-27",
    );
    fireEvent.change(screen.getByLabelText("Filter by procurement type"), {
      target: { value: "works" },
    });
    fireEvent.change(screen.getByLabelText("Order by"), {
      target: { value: "closing" },
    });
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(
        {
          ...defaultBrowseParams,
          procurementType: "works",
          sort: "closing",
        },
        expect.anything(),
      ),
    );
  });

  it("reveals duplicate listings when the mirror toggle is on", async () => {
    render(<App />);
    await screen.findByText(
      "Re-tender for Maintenance Store Items SZHL FY 2026-27",
    );
    fireEvent.click(screen.getByLabelText("Include duplicate listings"));
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(
        { ...defaultBrowseParams, includeDuplicates: true },
        expect.anything(),
      ),
    );
  });

  it("filters by source in browse mode", async () => {
    render(<App />);
    await screen.findByText(
      "Re-tender for Maintenance Store Items SZHL FY 2026-27",
    );
    fireEvent.change(screen.getByLabelText("Filter by source"), {
      target: { value: "1" },
    });
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(
        { ...defaultBrowseParams, sourceId: 1 },
        expect.anything(),
      ),
    );
  });

  it("pages through results with Next", async () => {
    mockedList.mockResolvedValue({ ...page, total: 21 });
    render(<App />);
    await screen.findByText(
      "Re-tender for Maintenance Store Items SZHL FY 2026-27",
    );
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith(
        { ...defaultBrowseParams, offset: 20 },
        expect.anything(),
      ),
    );
    expect(screen.getByText("Showing 21–21 of 21")).toBeInTheDocument();
  });
});

describe("App search mode", () => {
  it("submits a search and shows ranked hits with snippets", async () => {
    render(<App />);
    await screen.findByText(
      "Re-tender for Maintenance Store Items SZHL FY 2026-27",
    );
    fireEvent.change(screen.getByLabelText("Search tenders"), {
      target: { value: "medical" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(
      await screen.findByText("supply of medical equipment and furniture"),
    ).toBeInTheDocument();
    expect(mockedSearch).toHaveBeenCalledWith(
      "medical",
      { limit: 20, offset: 0 },
      expect.anything(),
    );
  });

  it("clears a search and returns to the browse list", async () => {
    render(<App />);
    await screen.findByText(
      "Re-tender for Maintenance Store Items SZHL FY 2026-27",
    );
    fireEvent.change(screen.getByLabelText("Search tenders"), {
      target: { value: "medical" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("supply of medical equipment and furniture");
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(
      await screen.findByText("Showing 1–2 of 2"),
    ).toBeInTheDocument();
    expect(mockedList).toHaveBeenLastCalledWith(
      defaultBrowseParams,
      expect.anything(),
    );
  });
});

describe("App detail view", () => {
  it("opens a detail view from a row and returns to the list", async () => {
    mockedGet.mockResolvedValue(detail);
    render(<App />);
    fireEvent.click(
      await screen.findByText(
        "Supply of laboratory equipment for district hospital",
      ),
    );
    expect(await screen.findByText("Documents (1)")).toBeInTheDocument();
    expect(screen.getByText(/tender\.pdf/)).toBeInTheDocument();
    expect(mockedGet).toHaveBeenCalledWith(2, expect.anything());
    expect(mockedGetDuplicates).toHaveBeenCalledWith(2, expect.anything());
    fireEvent.click(screen.getByRole("button", { name: /Back to list/i }));
    expect(await screen.findByText("Showing 1–2 of 2")).toBeInTheDocument();
  });

  it("runs AI analysis from the detail page", async () => {
    mockedGet.mockResolvedValue(detail);
    render(<App />);
    fireEvent.click(
      await screen.findByText(
        "Supply of laboratory equipment for district hospital",
      ),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Analyze this tender" }),
    );
    expect(
      await screen.findByText(
        "Supply of laboratory equipment for district hospital.",
      ),
    ).toBeInTheDocument();
    expect(mockedAnalyze).toHaveBeenCalledWith(2);
  });

  it("shows an error banner when the API fails", async () => {
    mockedList.mockRejectedValue(new api.ApiError(500, "Internal database error."));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "API error 500: Internal database error.",
    );
  });
});

describe("App notifications page", () => {
  it("renders new and closing-soon feeds from the nav link", async () => {
    render(<App />);
    await screen.findByText(
      "Re-tender for Maintenance Store Items SZHL FY 2026-27",
    );
    fireEvent.click(screen.getByRole("button", { name: "Notifications" }));
    expect(
      await screen.findByText("Installation of solar panels at district court"),
    ).toBeInTheDocument();
    expect(mockedNotifications).toHaveBeenCalledWith(7, 20, expect.anything());
  });
});
import { useEffect, useRef, useState, type ChangeEvent } from "react";
import type { SourceKind } from "../api";
import { SearchableSelect } from "./SearchableSelect";

/**
 * All browse-mode filters in one bar: source family (government or
 * corporate), one portal, city, category, province, industry (the
 * stored sector vocabulary), department and procurement type plus a
 * deadline date range, an order switch (newest / closing soon) and a
 * toggle that reveals mirrored duplicate listings.
 * Values the user picks are opaque strings sent straight to the API;
 * scraped facet values are never parsed by the UI. The deadline range
 * sends date-only strings; the API compares the stored ISO date's first
 * ten characters (the deadline's Pakistan-local date), inclusive on both
 * ends.
 */
export type SortValue = "newest" | "closing";

export interface FilterValues {
  /** Source family: government portals and/or corporate sources. */
  sourceKinds: SourceKind[];
  sourceId: number | null;
  location: string;
  category: string;
  province: string;
  sector: string;
  department: string;
  procurementType: string;
  /** ISO date (YYYY-MM-DD) or "" — inclusive lower bound on deadlines. */
  deadlineFrom: string;
  /** ISO date (YYYY-MM-DD) or "" — inclusive upper bound on deadlines. */
  deadlineTo: string;
  /** newest = insertion order; closing = soonest deadline first. */
  sort: SortValue;
  /** Show every duplicate listing instead of one row per notice. */
  includeDuplicates: boolean;
}

export const EMPTY_FILTERS: FilterValues = {
  sourceKinds: [],
  sourceId: null,
  location: "",
  category: "",
  province: "",
  sector: "",
  department: "",
  procurementType: "",
  deadlineFrom: "",
  deadlineTo: "",
  sort: "newest",
  includeDuplicates: false,
};

export function filtersAreDefault(filters: FilterValues): boolean {
  return (
    filters.sourceKinds.length === 0 &&
    filters.sourceId === null &&
    filters.location === "" &&
    filters.category === "" &&
    filters.province === "" &&
    filters.sector === "" &&
    filters.department === "" &&
    filters.procurementType === "" &&
    filters.deadlineFrom === "" &&
    filters.deadlineTo === "" &&
    filters.sort === "newest" &&
    filters.includeDuplicates === false
  );
}

function activeFilterCount(filters: FilterValues): number {
  let count = 0;
  if (filters.sourceKinds.length > 0) count += 1;
  if (filters.sourceId !== null) count += 1;
  if (filters.location) count += 1;
  if (filters.category) count += 1;
  if (filters.province) count += 1;
  if (filters.sector) count += 1;
  if (filters.department) count += 1;
  if (filters.procurementType) count += 1;
  if (filters.deadlineFrom) count += 1;
  if (filters.deadlineTo) count += 1;
  if (filters.sort !== "newest") count += 1;
  if (filters.includeDuplicates) count += 1;
  return count;
}

interface FilterBarProps {
  values: FilterValues;
  sources: { id: number; name: string }[];
  locations: string[];
  categories: string[];
  provinces: string[];
  sectors: string[];
  departments: string[];
  procurementTypes: string[];
  disabled: boolean;
  onChange: (values: FilterValues) => void;
}

const FIELD_CLASS =
  "w-full rounded-lg border border-slate-700/80 bg-slate-900 px-3 py-2 text-sm text-slate-200 transition focus:border-sky-500 focus:outline-none disabled:cursor-not-allowed disabled:opacity-40";

/**
 * Display form for facet values. The API returns controlled-vocabulary
 * facets lowercased for case-insensitive matching ("it & telecom",
 * "khyber pakhtunkhwa"); the option value stays exactly as sent while the
 * label is humanized. Acronyms keep their conventional casing.
 */
const FACET_ACRONYMS: Record<string, string> = {
  it: "IT",
  ict: "ICT",
  hr: "HR",
  undp: "UNDP",
  wasa: "WASA",
  nadra: "NADRA",
  pta: "PTA",
  gis: "GIS",
  erp: "ERP",
  cctv: "CCTV",
  scada: "SCADA",
  "p&d": "P&D",
  "c&w": "C&W",
};

function facetLabel(value: string): string {
  return value
    .split(" ")
    .map((word) => {
      const bare = word.replace(/[^a-z0-9&]/gi, "").toLowerCase();
      const acronym = FACET_ACRONYMS[bare];
      if (acronym) {
        return word.length === bare.length ? acronym : word;
      }
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(" ");
}

/**
 * Keep the current choice listed even when its facet no longer offers it,
 * so narrowing the options never blanks out an active filter.
 */
function withSelected(options: string[], selected: string): string[] {
  return selected !== "" && !options.includes(selected)
    ? [selected, ...options]
    : options;
}

function FieldLabel({ children }: { children: string }) {
  return (
    <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-slate-500">
      {children}
    </span>
  );
}

function change<T extends HTMLSelectElement | HTMLInputElement>(
  key: keyof FilterValues,
  props: FilterBarProps,
  transform: (event: ChangeEvent<T>) => FilterValues[keyof FilterValues],
): (event: ChangeEvent<T>) => void {
  return (event) => {
    props.onChange({ ...props.values, [key]: transform(event) });
  };
}

/** The two source families, in display order. */
const SOURCE_KIND_OPTIONS: { value: SourceKind; label: string }[] = [
  { value: "government", label: "Government" },
  { value: "corporate", label: "Corporate" },
];

/**
 * Government/Corporate multi-select: a dropdown holding two checkboxes,
 * so the user can pick either family or both. Picking neither means
 * "all sources" (the same thing as picking both today, but the empty
 * state keeps the filter visibly inactive).
 */
function SourceKindSelect({
  values,
  disabled,
  onChange,
}: {
  values: SourceKind[];
  disabled: boolean;
  onChange: (next: SourceKind[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    function onPointerDown(event: MouseEvent) {
      if (
        containerRef.current &&
        !containerRef.current.contains(event.target as Node)
      ) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  const selected = SOURCE_KIND_OPTIONS.filter((option) =>
    values.includes(option.value),
  );
  const summary =
    selected.length === 0
      ? "All sources"
      : selected.map((option) => option.label).join(", ");

  function toggle(kind: SourceKind) {
    const next = values.includes(kind)
      ? values.filter((value) => value !== kind)
      : [...values, kind];
    // Keep a stable order so the query string does not churn.
    onChange(
      SOURCE_KIND_OPTIONS.map((option) => option.value).filter((value) =>
        next.includes(value),
      ),
    );
  }

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        aria-label="Filter by source"
        aria-expanded={open}
        aria-haspopup="listbox"
        onClick={() => setOpen((current) => !current)}
        disabled={disabled}
        className={`${FIELD_CLASS} flex items-center justify-between gap-2 text-left`}
      >
        <span className="truncate">{summary}</span>
        <svg
          viewBox="0 0 24 24"
          className="h-3.5 w-3.5 shrink-0 text-slate-500"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {open ? (
        <div
          role="group"
          aria-label="Source options"
          className="absolute left-0 right-0 top-full z-30 mt-1 rounded-lg border border-slate-700 bg-slate-900 p-1.5 shadow-xl shadow-slate-950/60"
        >
          {SOURCE_KIND_OPTIONS.map((option) => (
            <label
              key={option.value}
              className="flex cursor-pointer select-none items-center gap-2 rounded px-2 py-1.5 text-sm text-slate-300 transition hover:bg-slate-800"
            >
              <input
                type="checkbox"
                checked={values.includes(option.value)}
                onChange={() => toggle(option.value)}
                aria-label={option.label}
                className="h-4 w-4 rounded border-slate-600 bg-slate-800 accent-sky-500"
              />
              {option.label}
            </label>
          ))}
          <button
            type="button"
            onClick={() => {
              onChange([]);
              setOpen(false);
            }}
            className="mt-1 block w-full rounded px-2 py-1.5 text-left text-xs font-medium text-slate-400 transition hover:bg-slate-800 hover:text-slate-200"
          >
            All sources
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function FilterBar(props: FilterBarProps) {
  const {
    values,
    sources,
    locations,
    categories,
    provinces,
    sectors,
    departments,
    procurementTypes,
    disabled,
  } = props;
  const applied = activeFilterCount(values);

  return (
    <div
      data-testid="filter-bar"
      className="rounded-2xl border border-slate-800 bg-slate-900/60 p-4 shadow-lg shadow-slate-950/30"
    >
      <div className="flex items-center justify-between gap-3 pb-3">
        <h2 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-slate-500">
          <svg
            viewBox="0 0 24 24"
            className="h-3.5 w-3.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            aria-hidden="true"
          >
            <path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z" />
          </svg>
          Filters
          {applied > 0 ? (
            <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-[10px] font-semibold text-sky-300">
              {applied} active
            </span>
          ) : null}
        </h2>
        {!filtersAreDefault(values) ? (
          <button
            type="button"
            onClick={() => props.onChange(EMPTY_FILTERS)}
            className="rounded-lg border border-slate-700 px-2.5 py-1 text-xs font-medium text-slate-300 transition hover:border-slate-600 hover:bg-slate-800"
          >
            Clear all filters
          </button>
        ) : null}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <div>
          <FieldLabel>Source</FieldLabel>
          <SourceKindSelect
            values={values.sourceKinds}
            disabled={disabled}
            onChange={(sourceKinds) =>
              props.onChange({ ...props.values, sourceKinds })
            }
          />
        </div>

        <div>
          <FieldLabel>Province</FieldLabel>
          <select
            value={values.province}
            onChange={change("province", props, (e) => e.target.value)}
            disabled={disabled}
            aria-label="Filter by province"
            title="Province filter applies to browse mode only"
            className={FIELD_CLASS}
          >
            <option value="">All provinces</option>
            {withSelected(provinces, values.province).map((province) => (
              <option key={province} value={province}>
                {facetLabel(province)}
              </option>
            ))}
          </select>
        </div>

        <div>
          <FieldLabel>City</FieldLabel>
          <SearchableSelect
            label="Filter by city"
            value={values.location}
            options={withSelected(locations, values.location)}
            allLabel="All cities"
            disabled={disabled}
            onChange={(location) =>
              props.onChange({ ...props.values, location })
            }
          />
        </div>

        <div>
          <FieldLabel>Industry</FieldLabel>
          <select
            value={values.sector}
            onChange={change("sector", props, (e) => e.target.value)}
            disabled={disabled}
            aria-label="Filter by industry"
            className={FIELD_CLASS}
          >
            <option value="">All industries</option>
            {withSelected(sectors, values.sector).map((sector) => (
              <option key={sector} value={sector}>
                {facetLabel(sector)}
              </option>
            ))}
          </select>
        </div>

        <div>
          <FieldLabel>Category</FieldLabel>
          <select
            value={values.category}
            onChange={change("category", props, (e) => e.target.value)}
            disabled={disabled}
            aria-label="Filter by category"
            title="Category filter applies to browse mode only"
            className={FIELD_CLASS}
          >
            <option value="">All categories</option>
            {withSelected(categories, values.category).map((category) => (
              <option key={category} value={category}>
                {category}
              </option>
            ))}
          </select>
        </div>

        <div>
          <FieldLabel>Portal</FieldLabel>
          <select
            value={values.sourceId ?? ""}
            onChange={change("sourceId", props, (e) =>
              e.target.value === "" ? null : Number(e.target.value),
            )}
            disabled={disabled}
            aria-label="Filter by portal"
            title="Narrow to one portal (browse mode only)"
            className={FIELD_CLASS}
          >
            <option value="">All portals</option>
            {sources.map((source) => (
              <option key={source.id} value={source.id}>
                {source.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <FieldLabel>Department</FieldLabel>
          <select
            value={values.department}
            onChange={change("department", props, (e) => e.target.value)}
            disabled={disabled}
            aria-label="Filter by department"
            className={FIELD_CLASS}
          >
            <option value="">All departments</option>
            {withSelected(departments, values.department).map((department) => (
              <option key={department} value={department}>
                {facetLabel(department)}
              </option>
            ))}
          </select>
        </div>

        <div>
          <FieldLabel>Type</FieldLabel>
          <select
            value={values.procurementType}
            onChange={change("procurementType", props, (e) => e.target.value)}
            disabled={disabled}
            aria-label="Filter by procurement type"
            title="Procurement type filter applies to browse mode only"
            className={FIELD_CLASS}
          >
            <option value="">All types</option>
            {withSelected(procurementTypes, values.procurementType).map((procurementType) => (
              <option key={procurementType} value={procurementType}>
                {procurementType}
              </option>
            ))}
          </select>
        </div>

        <div className="col-span-2">
          <FieldLabel>Deadline window</FieldLabel>
          <div className="flex items-center gap-2">
            <label htmlFor="deadline-from" className="sr-only">
              Deadline from
            </label>
            <input
              id="deadline-from"
              type="date"
              value={values.deadlineFrom}
              onChange={change("deadlineFrom", props, (e) => e.target.value)}
              disabled={disabled}
              aria-label="Deadline from"
              title="Only tenders closing on or after this date"
              className={`${FIELD_CLASS} flex-1`}
            />
            <span
              aria-hidden="true"
              className="text-slate-600"
            >
              →
            </span>
            <label htmlFor="deadline-to" className="sr-only">
              Deadline to
            </label>
            <input
              id="deadline-to"
              type="date"
              value={values.deadlineTo}
              onChange={change("deadlineTo", props, (e) => e.target.value)}
              disabled={disabled}
              aria-label="Deadline to"
              title="Only tenders closing on or before this date"
              className={`${FIELD_CLASS} flex-1`}
            />
          </div>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-slate-800/80 pt-3">
        <label
          className="flex cursor-pointer select-none items-center gap-2 text-sm text-slate-300"
          title="Show the same notice as listed by every source"
        >
          <span className="relative inline-block h-5 w-9">
            <input
              type="checkbox"
              checked={values.includeDuplicates}
              onChange={change(
                "includeDuplicates",
                props,
                (e) => e.target.checked,
              )}
              disabled={disabled}
              aria-label="Include duplicate listings"
              className="peer sr-only"
            />
            <span className="absolute inset-0 rounded-full bg-slate-700 transition peer-checked:bg-sky-500/70 peer-disabled:opacity-40" />
            <span className="absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white shadow transition peer-checked:translate-x-4" />
          </span>
          Show mirror listings
        </label>

        <div className="flex flex-wrap items-center gap-2">
          <select
            value={values.sort}
            onChange={change("sort", props, (e) => e.target.value as SortValue)}
            disabled={disabled}
            aria-label="Order by"
            title="Order results by newest or soonest closing"
            className={`${FIELD_CLASS} !w-auto`}
          >
            <option value="newest">Newest first</option>
            <option value="closing">Closing soon first</option>
          </select>
        </div>
      </div>
    </div>
  );
}
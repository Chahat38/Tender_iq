import type { ChangeEvent } from "react";

/**
 * All browse-mode filters in one bar: source, city, category, province,
 * department, procurement type and industry (the stored sector
 * vocabulary) plus a deadline date range, an order switch (newest /
 * closing soon) and a toggle that reveals mirrored duplicate listings.
 * Values the user picks are opaque strings sent straight to the API;
 * scraped facet values are never parsed by the UI. The deadline range
 * sends date-only strings; the API compares the stored ISO date's first
 * ten characters (the deadline's Pakistan-local date), inclusive on both
 * ends.
 */
export type SortValue = "newest" | "closing";

export interface FilterValues {
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
          <select
            value={values.sourceId ?? ""}
            onChange={change("sourceId", props, (e) =>
              e.target.value === "" ? null : Number(e.target.value),
            )}
            disabled={disabled}
            aria-label="Filter by source"
            title="Source filter applies to browse mode only"
            className={FIELD_CLASS}
          >
            <option value="">All sources</option>
            {sources.map((source) => (
              <option key={source.id} value={source.id}>
                {source.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <FieldLabel>City</FieldLabel>
          <select
            value={values.location}
            onChange={change("location", props, (e) => e.target.value)}
            disabled={disabled}
            aria-label="Filter by city"
            title="City filter applies to browse mode only"
            className={FIELD_CLASS}
          >
            <option value="">All cities</option>
            {locations.map((location) => (
              <option key={location} value={location}>
                {location}
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
            {categories.map((category) => (
              <option key={category} value={category}>
                {category}
              </option>
            ))}
          </select>
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
            {provinces.map((province) => (
              <option key={province} value={province}>
                {facetLabel(province)}
              </option>
            ))}
          </select>
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
            {sectors.map((sector) => (
              <option key={sector} value={sector}>
                {facetLabel(sector)}
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
            {departments.map((department) => (
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
            {procurementTypes.map((procurementType) => (
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
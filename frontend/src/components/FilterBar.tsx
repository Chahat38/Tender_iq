import type { ChangeEvent } from "react";

/**
 * All browse-mode filters in one bar: source, city, category, province,
 * procurement type and organization (text) plus a deadline date range,
 * an order switch (newest / closing soon) and a toggle that reveals
 * mirrored duplicate listings. Values the user picks are opaque strings
 * sent straight to the API; scraped facet values are never parsed by
 * the UI. The deadline range sends date-only strings; the API compares
 * the stored ISO date's first ten characters (the deadline's
 * Pakistan-local date), inclusive on both ends.
 */
export type SortValue = "newest" | "closing";

export interface FilterValues {
  sourceId: number | null;
  location: string;
  category: string;
  province: string;
  procurementType: string;
  organization: string;
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
  procurementType: "",
  organization: "",
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
    filters.procurementType === "" &&
    filters.organization === "" &&
    filters.deadlineFrom === "" &&
    filters.deadlineTo === "" &&
    filters.sort === "newest" &&
    filters.includeDuplicates === false
  );
}

interface FilterBarProps {
  values: FilterValues;
  sources: { id: number; name: string }[];
  locations: string[];
  categories: string[];
  provinces: string[];
  procurementTypes: string[];
  disabled: boolean;
  onChange: (values: FilterValues) => void;
}

const SELECT_CLASS =
  "rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 disabled:cursor-not-allowed disabled:opacity-40";
const INPUT_CLASS =
  "rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-500 disabled:cursor-not-allowed disabled:opacity-40";

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
    procurementTypes,
    disabled,
  } = props;

  return (
    <div
      data-testid="filter-bar"
      className="flex flex-wrap items-center gap-2"
    >
      <select
        value={values.sourceId ?? ""}
        onChange={change("sourceId", props, (e) =>
          e.target.value === "" ? null : Number(e.target.value),
        )}
        disabled={disabled}
        aria-label="Filter by source"
        title="Source filter applies to browse mode only"
        className={SELECT_CLASS}
      >
        <option value="">All sources</option>
        {sources.map((source) => (
          <option key={source.id} value={source.id}>
            {source.name}
          </option>
        ))}
      </select>

      <select
        value={values.location}
        onChange={change("location", props, (e) => e.target.value)}
        disabled={disabled}
        aria-label="Filter by city"
        title="City filter applies to browse mode only"
        className={SELECT_CLASS}
      >
        <option value="">All cities</option>
        {locations.map((location) => (
          <option key={location} value={location}>
            {location}
          </option>
        ))}
      </select>

      <select
        value={values.category}
        onChange={change("category", props, (e) => e.target.value)}
        disabled={disabled}
        aria-label="Filter by category"
        title="Category filter applies to browse mode only"
        className={SELECT_CLASS}
      >
        <option value="">All categories</option>
        {categories.map((category) => (
          <option key={category} value={category}>
            {category}
          </option>
        ))}
      </select>

      <select
        value={values.province}
        onChange={change("province", props, (e) => e.target.value)}
        disabled={disabled}
        aria-label="Filter by province"
        title="Province filter applies to browse mode only"
        className={SELECT_CLASS}
      >
        <option value="">All provinces</option>
        {provinces.map((province) => (
          <option key={province} value={province}>
            {province}
          </option>
        ))}
      </select>

      <select
        value={values.procurementType}
        onChange={change("procurementType", props, (e) => e.target.value)}
        disabled={disabled}
        aria-label="Filter by procurement type"
        title="Procurement type filter applies to browse mode only"
        className={SELECT_CLASS}
      >
        <option value="">All procurement types</option>
        {procurementTypes.map((procurementType) => (
          <option key={procurementType} value={procurementType}>
            {procurementType}
          </option>
        ))}
      </select>

      <input
        type="text"
        value={values.organization}
        onChange={change("organization", props, (e) => e.target.value)}
        disabled={disabled}
        maxLength={160}
        placeholder="Organization contains…"
        aria-label="Filter by organization"
        title="Organization filter applies to browse mode only"
        className={`${INPUT_CLASS} w-56`}
      />

      <span className="flex items-center gap-1 text-xs text-slate-400">
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
          className={SELECT_CLASS}
        />
        <span aria-hidden="true">→</span>
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
          className={SELECT_CLASS}
        />
      </span>

      <select
        value={values.sort}
        onChange={change("sort", props, (e) => e.target.value as SortValue)}
        disabled={disabled}
        aria-label="Order by"
        title="Order results by newest or soonest closing"
        className={SELECT_CLASS}
      >
        <option value="newest">Newest first</option>
        <option value="closing">Closing soon first</option>
      </select>

      <label
        className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-300"
        title="Show the same notice as listed by every source"
      >
        <input
          type="checkbox"
          checked={values.includeDuplicates}
          onChange={change("includeDuplicates", props, (e) => e.target.checked)}
          disabled={disabled}
          aria-label="Include duplicate listings"
          className="h-3.5 w-3.5 accent-sky-500"
        />
        Show mirror listings
      </label>

      {!filtersAreDefault(values) ? (
        <button
          type="button"
          onClick={() => props.onChange(EMPTY_FILTERS)}
          className="rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800"
        >
          Clear filters
        </button>
      ) : null}
    </div>
  );
}
import type { FormEvent } from "react";

interface SearchBarProps {
  /** Controlled input value (owned by App). */
  value: string;
  /** True while a search is active (shows the Clear button). */
  active: boolean;
  onValueChange: (value: string) => void;
  /** Submit the trimmed query; empty queries are ignored. */
  onSubmit: (query: string) => void;
  onClear: () => void;
}

/** Free-text search form backed by FTS5 (GET /search). */
export function SearchBar({
  value,
  active,
  onValueChange,
  onSubmit,
  onClear,
}: SearchBarProps) {
  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = value.trim();
    if (query) {
      onSubmit(query);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-1 gap-2" role="search">
      <input
        type="search"
        value={value}
        onChange={(event) => onValueChange(event.target.value)}
        placeholder="Search tenders (every word must match)…"
        aria-label="Search tenders"
        className="min-w-0 flex-1 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none"
      />
      <button
        type="submit"
        className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500"
      >
        Search
      </button>
      {active && (
        <button
          type="button"
          onClick={onClear}
          className="rounded-md border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800"
        >
          Clear
        </button>
      )}
    </form>
  );
}

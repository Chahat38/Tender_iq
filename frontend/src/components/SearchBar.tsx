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
    <form
      onSubmit={handleSubmit}
      role="search"
      className="flex items-center gap-2 rounded-2xl border border-slate-800 bg-slate-900/70 p-2 shadow-lg shadow-slate-950/40 transition focus-within:border-sky-500/60"
    >
      <div className="relative min-w-0 flex-1">
        <svg
          viewBox="0 0 24 24"
          className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          aria-hidden="true"
        >
          <circle cx="11" cy="11" r="7" />
          <path d="m21 21-4.3-4.3" />
        </svg>
        <input
          type="search"
          value={value}
          onChange={(event) => onValueChange(event.target.value)}
          placeholder="Search tenders, organizations, keywords…"
          aria-label="Search tenders"
          className="min-w-0 w-full rounded-xl border border-transparent bg-transparent py-2.5 pl-10 pr-3 text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none"
        />
      </div>
      <button
        type="submit"
        className="rounded-xl bg-gradient-to-r from-sky-500 to-indigo-500 px-5 py-2.5 text-sm font-semibold text-white shadow-md shadow-sky-500/25 transition hover:from-sky-400 hover:to-indigo-400 active:scale-[0.99]"
      >
        Search
      </button>
      {active && (
        <button
          type="button"
          onClick={onClear}
          className="rounded-xl border border-slate-700 px-3.5 py-2.5 text-sm font-medium text-slate-300 transition hover:border-slate-600 hover:bg-slate-800"
        >
          Clear
        </button>
      )}
    </form>
  );
}
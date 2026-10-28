import { useState, type FormEvent, type KeyboardEvent } from "react";

interface SearchBarProps {
  /** Controlled input value (owned by App). */
  value: string;
  /** True while a search is active (shows the Clear button). */
  active: boolean;
  /**
   * Facet labels (industries, categories, departments, provinces, cities,
   * sources) offered back to the user as they type. Computed in App so the
   * bar stays a dumb input; the prediction itself lives in
   * {@link predictSuggestions}.
   */
  suggestions?: string[];
  onValueChange: (value: string) => void;
  /** Submit the trimmed query; empty queries are ignored. */
  onSubmit: (query: string) => void;
  onClear: () => void;
}

/** Minimum typed characters before predictions appear. */
export const MIN_SUGGESTION_CHARS = 2;

/** How many predictions a dropdown may show. */
export const MAX_SUGGESTIONS = 8;

/**
 * Predict what the user is about to search from the facet vocabulary.
 *
 * Prefix matches win over substring matches (so "sec" ranks
 * "security & surveillance" above "research & development") and the query
 * itself is never suggested back.
 */
export function predictSuggestions(
  value: string,
  pool: string[] = [],
): string[] {
  const query = value.trim().toLowerCase();
  if (query.length < MIN_SUGGESTION_CHARS) {
    return [];
  }
  const prefix: string[] = [];
  const substring: string[] = [];
  for (const item of pool) {
    const lower = item.toLowerCase();
    if (lower === query) {
      continue;
    }
    if (lower.startsWith(query)) {
      prefix.push(item);
    } else if (lower.includes(query)) {
      substring.push(item);
    }
    if (prefix.length >= MAX_SUGGESTIONS) {
      break;
    }
  }
  return [...prefix, ...substring].slice(0, MAX_SUGGESTIONS);
}

/** Free-text search form backed by FTS5 (GET /search) with predictions. */
export function SearchBar({
  value,
  active,
  suggestions = [],
  onValueChange,
  onSubmit,
  onClear,
}: SearchBarProps) {
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(-1);

  const options = predictSuggestions(value, suggestions);
  const showList = open && options.length > 0;

  function submit(query: string) {
    const trimmed = query.trim();
    if (trimmed) {
      setOpen(false);
      setHighlight(-1);
      onSubmit(trimmed);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    submit(highlight >= 0 ? options[highlight] : value);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (!showList) {
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setHighlight((index) => (index + 1) % options.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setHighlight((index) =>
        index <= 0 ? options.length - 1 : index - 1,
      );
    } else if (event.key === "Enter" && highlight >= 0) {
      event.preventDefault();
      const picked = options[highlight];
      onValueChange(picked);
      submit(picked);
    } else if (event.key === "Escape") {
      setOpen(false);
      setHighlight(-1);
    }
  }

  function choose(option: string) {
    onValueChange(option);
    submit(option);
  }

  /** "software consultancy" + "soft" -> ["soft", "ware consultancy"]. */
  function splitMatch(option: string) {
    const index = option.toLowerCase().indexOf(value.trim().toLowerCase());
    if (index < 0 || !value.trim()) {
      return [option, "", ""];
    }
    return [
      option.slice(0, index),
      option.slice(index, index + value.trim().length),
      option.slice(index + value.trim().length),
    ];
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
          onChange={(event) => {
            onValueChange(event.target.value);
            setOpen(true);
            setHighlight(-1);
          }}
          onKeyDown={handleKeyDown}
          onFocus={() => setOpen(true)}
          onBlur={() => {
            setOpen(false);
            setHighlight(-1);
          }}
          placeholder="Search tenders, organizations, keywords…"
          aria-label="Search tenders"
          role="combobox"
          aria-expanded={showList}
          aria-autocomplete="list"
          aria-controls="search-suggestions"
          className="min-w-0 w-full rounded-xl border border-transparent bg-transparent py-2.5 pl-10 pr-3 text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none"
        />
        {showList ? (
          <ul
            id="search-suggestions"
            role="listbox"
            aria-label="Search suggestions"
            className="absolute left-0 right-0 top-full z-30 mt-2 max-h-72 overflow-auto rounded-xl border border-slate-800 bg-slate-900 py-1 shadow-xl shadow-slate-950/60"
          >
            {options.map((option, index) => {
              const [before, match, after] = splitMatch(option);
              return (
                <li key={option}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={index === highlight}
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseEnter={() => setHighlight(index)}
                    onClick={() => choose(option)}
                    className={`block w-full px-3 py-2 text-left text-sm text-slate-300 transition ${
                      index === highlight
                        ? "bg-sky-500/15 text-sky-200"
                        : "hover:bg-slate-800"
                    }`}
                  >
                    {before}
                    <span className="font-semibold text-sky-300">{match}</span>
                    {after}
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}
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
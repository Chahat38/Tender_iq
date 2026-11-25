import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

const FIELD_CLASS =
  "w-full rounded-lg border border-slate-700/80 bg-slate-900 px-3 py-2 text-sm text-slate-200 transition focus:border-sky-500 focus:outline-none disabled:cursor-not-allowed disabled:opacity-40";

interface SearchableSelectProps {
  /** Accessible name for the input and the listbox. */
  label: string;
  /** Selected option; "" means the clearing ("all") option. */
  value: string;
  options: string[];
  /** Label of the clearing option, also the empty-input placeholder. */
  allLabel: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}

interface Choice {
  value: string;
  label: string;
}

/**
 * Type-to-filter dropdown for long option lists (the city facet holds
 * hundreds of values, so a native select is unusable on mobile).
 *
 * Typing narrows the list, the "all" option clears the filter, and
 * Arrow/Enter/Escape work as a keyboard user expects. The option value
 * is always sent back exactly as the API returned it.
 */
export function SearchableSelect({
  label,
  value,
  options,
  allLabel,
  disabled = false,
  onChange,
}: SearchableSelectProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const listboxId = useId();

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
        setQuery("");
      }
    }
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  const choices = useMemo<Choice[]>(() => {
    const needle = query.trim().toLowerCase();
    const matched = needle
      ? options.filter((option) => option.toLowerCase().includes(needle))
      : options;
    return [
      { value: "", label: allLabel },
      ...matched.map((option) => ({ value: option, label: option })),
    ];
  }, [options, query, allLabel]);

  function choose(next: string) {
    onChange(next);
    setQuery("");
    setHighlight(0);
    setOpen(false);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setHighlight((index) => (index + 1) % choices.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      setHighlight((index) =>
        index <= 0 ? choices.length - 1 : index - 1,
      );
    } else if (event.key === "Enter") {
      const choice = choices[highlight];
      if (open && choice) {
        event.preventDefault();
        choose(choice.value);
      }
    } else if (event.key === "Escape") {
      setOpen(false);
      setQuery("");
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <input
        type="text"
        role="combobox"
        aria-label={label}
        aria-expanded={open}
        aria-controls={listboxId}
        aria-autocomplete="list"
        autoComplete="off"
        value={open ? query : value}
        placeholder={allLabel}
        disabled={disabled}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
          setHighlight(0);
        }}
        onFocus={() => {
          setOpen(true);
          setQuery("");
        }}
        onClick={() => {
          // A click on the already-focused input fires no focus event, so
          // reopen explicitly or the list stays shut after a pick.
          setOpen(true);
        }}
        onBlur={(event) => {
          if (
            !containerRef.current?.contains(event.relatedTarget as Node | null)
          ) {
            setOpen(false);
            setQuery("");
          }
        }}
        onKeyDown={handleKeyDown}
        className={`${FIELD_CLASS} pr-8`}
      />
      <svg
        viewBox="0 0 24 24"
        className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="m6 9 6 6 6-6" />
      </svg>
      {open ? (
        <ul
          id={listboxId}
          role="listbox"
          aria-label={label}
          className="absolute left-0 right-0 top-full z-30 mt-1 max-h-64 overflow-auto rounded-lg border border-slate-700 bg-slate-900 py-1 shadow-xl shadow-slate-950/60"
        >
          {choices.map((choice, index) => (
            <li key={choice.value || "__all__"}>
              <button
                type="button"
                role="option"
                aria-selected={choice.value === value}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setHighlight(index)}
                onClick={() => choose(choice.value)}
                className={`block w-full px-3 py-2 text-left text-sm transition ${
                  index === highlight
                    ? "bg-sky-500/15 text-sky-200"
                    : "text-slate-300 hover:bg-slate-800"
                }`}
              >
                {choice.label}
              </button>
            </li>
          ))}
          {choices.length === 1 ? (
            <li className="px-3 py-2 text-xs text-slate-500">No matches</li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}

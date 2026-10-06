interface PaginationProps {
  offset: number;
  limit: number;
  total: number;
  onPage: (offset: number) => void;
}

/** Prev/Next pager over limit/offset pages. */
export function Pagination({ offset, limit, total, onPage }: PaginationProps) {
  if (total === 0) {
    return null;
  }
  const first = offset + 1;
  const last = Math.min(offset + limit, total);
  const hasPrev = offset > 0;
  const hasNext = offset + limit < total;
  return (
    <nav
      aria-label="Pagination"
      className="flex items-center justify-between py-4 text-sm text-slate-300"
    >
      <span>
        Showing {first.toLocaleString("en-US")}–
        {last.toLocaleString("en-US")} of {total.toLocaleString("en-US")}
      </span>
      <div className="flex gap-2">
        <button
          type="button"
          disabled={!hasPrev}
          onClick={() => onPage(Math.max(0, offset - limit))}
          className="rounded-md border border-slate-700 px-3 py-1.5 enabled:cursor-pointer enabled:hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
        >
          ← Prev
        </button>
        <button
          type="button"
          disabled={!hasNext}
          onClick={() => onPage(offset + limit)}
          className="rounded-md border border-slate-700 px-3 py-1.5 enabled:cursor-pointer enabled:hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next →
        </button>
      </div>
    </nav>
  );
}

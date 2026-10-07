interface PaginationProps {
  offset: number;
  limit: number;
  total: number;
  onPage: (offset: number) => void;
}

const pageCount = (total: number, limit: number): number =>
  Math.max(1, Math.ceil(total / limit));

/** Prev/Next pager over limit/offset pages. */
export function Pagination({ offset, limit, total, onPage }: PaginationProps) {
  if (total === 0) {
    return null;
  }
  const first = offset + 1;
  const last = Math.min(offset + limit, total);
  const hasPrev = offset > 0;
  const hasNext = offset + limit < total;
  const pages = pageCount(total, limit);
  const currentPage = Math.floor(offset / limit) + 1;

  const jump =
    (page: number) =>
    () =>
      onPage((page - 1) * limit);

  return (
    <nav
      aria-label="Pagination"
      className="flex flex-wrap items-center justify-between gap-3 px-1 py-4 text-sm text-slate-400"
    >
      <span>
        Showing {first.toLocaleString("en-US")}–
        {last.toLocaleString("en-US")} of {total.toLocaleString("en-US")}
      </span>

      <div className="flex items-center gap-1.5">
        <button
          type="button"
          disabled={!hasPrev}
          onClick={() => onPage(Math.max(0, offset - limit))}
          aria-label="Previous page"
          className="rounded-lg border border-slate-700 px-3 py-1.5 font-medium transition enabled:cursor-pointer enabled:text-slate-200 enabled:hover:border-slate-600 enabled:hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
        >
          ← Prev
        </button>
        <span className="hidden items-center gap-1 px-1 sm:flex" aria-hidden="true">
          {Array.from({ length: pages }).map((_, index) => {
            const page = index + 1;
            const active = page === currentPage;
            return (
              <button
                key={page}
                type="button"
                onClick={jump(page)}
                aria-label={`Page ${page}`}
                className={`h-8 w-8 rounded-lg text-xs font-medium transition ${
                  active
                    ? "bg-gradient-to-r from-sky-500 to-indigo-500 text-white shadow-md shadow-sky-500/25"
                    : "text-slate-400 hover:bg-slate-800 hover:text-slate-200"
                }`}
              >
                {page}
              </button>
            );
          })}
        </span>
        <button
          type="button"
          disabled={!hasNext}
          onClick={() => onPage(offset + limit)}
          aria-label="Next page"
          className="rounded-lg border border-slate-700 px-3 py-1.5 font-medium transition enabled:cursor-pointer enabled:text-slate-200 enabled:hover:border-slate-600 enabled:hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next →
        </button>
      </div>
    </nav>
  );
}
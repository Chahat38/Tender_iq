import { formatDateTime } from "../utils/format";
import { StatusBadge } from "./StatusBadge";

/**
 * One display row. Works for both browse results (Opportunity) and
 * search hits (SearchHit) — App maps both into this view model.
 */
export interface TableRow {
  id: number;
  title: string | null;
  organization: string | null;
  referenceNumber: string | null;
  deadline: string | null;
  externalId: string;
  /** FTS5 snippet; only set for search hits. */
  snippet?: string | null;
}

interface OpportunityTableProps {
  rows: TableRow[];
  loading: boolean;
  onSelect: (id: number) => void;
}

function SkeletonRows() {
  return (
    <div className="space-y-3 p-4" aria-hidden="true">
      {Array.from({ length: 6 }).map((_, index) => (
        <div key={index} className="flex items-center gap-4">
          <div className="ts-skeleton h-4 w-2/3 rounded" />
          <div className="ts-skeleton h-4 w-1/6 rounded" />
          <div className="ts-skeleton h-4 w-24 rounded" />
        </div>
      ))}
    </div>
  );
}

/** Read-only tender list; clicking a row opens the detail view. */
export function OpportunityTable({
  rows,
  loading,
  onSelect,
}: OpportunityTableProps) {
  if (loading) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900/60">
        <SkeletonRows />
        <p className="sr-only">Loading…</p>
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900/60 px-6 py-16 text-center">
        <p className="text-sm text-slate-400">
          No tenders found. Try widening your filters or search terms.
        </p>
      </div>
    );
  }
  return (
    <div className="overflow-hidden rounded-2xl border border-slate-800 bg-slate-900/60 shadow-lg shadow-slate-950/30">
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-slate-800 bg-slate-900/90 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
              <th scope="col" className="px-5 py-3">
                Tender
              </th>
              <th scope="col" className="hidden px-4 py-3 md:table-cell">
                Organization
              </th>
              <th scope="col" className="px-5 py-3 text-right">
                Deadline
              </th>
              <th scope="col" className="sr-only">
                Open
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.id}
                onClick={() => onSelect(row.id)}
                className="group cursor-pointer border-b border-slate-800/60 transition last:border-b-0 hover:bg-slate-800/40"
              >
                <td className="px-5 py-4">
                  <div className="font-medium text-slate-100 transition group-hover:text-sky-200">
                    {row.title ?? "(untitled)"}
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-slate-500">
                    <span className="font-mono">{row.externalId}</span>
                    {row.referenceNumber ? (
                      <span className="hidden sm:inline">
                        · ref {row.referenceNumber}
                      </span>
                    ) : null}
                  </div>
                  {row.snippet ? (
                    <div className="mt-1.5 line-clamp-2 max-w-xl text-xs leading-relaxed text-slate-400">
                      {row.snippet}
                    </div>
                  ) : null}
                </td>
                <td className="hidden px-4 py-4 align-top text-slate-300 md:table-cell">
                  {row.organization ?? "—"}
                </td>
                <td className="px-5 py-4 text-right">
                  <div className="text-slate-300">
                    {formatDateTime(row.deadline)}
                  </div>
                  <div className="mt-1.5 inline-flex">
                    <StatusBadge deadline={row.deadline} />
                  </div>
                </td>
                <td className="pr-4 text-slate-600 transition group-hover:text-sky-400">
                  <svg
                    viewBox="0 0 24 24"
                    className="h-4 w-4"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden="true"
                  >
                    <path d="m9 18 6-6-6-6" />
                  </svg>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
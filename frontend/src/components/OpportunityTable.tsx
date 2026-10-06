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

/** Read-only tender list; clicking a row opens the detail view. */
export function OpportunityTable({
  rows,
  loading,
  onSelect,
}: OpportunityTableProps) {
  if (loading) {
    return <p className="py-16 text-center text-sm text-slate-400">Loading…</p>;
  }
  if (rows.length === 0) {
    return (
      <p className="py-16 text-center text-sm text-slate-400">
        No tenders found.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-slate-800 bg-slate-900/80 text-xs uppercase tracking-wide text-slate-400">
            <th scope="col" className="px-4 py-3 font-medium">
              Tender
            </th>
            <th scope="col" className="px-4 py-3 font-medium">
              Organization
            </th>
            <th scope="col" className="px-4 py-3 font-medium">
              Deadline
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.id}
              onClick={() => onSelect(row.id)}
              className="cursor-pointer border-b border-slate-800/60 last:border-b-0 hover:bg-slate-900"
            >
              <td className="px-4 py-3">
                <div className="font-medium text-slate-100">
                  {row.title ?? "(untitled)"}
                </div>
                <div className="mt-0.5 text-xs text-slate-500">
                  {row.externalId}
                  {row.referenceNumber ? ` · ref ${row.referenceNumber}` : ""}
                </div>
                {row.snippet ? (
                  <div className="mt-1 line-clamp-2 max-w-xl text-xs text-slate-400">
                    {row.snippet}
                  </div>
                ) : null}
              </td>
              <td className="px-4 py-3 align-top text-slate-300">
                {row.organization ?? "—"}
              </td>
              <td className="px-4 py-3 align-top">
                <div className="text-slate-300">
                  {formatDateTime(row.deadline)}
                </div>
                <div className="mt-1">
                  <StatusBadge deadline={row.deadline} />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

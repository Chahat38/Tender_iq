import { useEffect, useState } from "react";
import { ApiError, getOpportunity } from "../api";
import type { OpportunityDetail as OpportunityDetailData } from "../types";
import { formatDateTime } from "../utils/format";
import { StatusBadge } from "./StatusBadge";

interface OpportunityDetailProps {
  id: number;
  onBack: () => void;
}

function Field({
  label,
  value,
}: {
  label: string;
  value: string | null | undefined;
}) {
  const shown = value !== null && value !== undefined && value.trim() ? value : "—";
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-500">
        {label}
      </dt>
      <dd className="mt-0.5 text-sm break-words text-slate-200">{shown}</dd>
    </div>
  );
}

/** Full record view: normalized fields, documents and update history. */
export function OpportunityDetail({ id, onBack }: OpportunityDetailProps) {
  const [detail, setDetail] = useState<OpportunityDetailData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null);
    setError(null);
    getOpportunity(id, controller.signal)
      .then(setDetail)
      .catch((err: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setError(err instanceof ApiError ? err.detail : String(err));
      });
    return () => controller.abort();
  }, [id]);

  if (error !== null) {
    return (
      <section
        role="alert"
        className="rounded-lg border border-rose-500/40 bg-rose-500/10 p-4 text-sm text-rose-200"
      >
        Failed to load opportunity: {error}
        <button
          type="button"
          onClick={onBack}
          className="ml-3 rounded border border-rose-500/40 px-2 py-0.5 hover:bg-rose-500/20"
        >
          Back
        </button>
      </section>
    );
  }
  if (detail === null) {
    return <p className="py-16 text-center text-sm text-slate-400">Loading…</p>;
  }
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-6">
      <button
        type="button"
        onClick={onBack}
        className="mb-4 rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
      >
        ← Back to list
      </button>
      <h2 className="text-xl font-semibold text-slate-100">
        {detail.title ?? "(untitled)"}
      </h2>
      <p className="mt-1 text-sm text-slate-400">
        {detail.organization ?? "—"} · {detail.external_id}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <StatusBadge deadline={detail.deadline} />
        <a
          href={detail.source_url}
          target="_blank"
          rel="noreferrer"
          className="text-sm text-sky-400 hover:underline"
        >
          View on source ↗
        </a>
      </div>
      <dl className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Field label="Reference no." value={detail.reference_number} />
        <Field label="Deadline" value={formatDateTime(detail.deadline)} />
        <Field label="Published" value={formatDateTime(detail.published_at)} />
        <Field label="Location" value={detail.location} />
        <Field label="Category" value={detail.category} />
        <Field
          label="Procurement type"
          value={detail.procurement_type}
        />
        <Field label="First seen" value={formatDateTime(detail.first_seen_at)} />
        <Field label="Last seen" value={formatDateTime(detail.last_seen_at)} />
        <Field
          label="Last verified"
          value={formatDateTime(detail.last_verified_at)}
        />
      </dl>
      {detail.description ? (
        <div className="mt-6">
          <h3 className="text-xs uppercase tracking-wide text-slate-500">
            Description
          </h3>
          <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-slate-300">
            {detail.description}
          </p>
        </div>
      ) : null}
      <div className="mt-6">
        <h3 className="text-xs uppercase tracking-wide text-slate-500">
          Documents ({detail.documents.length})
        </h3>
        {detail.documents.length === 0 ? (
          <p className="mt-2 text-sm text-slate-400">
            No documents recorded.
          </p>
        ) : (
          <ul className="mt-2 space-y-1">
            {detail.documents.map((document) => (
              <li key={document.id}>
                <a
                  href={document.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-sm break-all text-sky-400 hover:underline"
                >
                  📄 {document.filename ?? document.url}
                </a>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="mt-6">
        <h3 className="text-xs uppercase tracking-wide text-slate-500">
          Update history ({detail.updates.length})
        </h3>
        {detail.updates.length === 0 ? (
          <p className="mt-2 text-sm text-slate-400">
            No changes recorded since first collection.
          </p>
        ) : (
          <ul className="mt-2 space-y-2">
            {detail.updates.map((update) => (
              <li
                key={update.id}
                className="rounded-md border border-slate-800 bg-slate-900 p-3 text-sm"
              >
                <span className="font-medium text-slate-200">
                  {update.field}
                </span>{" "}
                <span className="text-slate-500">({update.update_type})</span>
                <div className="mt-1 text-slate-400">
                  <span className="line-through">
                    {update.old_value ?? "—"}
                  </span>
                  {" → "}
                  <span className="text-slate-200">
                    {update.new_value ?? "—"}
                  </span>
                </div>
                <div className="mt-1 text-xs text-slate-500">
                  {formatDateTime(update.detected_at)}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

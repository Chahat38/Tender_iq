import { useEffect, useState } from "react";
import { ApiError, getDuplicates, getOpportunity } from "../api";
import type { DuplicateLink, OpportunityDetail as OpportunityDetailData } from "../types";
import { daysUntil, formatDateTime } from "../utils/format";
import { AiAssistPanel } from "./AiAssistPanel";
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

/** Full record view: normalized fields, documents, duplicate listings and update history. */
export function OpportunityDetail({ id, onBack }: OpportunityDetailProps) {
  const [detail, setDetail] = useState<OpportunityDetailData | null>(null);
  const [duplicates, setDuplicates] = useState<DuplicateLink[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null);
    setDuplicates([]);
    setError(null);
    getOpportunity(id, controller.signal)
      .then(setDetail)
      .catch((err: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setError(err instanceof ApiError ? err.detail : String(err));
      });
    getDuplicates(id, controller.signal)
      .then(setDuplicates)
      .catch(() => setDuplicates([]));
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
  const remainingDays = daysUntil(detail?.deadline ?? null);
  const countdownLabel =
    remainingDays === null
      ? null
      : remainingDays === 0
        ? "Closes today"
        : remainingDays < 0
          ? `Deadline passed ${-remainingDays} day${-remainingDays === 1 ? "" : "s"} ago`
          : `${remainingDays} day${remainingDays === 1 ? "" : "s"} remaining`;

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
          Open official source ↗
        </a>
      </div>
      <dl className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Field label="Reference no." value={detail.reference_number} />
        <Field label="Deadline" value={formatDateTime(detail.deadline)} />
        {countdownLabel !== null ? (
          <Field label="Countdown" value={countdownLabel} />
        ) : null}
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
      {duplicates.length >= 2 ? (
        <div className="mt-6 rounded-md border border-amber-500/30 bg-amber-500/5 p-4">
          <h3 className="text-xs uppercase tracking-wide text-amber-300">
            Also listed on {duplicates.length} other portals
          </h3>
          <ul className="mt-2 space-y-1">
            {duplicates.map((duplicate) => (
              <li key={duplicate.id} className="text-sm text-slate-300">
                <a
                  href={duplicate.source_url ?? undefined}
                  target="_blank"
                  rel="noreferrer"
                  className={duplicate.source_url ? "text-sky-400 hover:underline" : undefined}
                >
                  {duplicate.source_name ?? "Unknown source"}
                </a>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-slate-500">
            This is one notice published by several portals — use AI Assist to
            confirm the match.
          </p>
        </div>
      ) : null}
      <AiAssistPanel opportunityId={detail.id} duplicates={duplicates} />
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

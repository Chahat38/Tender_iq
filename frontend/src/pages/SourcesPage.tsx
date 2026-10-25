import { useEffect, useState } from "react";
import { listCoverage, listSources } from "../api";
import type { Source, SourceCoverage } from "../types";
import { formatDateTime } from "../utils/format";

const STATUS_STYLES: Record<string, string> = {
  Active: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  "Login Required": "border-amber-500/40 bg-amber-500/10 text-amber-300",
  Restricted: "border-rose-500/40 bg-rose-500/10 text-rose-200",
  Failed: "border-rose-500/40 bg-rose-500/10 text-rose-200",
  "Not Tested": "border-slate-600/60 bg-slate-800/60 text-slate-300",
};

function statusDot(status: string): string {
  if (status === "Active") return "bg-emerald-400";
  if (status === "Login Required") return "bg-amber-400";
  if (status === "Restricted" || status === "Failed") return "bg-rose-400";
  return "bg-slate-400";
}

function StatusTag({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${
        STATUS_STYLES[status] ?? STATUS_STYLES["Not Tested"]
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${statusDot(status)}`} />
      {status}
    </span>
  );
}

function sourceStatus(source: Source): string {
  if (source.active !== 1) return "Not Tested";
  if (source.requires_authentication === 1) return "Login Required";
  if (source.robots_allowed === 0) return "Restricted";
  if (source.last_status === "failed" || (source.consecutive_failures ?? 0) > 0) {
    return "Failed";
  }
  if (source.last_status === "ok") return "Active";
  return "Not Tested";
}

function SourceCard({ source }: { source: Source }) {
  const status = sourceStatus(source);
  return (
    <div className="group rounded-2xl border border-slate-800 bg-slate-900/60 p-5 transition hover:border-slate-700 hover:bg-slate-900">
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-semibold text-slate-100">{source.name}</h3>
        <StatusTag status={status} />
      </div>
      <p className="mt-1.5 font-mono text-[11px] text-slate-500">
        {source.adapter_type}
      </p>
      <div className="mt-3 grid grid-cols-2 gap-2 border-t border-slate-800 pt-3 text-xs text-slate-400">
        <div>
          <div className="text-[10px] uppercase tracking-wide text-slate-500">
            Tenders
          </div>
          <div className="mt-0.5 font-mono text-sm text-slate-200">
            {source.opportunity_count.toLocaleString()}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide text-slate-500">
            Last successful crawl
          </div>
          <div className="mt-0.5">
            {source.last_success_at
              ? formatDateTime(source.last_success_at)
              : "No successful crawl"}
          </div>
        </div>
      </div>
      {source.crawl_frequency ? (
        <p className="mt-2 text-[11px] text-slate-500">
          Frequency: {source.crawl_frequency}
        </p>
      ) : null}
      {source.last_error ? (
        <p className="mt-2 line-clamp-2 text-[11px] text-rose-300/80">
          Last error: {source.last_error}
        </p>
      ) : null}
    </div>
  );
}

function CoverageCard({ item }: { item: SourceCoverage }) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/40 p-5">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h4 className="font-semibold text-slate-100">{item.requested_name}</h4>
          {item.source_name ? (
            <p className="mt-0.5 text-xs text-slate-500">{item.source_name}</p>
          ) : null}
        </div>
        <StatusTag status={item.status} />
      </div>
      <p className="mt-3 text-sm leading-6 text-slate-400">{item.note}</p>
      {item.source_id !== null ? (
        <p className="mt-2 text-xs text-slate-500">
          {item.opportunity_count ?? 0} stored ·{" "}
          {item.last_success_at
            ? `last success ${formatDateTime(item.last_success_at)}`
            : "no successful crawl"}
        </p>
      ) : null}
      {item.url ? (
        <a
          className="mt-3 inline-block text-sm font-medium text-sky-300 transition hover:text-sky-200"
          href={item.url}
          target="_blank"
          rel="noreferrer"
        >
          Official page &rarr;
        </a>
      ) : null}
    </div>
  );
}

/** Registered portals, their latest health, and the requested coverage matrix. */
export function SourcesPage() {
  const [sources, setSources] = useState<Source[]>([]);
  const [coverage, setCoverage] = useState<SourceCoverage[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      listSources(controller.signal),
      listCoverage(controller.signal),
    ])
      .then(([nextSources, nextCoverage]) => {
        setSources(nextSources);
        setCoverage(nextCoverage);
      })
      .catch((err: unknown) => setError(String(err)));
    return () => controller.abort();
  }, []);

  return (
    <section>
      <div>
        <h2 className="text-xl font-bold tracking-tight text-slate-50">
          Sources
        </h2>
        <p className="mt-1 max-w-3xl text-sm text-slate-400">
          Every notice is collected from an official portal or a clearly
          labelled public aggregator whose robots/terms allow polite access.
          Blocked and login-only sources stay visible in the coverage matrix but
          never appear as empty tender filters.
        </p>
      </div>
      {error !== null ? (
        <div
          role="alert"
          className="mt-4 rounded-xl border border-rose-500/40 bg-rose-500/10 p-4 text-sm text-rose-200"
        >
          {error}
        </div>
      ) : sources.length === 0 ? (
        <p className="py-16 text-center text-sm text-slate-400">Loading…</p>
      ) : (
        <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {sources.map((source) => (
            <SourceCard key={source.id} source={source} />
          ))}
        </div>
      )}
      {coverage.length > 0 ? (
        <div className="mt-8">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <div>
              <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-400">
                Requested source coverage
              </h3>
              <p className="mt-1 text-xs text-slate-500">
                All requested organizations, including sources that cannot be
                crawled or have no public tender listing.
              </p>
            </div>
            <span className="text-xs text-slate-500">
              {coverage.length} requested
            </span>
          </div>
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {coverage.map((item) => (
              <CoverageCard key={item.key} item={item} />
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}

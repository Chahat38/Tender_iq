import { useEffect, useState } from "react";
import { listSources } from "../api";
import type { Source } from "../types";
import { formatDateTime } from "../utils/format";

function ComplianceTag({
  active,
  robots,
  terms,
  auth,
}: {
  active: number | boolean | null;
  robots: number | boolean | null;
  terms: number | boolean | null;
  auth: number | boolean | null;
}) {
  const model = { active, robots, terms, auth };
  const ok = model.active === 1 && model.robots === 1 && model.terms === 1 && model.auth === 0;
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${
        ok
          ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
          : "border-amber-500/40 bg-amber-500/10 text-amber-300"
      }`}
    >
      {ok ? "Verified" : "Paused"}
    </span>
  );
}

/** Registered portals, their adapter and last crawl; duplicates on them are collapsed. */
export function SourcesPage() {
  const [sources, setSources] = useState<Source[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    listSources(controller.signal)
      .then(setSources)
      .catch((err: unknown) => setError(String(err)));
    return () => controller.abort();
  }, []);

  return (
    <section>
      <h2 className="text-xl font-semibold text-slate-100">Sources</h2>
      <p className="mt-1 max-w-2xl text-sm text-slate-400">
        Every notice is collected from an official portal that publishes
        tenders publicly and whose robots/terms allow it. When the same notice
        appears on several portals it is stored once and shown once.
      </p>
      {error !== null ? (
        <div
          role="alert"
          className="mt-4 rounded-lg border border-rose-500/40 bg-rose-500/10 p-4 text-sm text-rose-200"
        >
          {error}
        </div>
      ) : sources.length === 0 ? (
        <p className="py-16 text-center text-sm text-slate-400">Loading…</p>
      ) : (
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {sources.map((source) => (
            <div
              key={source.id}
              className="rounded-lg border border-slate-800 bg-slate-900/60 p-4"
            >
              <div className="flex items-start justify-between gap-2">
                <h3 className="font-medium text-slate-100">{source.name}</h3>
                <ComplianceTag
                  active={source.active}
                  robots={source.robots_allowed}
                  terms={source.terms_checked}
                  auth={source.requires_authentication}
                />
              </div>
              <p className="mt-1 text-xs text-slate-500">{source.adapter_type}</p>
              <p className="mt-2 text-xs text-slate-400">
                {source.last_crawled_at
                  ? `Last crawl ${formatDateTime(source.last_crawled_at)}`
                  : "Never crawled yet"}
                {source.crawl_frequency
                  ? ` · every ${source.crawl_frequency}h`
                  : ""}
              </p>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
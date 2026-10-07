import { useEffect, useState } from "react";
import { ApiError, getNotifications } from "../api";
import type { NotificationsPage as NotificationsPageData } from "../types";
import { formatDateTime } from "../utils/format";
import { OpportunityTable as OpportunityTable } from "../components/OpportunityTable";
import type { TableRow } from "../components/OpportunityTable";

export type NotificationsWindow = 1 | 3 | 7 | 30;

function toRows(items: NotificationsPageData["new_tenders"]): TableRow[] {
  return items.map((opportunity) => ({
    id: opportunity.id,
    title: opportunity.title,
    organization: opportunity.organization,
    referenceNumber: opportunity.reference_number,
    deadline: opportunity.deadline,
    externalId: opportunity.external_id,
    snippet: null,
  }));
}

const WINDOWS: { value: NotificationsWindow; label: string }[] = [
  { value: 1, label: "Last 1 day" },
  { value: 3, label: "Last 3 days" },
  { value: 7, label: "Last 7 days" },
  { value: 30, label: "Last 30 days" },
];

/**
 * Two live feeds: tenders first collected within the window (new) and
 * tenders whose deadline falls within the window (closing soon). Rows
 * link to the same detail view used by browsing. Also offers a browser
 * Notification() subscribe button — purely client-side, optional.
 */
export function NotificationsPage({
  onSelectTender,
}: {
  onSelectTender: (id: number) => void;
}) {
  const [windowDays, setWindowDays] = useState<NotificationsWindow>(7);
  const [data, setData] = useState<NotificationsPageData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [permission, setPermission] = useState<NotificationPermission>(
    typeof Notification === "undefined" ? "denied" : Notification.permission,
  );

  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError(null);
    getNotifications(windowDays, 20, controller.signal)
      .then(setData)
      .catch((err: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setError(err instanceof ApiError ? err.detail : String(err));
      });
    return () => controller.abort();
  }, [windowDays]);

  async function handleSubscribe() {
    if (typeof Notification !== "undefined" && permission !== "granted") {
      const result = await Notification.requestPermission();
      setPermission(result);
    }
    if (resultGranted()) {
      new Notification("TenderScout AI", {
        body: "You'll get browser notifications for new and closing-soon tenders.",
      });
    }
  }

  function resultGranted(): boolean {
    return (
      typeof Notification !== "undefined" &&
      (permission === "granted" || Notification.permission === "granted")
    );
  }

  return (
    <section>
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-slate-50">
            Notifications
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-slate-400">
            Tenders new to the tracker and tenders closing soon. Widen the
            window to see more.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm text-slate-400">
            Window
            <select
              value={windowDays}
              onChange={(event) =>
                setWindowDays(Number(event.target.value) as NotificationsWindow)
              }
              aria-label="Time window"
              className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 focus:border-sky-500 focus:outline-none"
            >
              {WINDOWS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          {resultGranted() ? (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-300">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
              Browser alerts on
            </span>
          ) : (
            <button
              type="button"
              onClick={handleSubscribe}
              className="rounded-lg bg-gradient-to-r from-sky-500 to-indigo-500 px-4 py-2 text-sm font-semibold text-white shadow-md shadow-sky-500/25 transition hover:from-sky-400 hover:to-indigo-400"
            >
              Enable browser alerts
            </button>
          )}
        </div>
      </div>

      {error !== null ? (
        <div
          role="alert"
          className="mt-4 rounded-xl border border-rose-500/40 bg-rose-500/10 p-4 text-sm text-rose-200"
        >
          {error}
        </div>
      ) : data === null ? (
        <p className="py-16 text-center text-sm text-slate-400">Loading…</p>
      ) : (
        <div className="mt-6 space-y-8">
          <section>
            <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              New tenders ({data.new_total})
            </h3>
            <div className="mt-2">
              <OpportunityTable
                rows={toRows(data.new_tenders)}
                loading={false}
                onSelect={onSelectTender}
              />
            </div>
          </section>
          <section>
            <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              Closing soon ({data.closing_total})
            </h3>
            <div className="mt-2">
              <OpportunityTable
                rows={toRows(data.closing_soon)}
                loading={false}
                onSelect={onSelectTender}
              />
            </div>
          </section>
          <p className="text-xs text-slate-500">
            Generated {formatDateTime(data.generated_at)} · duplicates are
            collapsed, so each notice appears once.
          </p>
        </div>
      )}
    </section>
  );
}
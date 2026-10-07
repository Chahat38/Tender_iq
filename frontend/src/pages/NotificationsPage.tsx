import { useEffect, useState } from "react";
import { ApiError, getNotifications } from "../api";
import type { NotificationsPage as NotificationsPageData } from "../types";
import { formatDateTime } from "../utils/format";
import { OpportunityTable, type TableRow } from "../components/OpportunityTable";

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
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold text-slate-100">
            Notifications
          </h2>
          <p className="mt-1 text-sm text-slate-400">
            Tenders new to the tracker and tenders closing soon. Widen the
            window to see more.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <select
            value={windowDays}
            onChange={(event) =>
              setWindowDays(Number(event.target.value) as NotificationsWindow)
            }
            aria-label="Time window"
            className="rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200"
          >
            <option value={1}>Last 1 day</option>
            <option value={3}>Last 3 days</option>
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
          </select>
          {resultGranted() ? (
            <span className="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-300">
              Browser alerts on
            </span>
          ) : (
            <button
              type="button"
              onClick={handleSubscribe}
              className="rounded-md bg-sky-600 px-3 py-2 text-sm font-medium text-white hover:bg-sky-500"
            >
              Enable browser alerts
            </button>
          )}
        </div>
      </div>

      {error !== null ? (
        <div
          role="alert"
          className="rounded-lg border border-rose-500/40 bg-rose-500/10 p-4 text-sm text-rose-200"
        >
          {error}
        </div>
      ) : data === null ? (
        <p className="py-16 text-center text-sm text-slate-400">Loading…</p>
      ) : (
        <>
          <h3 className="mt-2 text-xs uppercase tracking-wide text-slate-500">
            New tenders ({data.new_total})
          </h3>
          <div className="mt-2">
            <OpportunityTable
              rows={toRows(data.new_tenders)}
              loading={false}
              onSelect={onSelectTender}
            />
          </div>
          <h3 className="mt-6 text-xs uppercase tracking-wide text-slate-500">
            Closing soon ({data.closing_total})
          </h3>
          <div className="mt-2">
            <OpportunityTable
              rows={toRows(data.closing_soon)}
              loading={false}
              onSelect={onSelectTender}
            />
          </div>
          <p className="mt-4 text-xs text-slate-500">
            Generated {formatDateTime(data.generated_at)} · duplicates are
            collapsed, so each notice appears once.
          </p>
        </>
      )}
    </section>
  );
}
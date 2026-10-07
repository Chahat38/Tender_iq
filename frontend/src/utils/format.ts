/**
 * Small pure formatting helpers shared by the UI. Kept dependency-free
 * so they are trivially unit-testable.
 */

export type DeadlineTone = "danger" | "warn" | "ok" | "muted";

export interface DeadlineStatus {
  label: string;
  tone: DeadlineTone;
}

const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * Format a stored datetime for display.
 *
 * Stored values keep the source's own offset (PKT, +05:00 for PPRA);
 * the wall time is shown as published rather than re-expressed in the
 * viewer's timezone. ``null``/empty renders as an em dash, and a value
 * that does not parse is returned untouched instead of guessed at.
 */
export function formatDateTime(iso: string | null | undefined): string {
  if (iso === null || iso === undefined) {
    return "—";
  }
  const trimmed = iso.trim();
  if (!trimmed) {
    return "—";
  }
  if (Number.isNaN(Date.parse(trimmed))) {
    return trimmed;
  }
  return trimmed
    .replace("T", " ")
    .replace(/(Z|[+-]\d{2}:?\d{2})$/, "")
    .trim();
}

/**
 * Classify a deadline relative to ``now`` (defaults to the real clock;
 * tests pass a fixed date). Missing or unparseable deadlines are
 * "muted", never inferred.
 */
export function deadlineStatus(
  deadline: string | null | undefined,
  now: Date = new Date(),
): DeadlineStatus {
  if (deadline === null || deadline === undefined || !deadline.trim()) {
    return { label: "No deadline", tone: "muted" };
  }
  const at = Date.parse(deadline);
  if (Number.isNaN(at)) {
    return { label: "No deadline", tone: "muted" };
  }
  const diffMs = at - now.getTime();
  if (diffMs < 0) {
    return { label: "Closed", tone: "danger" };
  }
  const days = Math.ceil(diffMs / DAY_MS);
  const unit = days === 1 ? "1 day" : `${days} days`;
  return {
    label: `${unit} left`,
    tone: days <= 7 ? "warn" : "ok",
  };
}

/**
 * Whole 24-hour periods until ``deadline`` (floored, so anything later
 * today is 0 and a deadline 25h away is 1). Returns null for
 * missing/unparseable dates and negative for past deadlines. Purely
 * presentational countdown data; unlike deadlineStatus it never rounds
 * a partial day up.
 */
export function daysUntil(
  deadline: string | null | undefined,
  now: Date = new Date(),
): number | null {
  if (deadline === null || deadline === undefined || !deadline.trim()) {
    return null;
  }
  const at = Date.parse(deadline);
  if (Number.isNaN(at)) {
    return null;
  }
  return Math.floor((at - now.getTime()) / DAY_MS);
}

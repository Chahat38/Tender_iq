import type { DeadlineTone } from "../utils/format";
import { deadlineStatus } from "../utils/format";

const TONE_CLASSES: Record<DeadlineTone, string> = {
  danger: "border-rose-500/40 bg-rose-500/10 text-rose-300",
  warn: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  ok: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  muted: "border-slate-700 bg-slate-800/60 text-slate-400",
};

/** Colored chip conveying how soon a tender closes. */
export function StatusBadge({
  deadline,
}: {
  deadline: string | null | undefined;
}) {
  const status = deadlineStatus(deadline);
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap ${TONE_CLASSES[status.tone]}`}
    >
      {status.label}
    </span>
  );
}

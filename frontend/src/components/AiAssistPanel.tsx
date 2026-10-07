import { useState } from "react";
import { aiAnalyze, aiDedup, aiGenerateRfp, ApiError } from "../api";
import type {
  AiAnalysis,
  AiDedupResult,
  AiRfpDraft,
  DuplicateLink,
} from "../types";

interface AiAssistPanelProps {
  opportunityId: number;
  duplicates: DuplicateLink[];
}

function SectionList({ items }: { items: string[] }) {
  if (items.length === 0) {
    return <p className="text-sm text-slate-500">None detected.</p>;
  }
  return (
    <ul className="list-inside list-disc space-y-1">
      {items.map((item, index) => (
        <li key={index} className="text-sm text-slate-300">
          {item}
        </li>
      ))}
    </ul>
  );
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    return `API error ${err.status}: ${err.detail}`;
  }
  return String(err);
}

/** AI assistant for one tender, shown beside the record in a copilot column. */
export function AiAssistPanel({ opportunityId, duplicates }: AiAssistPanelProps) {
  const [analysis, setAnalysis] = useState<AiAnalysis | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysisError, setAnalysisError] = useState<string | null>(null);

  const [dedup, setDedup] = useState<AiDedupResult | null>(null);
  const [checkingDuplicates, setCheckingDuplicates] = useState(false);
  const [dedupError, setDedupError] = useState<string | null>(null);

  const [companyName, setCompanyName] = useState("");
  const [capabilities, setCapabilities] = useState("");
  const [draft, setDraft] = useState<AiRfpDraft | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);

  const duplicateIds = duplicates.map((duplicate) => duplicate.id);

  function handleAnalyze() {
    setAnalyzing(true);
    setAnalysisError(null);
    aiAnalyze(opportunityId)
      .then(setAnalysis)
      .catch((err: unknown) => setAnalysisError(errorText(err)))
      .finally(() => setAnalyzing(false));
  }

  function handleCheckDuplicates() {
    if (duplicateIds.length < 2) {
      return;
    }
    setCheckingDuplicates(true);
    setDedupError(null);
    aiDedup(duplicateIds)
      .then(setDedup)
      .catch((err: unknown) => setDedupError(errorText(err)))
      .finally(() => setCheckingDuplicates(false));
  }

  function handleDraft() {
    setDrafting(true);
    setDraftError(null);
    aiGenerateRfp(
      opportunityId,
      companyName.trim(),
      capabilities.trim().split(/\n+/).filter(Boolean),
    )
      .then(setDraft)
      .catch((err: unknown) => setDraftError(errorText(err)))
      .finally(() => setDrafting(false));
  }

  return (
    <section className="rounded-2xl border border-violet-500/25 bg-gradient-to-b from-violet-500/[0.08] to-transparent p-5">
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-violet-300">
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
          <path d="M12 3l1.9 5.7a2 2 0 0 0 1.3 1.3L21 12l-5.8 1.9a2 2 0 0 0-1.3 1.3L12 21l-1.9-5.8a2 2 0 0 0-1.3-1.3L3 12l5.8-1.9a2 2 0 0 0 1.3-1.3L12 3z" />
        </svg>
        AI Assist
      </h3>

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={handleAnalyze}
          disabled={analyzing}
          className="rounded-lg border border-violet-500/40 bg-violet-500/10 px-3 py-2 text-sm font-medium text-violet-200 transition enabled:hover:bg-violet-500/20 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {analyzing ? "Analyzing…" : "Analyze this tender"}
        </button>
        {duplicates.length >= 2 ? (
          <button
            type="button"
            onClick={handleCheckDuplicates}
            disabled={checkingDuplicates}
            className="rounded-lg border border-violet-500/40 bg-violet-500/10 px-3 py-2 text-sm font-medium text-violet-200 transition enabled:hover:bg-violet-500/20 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {checkingDuplicates
              ? "Checking…"
              : "Verify these are the same tender"}
          </button>
        ) : null}
      </div>

      {analysisError !== null ? (
        <p role="alert" className="mt-3 text-sm text-rose-300">
          {analysisError}
        </p>
      ) : null}

      {analysis !== null ? (
        <div className="mt-4 space-y-3">
          <p className="text-sm leading-relaxed text-slate-200">
            {analysis.summary}
          </p>
          <div className="grid grid-cols-1 gap-3">
            <div>
              <h4 className="text-xs font-medium text-slate-400">
                Requirements
              </h4>
              <SectionList items={analysis.requirements} />
            </div>
            <div>
              <h4 className="text-xs font-medium text-slate-400">Fit hints</h4>
              <SectionList items={analysis.fit_hints} />
            </div>
            <div>
              <h4 className="text-xs font-medium text-slate-400">Keywords</h4>
              <SectionList items={analysis.keywords} />
            </div>
            <div>
              <h4 className="text-xs font-medium text-slate-400">Red flags</h4>
              <SectionList items={analysis.red_flags} />
            </div>
          </div>
          {analysis.llm_available ? (
            <p className="text-xs text-slate-500">
              Prose summary by {analysis.provider} ({analysis.model}).
            </p>
          ) : (
            <p className="text-xs text-slate-500">
              Deterministic analysis only — add a Grok API key server-side to
              enable the LLM summary.
            </p>
          )}
        </div>
      ) : null}

      {dedupError !== null ? (
        <p role="alert" className="mt-3 text-sm text-rose-300">
          {dedupError}
        </p>
      ) : null}

      {dedup !== null ? (
        <div className="mt-4 rounded-lg border border-slate-700 bg-slate-900/60 p-3 text-sm">
          <p className="font-medium text-slate-100">
            {dedup.same_tender
              ? `Same tender — ${Math.round(dedup.confidence * 100)}% confidence.`
              : `Different tenders — ${Math.round(dedup.confidence * 100)}% confidence.`}
          </p>
          <p className="mt-1 text-slate-300">{dedup.reason}</p>
          {dedup.same_tender ? (
            <p className="mt-1 text-xs text-slate-400">
              Canonical deadline: {dedup.canonical.deadline} ·{" "}
              <a
                href={dedup.canonical.source_url}
                target="_blank"
                rel="noreferrer"
                className="text-sky-400 hover:underline"
              >
                official source ↗
              </a>
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="mt-5 border-t border-violet-500/20 pt-4">
        <h4 className="text-sm font-medium text-slate-200">
          Draft a bid document
        </h4>
        <div className="mt-2 grid grid-cols-1 gap-2">
          <input
            type="text"
            value={companyName}
            onChange={(event) => setCompanyName(event.target.value)}
            placeholder="Your company name"
            aria-label="Company name"
            maxLength={120}
            className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none"
          />
          <textarea
            value={capabilities}
            onChange={(event) => setCapabilities(event.target.value)}
            placeholder="Capabilities, one per line (e.g. Electrical works)"
            aria-label="Company capabilities"
            rows={3}
            className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={handleDraft}
          disabled={drafting || !companyName.trim()}
          className="mt-2 rounded-lg bg-gradient-to-r from-violet-600 to-indigo-600 px-4 py-2 text-sm font-medium text-white transition enabled:hover:from-violet-500 enabled:hover:to-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {drafting ? "Drafting…" : "Draft document"}
        </button>
        {draftError !== null ? (
          <p role="alert" className="mt-2 text-sm text-rose-300">
            {draftError}
          </p>
        ) : null}
        {draft !== null ? (
          <div className="mt-3 space-y-3 rounded-lg border border-slate-700 bg-slate-900/60 p-4">
            <p className="font-medium text-slate-100">{draft.document_title}</p>
            <p className="text-sm leading-relaxed text-slate-300">
              {draft.introduction}
            </p>
            <div>
              <h5 className="text-xs font-medium text-slate-400">
                Eligibility
              </h5>
              <SectionList items={draft.eligibility} />
            </div>
            <div>
              <h5 className="text-xs font-medium text-slate-400">
                Technical approach
              </h5>
              <SectionList items={draft.technical_approach} />
            </div>
            <p className="text-sm text-slate-300">{draft.pricing_note}</p>
            <div>
              <h5 className="text-xs font-medium text-slate-400">
                Submission checklist
              </h5>
              <SectionList items={draft.submission_checklist} />
            </div>
            <p className="text-xs text-slate-500">
              {draft.coverage_disclaimer}
            </p>
          </div>
        ) : null}
      </div>
    </section>
  );
}
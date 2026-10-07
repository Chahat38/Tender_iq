export type PageName = "home" | "notifications" | "sources";

interface NavBarProps {
  page: PageName;
  onNavigate: (hash: string) => void;
}

const LINKS: { page: PageName; hash: string; label: string }[] = [
  { page: "home", hash: "#/tenders", label: "Tenders" },
  { page: "notifications", hash: "#/notifications", label: "Notifications" },
  { page: "sources", hash: "#/sources", label: "Sources" },
];

/** Sticky glass header with brand mark, nav pills and a live indicator. */
export function NavBar({ page, onNavigate }: NavBarProps) {
  return (
    <header className="sticky top-0 z-40 border-b border-slate-800/70 bg-slate-950/85 backdrop-blur">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-6 py-3.5">
        <button
          type="button"
          onClick={() => onNavigate("#/tenders")}
          className="group flex items-center gap-3 text-left"
        >
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-sky-500 to-indigo-600 shadow-lg shadow-sky-500/25 transition group-hover:shadow-sky-400/40">
            <svg
              viewBox="0 0 24 24"
              className="h-5 w-5 text-white"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M12 3v18" />
              <path d="M5 21h14" />
              <path d="M6 7h3l2-2 2 2h5" />
              <path d="M6 12h3l3-3 1 1-4 4H6z" opacity="0" />
              <path d="M4 12h16v3H4z" />
              <path d="M7 12v5M12 12v5M17 12v5" />
            </svg>
          </span>
          <span>
            <span className="block text-lg font-bold tracking-tight text-slate-100 transition group-hover:text-sky-300">
              TenderScout AI
            </span>
            <span className="block text-[11px] font-medium tracking-wide text-slate-500">
              Pakistan public procurement
            </span>
          </span>
        </button>

        <div className="flex flex-wrap items-center gap-2.5">
          <nav
            aria-label="Main"
            className="flex gap-1 rounded-xl border border-slate-800 bg-slate-900/70 p-1"
          >
            {LINKS.map((link) => (
              <button
                key={link.page}
                type="button"
                onClick={() => onNavigate(link.hash)}
                aria-current={page === link.page ? "page" : undefined}
                className={`rounded-lg px-3.5 py-1.5 text-sm font-medium transition ${
                  page === link.page
                    ? "bg-gradient-to-r from-sky-500/20 to-indigo-500/20 text-sky-300 shadow-inner"
                    : "text-slate-300 hover:bg-slate-800/80 hover:text-slate-100"
                }`}
              >
                {link.label}
              </button>
            ))}
          </nav>
          <span className="hidden items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-300 sm:flex">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
            Live
          </span>
        </div>
      </div>
    </header>
  );
}
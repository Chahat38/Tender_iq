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

/** Top navigation shared by every page. */
export function NavBar({ page, onNavigate }: NavBarProps) {
  return (
    <header className="border-b border-slate-800 px-6 py-5">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3">
        <div className="flex items-baseline gap-3">
          <button
            type="button"
            onClick={() => onNavigate("#/tenders")}
            className="text-left text-2xl font-bold tracking-tight text-slate-100 hover:text-sky-300"
          >
            TenderScout AI
          </button>
          <span className="rounded-full border border-slate-700 px-2 py-0.5 text-xs text-slate-400">
            read-only
          </span>
        </div>
        <nav aria-label="Main" className="flex flex-wrap gap-1">
          {LINKS.map((link) => (
            <button
              key={link.page}
              type="button"
              onClick={() => onNavigate(link.hash)}
              aria-current={page === link.page ? "page" : undefined}
              className={`rounded-md px-3 py-1.5 text-sm ${
                page === link.page
                  ? "bg-sky-600/20 text-sky-300"
                  : "text-slate-300 hover:bg-slate-800"
              }`}
            >
              {link.label}
            </button>
          ))}
        </nav>
      </div>
      <p className="mx-auto mt-1 max-w-6xl text-sm text-slate-400">
        Verified public procurement opportunities in Pakistan — each notice
        shown once even when several portals publish it.
      </p>
    </header>
  );
}
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";

const FAVICON =
  "data:image/svg+xml," +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
      <defs>
        <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#0ea5e9" />
          <stop offset="1" stop-color="#6366f1" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="8" fill="url(#g)" />
      <path d="M9 12.5c0-1.9 1.4-3.5 3.2-3.5.9 0 1.7.4 2.3 1 .6-.6 1.4-1 2.3-1 1.8 0 3.2 1.6 3.2 3.5 0 2.4-4.2 6-5.5 6.4-1.3-.4-5.5-4-5.5-6.4z" fill="#fff" stroke="#fff" stroke-width="1.4"/>
      <path d="M20.5 18l3.2-4h-2.4l-1.2 1.6-1.2-1.6h-2.4l3.2 4c.4.5 1.2.5 1.6 0z" fill="#fff"/>
    </svg>`,
  );

const link = document.createElement("link");
link.rel = "icon";
link.href = FAVICON;
document.head.appendChild(link);
document.title = "TenderScout AI — Pakistan Public Tenders";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
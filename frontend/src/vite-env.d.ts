/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API base for the built app; defaults to same-origin "/api". */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

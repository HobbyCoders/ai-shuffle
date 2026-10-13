// CSR-only app (adapter-static fallback page). Without this, `vite dev`
// attempts SSR and crashes on browser-only modules (DOMPurify hooks,
// document access in +layout.svelte); the production build never SSRs.
export const ssr = false;

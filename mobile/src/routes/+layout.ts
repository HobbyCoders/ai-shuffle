// Client-only bundle (adapter-static fallback page). Workers serve index.html
// for every /mobile/ route (app/mobile_ui.py); there is no server rendering.
export const ssr = false;
export const prerender = false;

/**
 * When a "Check for updates" request counts as answered. Settings > Updates
 * (updates-state.svelte.ts) and the sidebar's manual check
 * (DesktopUpdateButton.svelte) share these rules so both wait for the same
 * workers for the same time.
 */

export const CHECK_POLL_MS = 2_000;
export const CHECK_TIMEOUT_MS = 90_000;
/** An idle worker this long after the request has finished (or never started) its check. */
export const CHECK_IDLE_SETTLE_MS = 10_000;

/** Phases that already say how a check ended. */
export const OUTCOME_PHASES = new Set(['available', 'ready', 'error', 'image-required', 'unsupported', 'downloading']);

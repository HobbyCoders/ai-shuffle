// Plan usage for AI Shuffle: what this mod posts, where, and when. Pure
// functions, so the hooks module stays a thin binding and the tests run them
// directly.
//
// After each main-thread turn, and when a rate-limit window moves a whole
// point, Claude Code raises `session.measure` with the rate-limit windows the
// last API response reported (empty off a subscription). The mod posts those
// windows, and nothing else, to the session's loopback bridge in fluid's
// DelegatedLoop, which hands them to AI Shuffle's Claude usage store. The
// report carries no prompt, no transcript, no account identity and no
// credential; the contract is fluid/fluid/engine/claude_mod_usage.py.
//
// The CLI's own `rate_limit_event` stream frame fires only when a window
// moves a whole point or its status changes, so without this a window that
// holds still is never reported again and reads as stale in the app.
import type { SessionRateLimit, UsageUnit } from 'claude-code'

/** Where the bridge listens and the token that authorizes this route alone. */
export type UsageBridge = { url: string; token: string; socketPath?: string }

/** An unchanged reading is posted again at most this often (the app calls a
 * window stale after five minutes without a report). */
export const REFRESH_MS = 60_000

/** A post still open after this long no longer holds the next one back. */
export const POST_TIMEOUT_MS = 10_000

/** Most windows one report carries (fluid refuses more). */
const MAX_WINDOWS = 8

/**
 * The bridge from the three values DelegatedLoop sets in the CLI's
 * environment, or null when any is missing or the URL is not loopback: a
 * TCP port on 127.0.0.1, or `localhost` over a Unix socket path.
 */
export function bridgeFrom(url?: string, token?: string, socketPath?: string): UsageBridge | null {
  if (!url || !token) return null
  if (/^http:\/\/127\.0\.0\.1:\d{1,5}\//.test(url)) return { url, token }
  if (/^http:\/\/localhost\//.test(url) && socketPath && socketPath.startsWith('/')) {
    return { url, token, socketPath }
  }
  return null
}

/** Whether this measurement is worth a post: a window moved, or the last
 * post is older than REFRESH_MS (or there was none). */
export function shouldPost(changed: readonly UsageUnit[], now: number, lastPostAt: number | null): boolean {
  return changed.includes('rateLimits') || lastPostAt === null || now - lastPostAt >= REFRESH_MS
}

/**
 * The report body for these windows measured at `measuredAt` (epoch ms), or
 * null when there is nothing to report. Each window keeps only its kind, the
 * share used and its reset time.
 */
export function usageReport(rateLimits: readonly SessionRateLimit[], measuredAt: number): string | null {
  const windows = rateLimits
    .filter((w) => typeof w.kind === 'string' && w.kind !== '' && Number.isFinite(w.percentUsed))
    .slice(0, MAX_WINDOWS)
    .map((w) => ({
      kind: w.kind,
      percentUsed: w.percentUsed,
      ...(typeof w.resetsAt === 'string' ? { resetsAt: w.resetsAt } : {}),
    }))
  if (windows.length === 0) return null
  return JSON.stringify({ v: 1, measuredAt, rateLimits: windows })
}

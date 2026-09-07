import { TAB_ACTIVE_SESSION_KEY } from './device-id';
import { sessionEphemeralCache } from './session-cache';
import { writable } from 'svelte/store';

/** A per-tab marker: another tab changing accounts must not relabel this tab's draft. */
export const TAB_SESSION_OWNER_KEY = 'ai-shuffle-session-owner';
let epoch = 0;
const boundary = writable(epoch);
/** Remount an editor only across account boundaries, never on ordinary session changes. */
export const sessionOwnerBoundary = { subscribe: boundary.subscribe };

/** Editors capture this at mount and retire deferred/unmount writes after an account change. */
export function sessionOwnerEpoch(): number { return epoch; }

/** Retire local session data before a different authenticated principal can restore it.
 * Unknown legacy ownership may migrate once; a known owner (including logout)
 * must match. The browser-wide preference owner is only a legacy fallback.
 */
export function bindSessionOwner(
 previous: string | null | undefined, next: string | null, legacyOwner: string | null = null,
): boolean {
 let known = previous;
 if (known === undefined) {
  try {
   const raw = sessionStorage.getItem(TAB_SESSION_OWNER_KEY);
   if (raw !== null) {
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed === 'string') known = parsed;
   }
  } catch { /* Memory and the legacy marker still protect this page lifetime. */ }
  if (known === undefined && legacyOwner) known = legacyOwner;
 }
 const changed = known !== undefined && known !== next;
 if (changed) {
  epoch++;
  sessionEphemeralCache.clear();
  try {
   sessionStorage.removeItem(TAB_ACTIVE_SESSION_KEY);
   // Drafts also have a per-tab reload mirror outside the ephemeral LRU.
   for (let i = sessionStorage.length - 1; i >= 0; i--) {
    const key = sessionStorage.key(i);
    if (key?.startsWith('__aihub_draft_')) sessionStorage.removeItem(key);
   }
  } catch { /* Unavailable browser storage must not interrupt auth teardown. */ }
  boundary.set(epoch);
 }
 try { sessionStorage.setItem(TAB_SESSION_OWNER_KEY, JSON.stringify(next)); }
 catch { /* In-memory previous owner remains authoritative. */ }
 return changed;
}

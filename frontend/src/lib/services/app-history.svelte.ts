import { randomUUID } from '$lib/utils/id';
import { goto } from '$app/navigation';
import { page } from '$app/state';
import type { ActiveView, ViewType } from '$lib/stores/layout.svelte';
import type { CategoryId, SettingsView } from '$lib/settings/types';
import { platform } from '$lib/stores/platform.svelte';

export interface SettingsPlace {
 category: CategoryId;
 view: SettingsView;
 item: string | null;
 tab: string | null;
 depth: number;
 /** Nearest list in this uninterrupted run of collection details. */
 listDepth?: number;
}

export interface Place {
 type: ViewType;
 sessionId?: string | null;
 doc?: string;
 settings?: SettingsPlace;
}

interface Entry { id: string; place: Place }
export type ShellState = App.PageState & { shell?: Entry };

// Ownership: the web UI only. The phone app keeps main's own history writers
// (chat picks, Settings, panel details), so there every call does nothing.
// In the web UI this module reads and writes only entries whose state is its
// own `shell` and nothing else. An overlay (the narrow window's Chat controls)
// pushes its own entry with its own flags, copying the state under it: that entry is
// the overlay's, as on main. Landing on it shows nothing new, and nothing is
// ever written onto it, so a copy can be stale without ever being read.
function owned(state: ShellState | undefined): boolean {
 return Object.keys(state ?? {}).every(key => key === 'shell');
}

/** The place a reload on this entry shows, when the entry is this module's. */
export function savedPlace(state: ShellState | undefined): Place | undefined {
 return !platform.isMobile && owned(state) ? state?.shell?.place : undefined;
}

// The place on screen, with the id of the browser entry it belongs to.
let shown = $state.raw<Entry | null>(null);
// The entry the browser is on once queued writes land. `shown` differs from it
// only while a write is held back (an overlay was up) or after the router
// rejected a push; selecting the place either holds settles it (see below).
let entry: Entry | null = null;
let writes = Promise.resolve();
// A refinement of an entry the browser was not on (an overlay over it), or
// that the router rejected: shown when the browser lands on that entry again.
let held: Entry | null = null;
// Writes still queued per entry id: Kit publishing our own write's state is not a step.
const pending = new Map<string, number>();
let mounted = false;
// No user step since this load: boot may still settle what its entry shows.
let settling = false;
// The load started on an entry with no saved place of its own.
let fresh = false;
// Back/Forward landings on another of this module's entries, counted: a
// navigation deferred to the next frame yields to one that happened since.
let traversals = 0;
// The user settled on an entry's committed place: a refinement held for it is dropped.
function settle(on: Entry) {
 if (held?.id === on.id) held = null;
 shown = { ...on };
}

function samePlace(a: Place | undefined, b: Place | undefined): boolean {
 const sorted = (_: string, value: unknown) => value && typeof value === 'object' && !Array.isArray(value)
  ? Object.fromEntries(Object.entries(value).sort(([x], [y]) => (x < y ? -1 : 1))) : value;
 return JSON.stringify(a, sorted) === JSON.stringify(b, sorted);
}

export const shellHistory = {
 get traversals() { return traversals; },
 get place() { return shown?.place; },
 /**
  * At mount. A saved place is shown as it is. Otherwise the arrival entry is
  * written now, before anything can push over it; boot's corrections replace it
  * while the browser is on it, or once an overlay over it closes.
  */
 start(state: ShellState, view: ActiveView | null) {
  if (platform.isMobile) return;
  mounted = settling = true;
  if (owned(state) && state.shell) {
   fresh = false;
   entry = shown = { ...state.shell };
   return;
  }
  fresh = true;
  entry = null;
  shown = { id: randomUUID(), place: viewPlace(view) };
  if (owned(state)) record(shown, 'stamp');
 },
 stop() { mounted = settling = false; entry = shown = held = null; },
 /**
  * The chat a fresh load's arrival actually shows (this tab's restored chat
  * may differ from the shared layout snapshot), until the user's first step.
  */
 shows(sessionId: string | null) {
  if (!settling || !fresh || shown?.place.type !== 'session' || shown.place.sessionId === sessionId) return;
  refine({ type: 'session', sessionId });
 },
 /**
  * The browser moved to an entry. True when it is another entry of this
  * module's, whose place the caller then shows. An overlay's entry, or the
  * entry this module is already on, shows nothing new.
  */
 land(state: ShellState) {
  if (!mounted || !owned(state) || !state.shell || pending.has(state.shell.id)) return false;
  let at = { ...state.shell };
  const kept = held?.id === at.id && !samePlace(held.place, at.place) ? held : null;
  if (at.id === entry?.id) {
   // Back on our entry once an overlay closed: a place held back meanwhile goes in now.
   entry = at;
   if (shown?.id === at.id && !samePlace(shown.place, at.place)) record(shown, 'replace');
   return false;
  }
  settling = false;
  traversals++;
  entry = at;
  if (kept) at = { ...kept };
  shown = at;
  if (kept) record(at, 'replace');
  return true;
 },
 /**
  * A view change; false when declined. `reconcile` is boot settling the view
  * it restored, only before the user's first step. `replace` corrects the
  * entry on screen. Anything else is the user's step.
  */
 destination(view: ActiveView, intent: 'push' | 'reconcile' | 'replace' = 'push') {
  if (!mounted || !shown) return true;
  const next = viewPlace(view);
  const top = (place?: Place) => !!place && place.type === next.type && (place.sessionId ?? null) === (next.sessionId ?? null);
  if (shown.id === entry?.id && top(shown.place)) return true;
  // The entry the browser is on already holds it: after a rejected push, or
  // over a held-back write. Settle on that entry, without writing again.
  if (entry && top(entry.place)) {
   settle(entry);
   return true;
  }
  if (intent === 'reconcile') {
   if (!settling) return false;
   // A saved entry is a stop the user made: boot moves the screen, not the entry.
   if (fresh) refine(next);
   else shown = { id: shown.id, place: next };
   return true;
  }
  settling = false;
  // Resolving a new chat into its server id is not another visible step.
  const resolving = next.type === 'session' && shown.place.type === 'session' && !shown.place.sessionId;
  if ((intent === 'replace' || resolving) && shown.id === entry?.id) refine(next);
  else push(next);
  return true;
 },
 detail(type: ViewType, patch: Partial<Place>, replace = false) {
  if (!mounted || shown?.place.type !== type) return;
  const next = { ...shown.place, ...patch };
  // Already committed: nothing to write. Otherwise a place already on screen
  // was rejected by the router: it is written again, onto the same entry.
  if (entry && samePlace(next, entry.place)) {
   settle(entry);
   return;
  }
  if ((replace || samePlace(next, shown.place)) && shown.id === entry?.id) refine(next);
  else push(next);
 },
 /**
  * The chat on screen proved unavailable (deleted, or not this viewer's):
  * its entry becomes New Chat, so New Chat's first send refines it. Only when
  * that chat is still what shows: a Back that already left it is untouched.
  */
 unavailable(sessionId: string) {
  if (!mounted || shown?.place.type !== 'session' || shown.place.sessionId !== sessionId) return false;
  // Back may already be on another entry whose landing has not run yet.
  const state = page.state as ShellState;
  if (owned(state) && state.shell && state.shell.id !== shown.id) return false;
  refine({ type: 'session', sessionId: null });
  return true;
 }
};

function viewPlace(view: ActiveView | null): Place {
 return !view || view.type === 'session' || view.type === 'board'
  ? { type: 'session', sessionId: view?.sessionId ?? null } : { type: view.type };
}

// The same entry, refined: it keeps its id. Written only while the browser is
// on it; otherwise held back until it is (see land).
function refine(place: Place) {
 if (!shown) return;
 shown = { id: shown.id, place };
 if (shown.id === entry?.id) record(shown, 'replace');
}

function push(place: Place) {
 shown = { id: randomUUID(), place };
 record(shown, 'push');
}

function record(next: Entry, mode: 'push' | 'replace' | 'stamp') {
 const { id } = next;
 const before = entry;
 entry = next;
 pending.set(id, (pending.get(id) ?? 0) + 1);
 const replace = mode !== 'push';
 // Not written: the browser stays where it is. Later writes already queued set their own.
 const keep = () => {
  if (replace) held = next;
  if (entry !== next) return;
  const state = page.state as ShellState;
  entry = owned(state) ? (state.shell ? { ...state.shell } : null) : before;
 };
 writes = writes.then(async () => {
  try {
   const state = page.state as ShellState;
   // A replace refines only our entry the browser is on; the arrival is stamped only while bare.
   if (replace && (!owned(state) || (state.shell?.id ?? (mode === 'stamp' ? id : null)) !== id)) return keep();
   // Kit's shallow goto settles a few ticks after its native history write
   // (no load, and no reroute or onNavigate hook here), so the next write
   // reads the entry this one made. No timer: one that let a later write
   // pass before this one committed would reorder the entries.
   await goto(location.href, { shallow: true, replace, persistState: true, state: { shell: next } as ShellState });
   if (held?.id === id) held = null;
  } catch (error) {
   keep();
   console.warn('Could not record navigation', error);
  } finally {
   const left = (pending.get(id) ?? 1) - 1;
   if (left) pending.set(id, left); else pending.delete(id);
  }
 });
}

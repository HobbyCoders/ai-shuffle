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
 panel?: string | null;
}

interface Entry { id: string; place: Place }
export type ShellState = App.PageState & { shell?: Entry };

// Ownership: this module reads and writes only entries whose state is its own
// `shell` and nothing else. A phone overlay (Chat controls, the drawer) pushes
// its own entry with its own flags, copying the state under it: that entry is
// the overlay's, as on main. Landing on it shows nothing new, and nothing is
// ever written onto it, so a copy can be stale without ever being read.
function owned(state: ShellState | undefined): boolean {
 return Object.keys(state ?? {}).every(key => key === 'shell');
}

/** The place a reload on this entry shows, when the entry is this module's. */
export function savedPlace(state: ShellState | undefined): Place | undefined {
 return owned(state) ? state?.shell?.place : undefined;
}

// The place on screen, with the id of the browser entry it belongs to.
let shown = $state.raw<Entry | null>(null);
// A place shown from the browser's history: Back, Forward, a reload on it.
let restored = $state.raw<Entry | null>(null);
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
// A chat picked from a list (main's chat-history rule, #953). The phone app
// adds an entry only for these; its other view changes refine the entry it is on.
let picked: { to: string; from: string | null } | null = null;
// A pick from the phone drawer: the drawer's own Back (it pops its entry as it
// closes) lands after the pick's push is queued. The push waits for it, or that
// Back would step off the new entry, and that Back is not the user's. Main's
// rule: chat picks only, and a second pick restarts the give-up.
let drawerPop: { end: () => void; restart: () => void; landed: Promise<void> } | null = null;
const DRAWER_BACK_WAIT_MS = 600;
function drawerPopped(): Promise<void> {
 if (drawerPop) {
  drawerPop.restart();
  return drawerPop.landed;
 }
 if (!(page.state as ShellState | undefined)?.mobileDrawer) return Promise.resolve();
 let resolve!: () => void;
 const landed = new Promise<void>(done => { resolve = done; });
 let timer: ReturnType<typeof setTimeout>;
 const end = () => { clearTimeout(timer); if (drawerPop?.landed === landed) drawerPop = null; resolve(); };
 const restart = () => { clearTimeout(timer); timer = setTimeout(end, DRAWER_BACK_WAIT_MS); };
 restart();
 drawerPop = { end, restart, landed };
 return landed;
}

// Where SvelteKit numbers the browser's entries (main's settings history reads it too).
function historyIndex(): number | null {
 const meta = (typeof history === 'undefined' ? null : history.state as Record<string, unknown> | null)?.['sveltekit:metadata'];
 const index = (meta as { historyIndex?: unknown } | null | undefined)?.historyIndex;
 return typeof index === 'number' ? index : null;
}
// The entry a Back or Forward left, so a page that refuses the step (a draft
// the user keeps) can return the browser there without adding a stop.
let left: { landing: Entry; index: number | null } | null = null;
let here: number | null = null;

function samePlace(a: Place | undefined, b: Place | undefined): boolean {
 const sorted = (_: string, value: unknown) => value && typeof value === 'object' && !Array.isArray(value)
  ? Object.fromEntries(Object.entries(value).sort(([x], [y]) => (x < y ? -1 : 1))) : value;
 return JSON.stringify(a, sorted) === JSON.stringify(b, sorted);
}

export const shellHistory = {
 get restored() { return restored; },
 get place() { return shown?.place; },
 /**
  * At mount. A saved place is shown as it is. Otherwise the arrival entry is
  * written now, before anything can push over it; boot's corrections replace it
  * while the browser is on it, or once an overlay over it closes.
  */
 start(state: ShellState, view: ActiveView | null) {
  mounted = settling = true;
  picked = null;
  left = null;
  here = historyIndex();
  if (owned(state) && state.shell) {
   fresh = false;
   entry = restored = shown = { ...state.shell };
   return;
  }
  fresh = true;
  restored = entry = null;
  shown = { id: randomUUID(), place: viewPlace(view) };
  if (owned(state)) record(shown, 'stamp');
 },
 stop() { mounted = settling = false; entry = restored = shown = held = left = null; picked = null; drawerPop?.end(); },
 /** A chat picked from a list, just before it opens (main's openSessionFromList rule). */
 pick(to: string, from: string | null) { picked = { to, from }; },
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
  if (drawerPop && !state.mobileDrawer) {
   drawerPop.end();
   return false;
  }
  if (!mounted || !owned(state) || !state.shell || pending.has(state.shell.id)) return false;
  let at = { ...state.shell };
  const kept = held?.id === at.id && !samePlace(held.place, at.place) ? held : null;
  if (at.id === entry?.id) {
   // Back on our entry once an overlay closed: a place held back meanwhile goes in now.
   entry = at;
   here = historyIndex();
   if (shown?.id === at.id && !samePlace(shown.place, at.place)) record(shown, 'replace');
   return false;
  }
  settling = false;
  entry = at;
  if (kept) at = { ...kept };
  restored = shown = at;
  left = { landing: at, index: here };
  here = historyIndex();
  if (kept) record(at, 'replace');
  return true;
 },
 /** The page kept what it showed instead of following this landing: go back to the entry it left. */
 refuse(landing: Entry | null) {
  if (!landing || landing !== restored || left?.landing !== landing || left.index === null || here === null) return;
  const delta = left.index - here;
  left = null;
  if (delta) history.go(delta);
 },
 /**
  * A view change; false when declined. `reconcile` is boot settling the view
  * it restored, only before the user's first step. `replace` corrects the
  * entry on screen. Anything else is the user's step.
  */
 destination(view: ActiveView, intent: 'push' | 'reconcile' | 'replace' = 'push') {
  if (!mounted || !shown) return true;
  const next = viewPlace(view);
  const pick = picked?.to === next.sessionId && next.type === 'session' ? picked : null;
  picked = null;
  const top = (place?: Place) => !!place && place.type === next.type && (place.sessionId ?? null) === (next.sessionId ?? null);
  if (shown.id === entry?.id && top(shown.place)) return true;
  // The entry the browser is on already holds it: after a rejected push, or
  // over a held-back write. Settle on that entry, without writing again.
  if (entry && top(entry.place)) {
   shown = { ...entry };
   restored = null;
   return true;
  }
  if (intent === 'reconcile') {
   if (!settling) return false;
   // A saved entry is a stop the user made: boot moves the screen, not the entry.
   if (fresh) refine(next);
   else { restored = null; shown = { id: shown.id, place: next }; }
   return true;
  }
  settling = false;
  // Resolving a new chat into its server id is not another visible step, and
  // the phone app adds an entry only for a chat picked from a list (as main).
  const resolving = next.type === 'session' && shown.place.type === 'session' && !shown.place.sessionId;
  const phoneRefine = platform.isMobile && !(pick?.from && pick.from !== pick.to);
  if (phoneRefine) refine(next, entry?.id);
  else if ((intent === 'replace' || resolving) && shown.id === entry?.id) refine(next);
  else push(next, !!pick);
  return true;
 },
 detail(type: ViewType, patch: Partial<Place>, replace = false) {
  if (!mounted || shown?.place.type !== type) return;
  const next = { ...shown.place, ...patch };
  if (shown.id === entry?.id && samePlace(next, shown.place)) return;
  if (entry && samePlace(next, entry.place)) {
   shown = { ...entry };
   restored = null;
   return;
  }
  if (replace && shown.id === entry?.id) refine(next);
  else push(next, false);
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
function refine(place: Place, id = shown?.id) {
 if (!shown || !id) return;
 restored = null;
 shown = { id, place };
 if (shown.id === entry?.id) record(shown, 'replace');
}

function push(place: Place, pick: boolean) {
 restored = null;
 shown = { id: randomUUID(), place };
 record(shown, 'push', pick);
}

function record(next: Entry, mode: 'push' | 'replace' | 'stamp', pick = false) {
 const { id } = next;
 const before = entry;
 entry = next;
 pending.set(id, (pending.get(id) ?? 0) + 1);
 const popped = pick ? drawerPopped() : Promise.resolve();
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
   await popped;
   const state = page.state as ShellState;
   // A replace refines only our entry the browser is on; the arrival is stamped only while bare.
   if (replace && (!owned(state) || (state.shell?.id ?? (mode === 'stamp' ? id : null)) !== id)) return keep();
   // Kit's shallow goto settles a few ticks after its native history write
   // (no load, and no reroute or onNavigate hook here), so the next write
   // reads the entry this one made. No timer: one that let a later write
   // pass before this one committed would reorder the entries.
   await goto(location.href, { shallow: true, replace, persistState: true, state: { shell: next } as ShellState });
   here = historyIndex();
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

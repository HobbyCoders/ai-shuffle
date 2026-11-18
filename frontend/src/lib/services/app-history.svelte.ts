import { randomUUID } from '$lib/utils/id';
import { goto } from '$app/navigation';
import { page } from '$app/state';
import type { ActiveView, ViewType } from '$lib/stores/layout.svelte';
import type { CategoryId, SettingsView } from '$lib/settings/types';

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

// Kit persists the place on the actual browser entry, including on reload. No
// historyIndex or sessionStorage lookup: a push after Back owns a new entry.
let current = $state.raw<Entry | null>(null);
let restored = $state.raw<Entry | null>(null);
let writes = Promise.resolve();
// Writes still queued per entry id. A replace reuses its entry's id, so an id
// stays pending until its last write lands, not its first.
const pending = new Map<string, number>();
let mounted = false;
// A fresh load's entry, while boot may still change what it shows. It is not
// written meanwhile: an overlay opened on it would copy it, and a copy cannot
// be corrected. Every entry without a saved place (it, and overlays' copies)
// shows this one place. It is written when the user leaves it, once final; a
// reload before then loads fresh, as on main.
let landing: Entry | null = null;
// No step (a push, Back or Forward) taken since mount: boot may still settle
// what the entry this load started on shows.
let settling = false;
// A write's history entry exists before Kit's navigation tail settles; a tail
// that never settles must not hold back every later write.
const WRITE_SETTLE_MS = 2000;
export const shellHistory = {
 get restored() { return restored; },
 get place() { return current?.place; },
 start(state: ShellState, view: ActiveView | null) {
  mounted = settling = true;
  if (state.shell) return void (restored = current = { ...state.shell });
  landing = current = { id: randomUUID(), place: viewPlace(view) };
 },
 stop() { mounted = settling = false; landing = restored = current = null; },
 land(state: ShellState) {
  // Every entry of the landing, saved or not, shows its one current place.
  const entry = !state.shell || state.shell.id === landing?.id ? landing : state.shell;
  if (!mounted || !entry || entry.id === current?.id || pending.has(entry.id)) return false;
  settling = false;
  restored = current = { ...entry };
  return true;
 },
 /**
  * A view change; false when declined. `reconcile` is boot settling the view
  * it restored, only before the user's first step and never in the browser's
  * entry: a saved place is shown from mount, so boot only moves it when it
  * could not be restored (a deleted chat). Anything else is the user's step.
  */
 destination(view: ActiveView, intent: 'push' | 'reconcile' = 'push') {
  const next = viewPlace(view);
  if (current?.place.type === next.type && current.place.sessionId === next.sessionId) return true;
  if (intent === 'reconcile') {
   if (!settling || !current) return false;
   restored = null;
   current = { id: current.id, place: next };
   if (landing?.id === current.id) landing = current;
   return true;
  }
  // Resolving a new chat into its server id is not another visible step.
  const resolving = next.type === 'session' && current?.place.type === 'session' && !current.place.sessionId;
  write(next, resolving);
  return true;
 },
 detail(type: ViewType, patch: Partial<Place>, replace = false) {
  if (current?.place.type !== type) return;
  const next = { ...current.place, ...patch };
  if (JSON.stringify(next) !== JSON.stringify(current.place)) write(next, replace);
 }
};

function viewPlace(view: ActiveView | null): Place {
 return !view || view.type === 'session' || view.type === 'board'
  ? { type: 'session', sessionId: view?.sessionId ?? null } : { type: view.type };
}

// A replace refines the same entry, so it keeps the entry's id and the live
// entry's other state. A phone overlay (Chat controls, the drawer) may have
// pushed its own entry on top, copying the id: stripping its state would read
// as Back, and a new id would make Back to the entry beneath look like a move.
function write(place: Place, replace = false) {
 if (!mounted) return;
 const from = current;
 const entry = { id: replace && from ? from.id : randomUUID(), place };
 restored = null;
 current = entry;
 if (landing && entry.id === landing.id) {
  landing = entry;
  // Boot may still correct it: keep it unwritten (see `landing`).
  if (settling) return;
 }
 if (!replace) {
  // Leaving the landing: its place is final, so the browser keeps it.
  if (landing && from?.id === landing.id) record(landing, true);
  settling = false;
 }
 record(entry, replace);
}

function record(entry: Entry, replace: boolean) {
 const { id } = entry;
 pending.set(id, (pending.get(id) ?? 0) + 1);
 writes = writes.then(async () => {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
   const state = page.state as ShellState;
   // A replace only refines the entry the browser is on (not after a push
   // that failed, or a Back). The landing is never written as an overlay's copy.
   if (replace && (state.shell?.id ?? (Object.keys(state).length ? null : landing?.id)) !== id) return;
   const kept: ShellState = replace ? { ...state } : {};
   delete kept.shell;
   const written = Promise.resolve(goto(location.href, { shallow: true, replace, persistState: true, state: { ...kept, shell: entry } as ShellState }))
    .catch(error => console.warn('Could not record navigation', error));
   await Promise.race([written, new Promise(resolve => { timer = setTimeout(resolve, WRITE_SETTLE_MS); })]);
  } finally {
   clearTimeout(timer);
   const left = (pending.get(id) ?? 1) - 1;
   if (left) pending.set(id, left); else pending.delete(id);
  }
 }).catch(error => console.warn('Could not record navigation', error));
}

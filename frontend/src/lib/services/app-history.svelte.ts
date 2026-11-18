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
const pending = new Set<string>();
let mounted = false;
// The entry this load stamped, with the state keys it carries. Null when the
// browser already had one (a reload): that entry is where the user is.
let arrival: { id: string; keys: string[] } | null = null;
// Boot settled the arrival on another view while a phone overlay's entry sat
// on top of it. The browser's arrival entry still holds the old place.
let stale: Entry | null = null;
export const shellHistory = {
 get restored() { return restored; },
 get place() { return current?.place; },
 get entryId() { return current?.id; },
 start(state: ShellState, view: ActiveView | null) {
  mounted = true;
  if (state.shell) return void (restored = current = { ...state.shell });
  write(viewPlace(view), true);
  arrival = { id: current!.id, keys: [...Object.keys(page.state), 'shell'] };
 },
 stop() { mounted = false; arrival = stale = restored = current = null; },
 land(state: ShellState) {
  let entry = state.shell;
  // An overlay over the arrival copies its old place; boot's view is the one.
  if (stale && entry?.id === stale.id) {
   entry = stale;
   if (onArrival(state)) correctArrival();
  }
  if (!mounted || !entry || entry.id === current?.id || pending.has(entry.id)) return false;
  restored = current = { ...entry };
  return true;
 },
 /**
  * A view change. `reconcile` is boot settling the view it restored: it only
  * corrects the entry this load stamped, never a stop the user made since or
  * an entry the browser restored. Anything else is the user's step.
  */
 destination(view: ActiveView, intent: 'push' | 'reconcile' = 'push') {
  const next = viewPlace(view);
  if (current?.place.type === next.type && current.place.sessionId === next.sessionId) return;
  if (intent === 'reconcile') {
   if (!arrival || current?.id !== arrival.id) return;
   restored = null;
   stale = current = { id: arrival.id, place: next };
   return correctArrival();
  }
  // Resolving a new chat into its server id is not another visible step.
  const resolving = next.type === 'session' && current?.place.type === 'session' && !current.place.sessionId;
  write(next, resolving);
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
 // A boot correction still waiting goes onto the arrival entry first.
 if (stale && !replace) correctArrival();
 const id = replace && current ? current.id : randomUUID();
 const entry = { id, place };
 restored = null;
 current = entry;
 // Refining the arrival under an overlay: the waiting correction is this place.
 if (stale?.id === id) stale = entry;
 pending.add(id);
 queue(async () => {
  try {
   const kept: ShellState = replace ? { ...page.state } : {};
   delete kept.shell;
   await goto(location.href, { shallow: true, replace, persistState: true, state: { ...kept, shell: entry } as ShellState });
  } finally { pending.delete(id); }
 });
}

/** The browser is on the arrival entry itself, not an overlay's copy of it. */
function onArrival(state: ShellState) {
 return !!arrival && state.shell?.id === arrival.id && Object.keys(state).every(key => arrival!.keys.includes(key));
}

// Writes boot's correction once the arrival entry is current, never onto an
// overlay's entry: Back from the overlay must land on the corrected place.
function correctArrival() {
 const entry = stale;
 queue(async () => {
  if (!mounted || !entry || entry !== stale || !onArrival(page.state)) return;
  stale = null;
  await goto(location.href, { shallow: true, replace: true, persistState: true, state: { ...page.state, shell: entry } as ShellState });
 });
}

function queue(task: () => Promise<void>) {
 writes = writes.then(task).catch(error => console.warn('Could not record navigation', error));
}

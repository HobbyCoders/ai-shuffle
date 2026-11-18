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
// Until boot settles, a view change rewrites the first entry's place instead of
// adding a stop: the restored or reconciled view is where the user arrived.
let booting = false;
export const shellHistory = {
 get restored() { return restored; },
 get place() { return current?.place; },
 get entryId() { return current?.id; },
 start(state: ShellState, view: ActiveView | null, boot = false) {
  mounted = true;
  booting = boot;
  if (state.shell) restored = current = { ...state.shell };
  else write(viewPlace(view), true);
 },
 /** Boot reconciled the view: later view changes are steps again. */
 settle(view: ActiveView | null) {
  this.destination(view);
  booting = false;
 },
 stop() { mounted = booting = false; restored = current = null; },
 land(state: ShellState) {
  const entry = state.shell;
  if (!mounted || !entry || entry.id === current?.id || pending.has(entry.id)) return false;
  restored = current = { ...entry };
  return true;
 },
 destination(view: ActiveView | null) {
  const next = viewPlace(view);
  if (current?.place.type === next.type && current.place.sessionId === next.sessionId) return;
  // Resolving a new chat into its server id is not another visible step.
  const resolving = next.type === 'session' && current?.place.type === 'session' && !current.place.sessionId;
  write(next, booting || resolving);
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
 const id = replace && current ? current.id : randomUUID();
 const entry = { id, place };
 restored = null;
 current = entry;
 pending.add(id);
 writes = writes.then(async () => {
  try {
   const kept: ShellState = replace ? { ...page.state } : {};
   delete kept.shell;
   await goto(location.href, { shallow: true, replace, persistState: true, state: { ...kept, shell: entry } as ShellState });
  } finally { pending.delete(id); }
 }).catch(error => console.warn('Could not record navigation', error));
}

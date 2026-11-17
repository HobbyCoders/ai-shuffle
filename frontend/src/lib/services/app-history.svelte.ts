import { goto } from '$app/navigation';
import type { ActiveView, ViewType } from '$lib/stores/layout.svelte';
import type { CategoryId, SettingsView } from '$lib/settings/types';

export interface SettingsPlace {
 category: CategoryId;
 view: SettingsView;
 item: string | null;
 tab: string | null;
 depth: number;
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
export const shellHistory = {
 get restored() { return restored; },
 get place() { return current?.place; },
 get entryId() { return current?.id; },
 start(state: ShellState, view: ActiveView | null) {
  mounted = true;
  if (state.shell) restored = current = { ...state.shell };
  else write(viewPlace(view), true, state);
 },
 stop() { mounted = false; restored = current = null; },
 land(state: ShellState) {
  const entry = state.shell;
  if (!mounted || !entry || entry.id === current?.id || pending.has(entry.id)) return false;
  restored = current = { ...entry };
  return true;
 },
 destination(view: ActiveView) {
  const next = viewPlace(view);
  if (current?.place.type === next.type && current.place.sessionId === next.sessionId) return;
  // Resolving a new chat into its server id is not another visible step.
  const resolving = next.type === 'session' && current?.place.type === 'session' && !current.place.sessionId;
  write(next, resolving);
 },
 detail(type: ViewType, patch: Partial<Place>, replace = false) {
  if (current?.place.type !== type) return;
  const next = { ...current.place, ...patch };
  if (JSON.stringify(next) !== JSON.stringify(current.place)) write(next, replace);
 },
 overlay(state: App.PageState) { if (current) write(current.place, false, state); }
};

function viewPlace(view: ActiveView | null): Place {
 return !view || view.type === 'session' || view.type === 'board'
  ? { type: 'session', sessionId: view?.sessionId ?? null } : { type: view.type };
}

function write(place: Place, replace = false, overlay: App.PageState = {}) {
 if (!mounted) return;
 const id = globalThis.crypto.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
 const entry = { id, place };
 restored = null;
 current = entry;
 pending.add(id);
 writes = writes.then(async () => {
  try {
   await goto(location.href, { shallow: true, replace, persistState: true, state: { ...overlay, shell: entry } as ShellState });
  } finally { pending.delete(id); }
 }).catch(error => console.warn('Could not record navigation', error));
}

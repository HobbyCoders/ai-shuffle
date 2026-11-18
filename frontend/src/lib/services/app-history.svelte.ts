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
/** Overlays pushed before `start`, keyed by their state: they carry no shell id. */
const early = new Set<string>();
/** The write queued last, by shell id or early overlay key. */
let latest: string | undefined;
/** The app is mounted: overlays (Chat controls, the phone drawer) may push entries. */
let armed = false;
/** Shell history started: places are recorded. */
let mounted = false;
/** The boot entry under an overlay that was open when the shell history started. */
let unclaimedBase: Place | null = null;
export const shellHistory = {
 get restored() { return restored; },
 get place() { return current?.place; },
 get entryId() { return current?.id; },
 /** Overlays opened while boot still loads push their entries before `start`. */
 arm() { armed = true; },
 start(state: ShellState, view: ActiveView | null) {
  armed = mounted = true;
  if (state.shell) restored = current = { ...state.shell };
  // Owns the entry live at commit: an overlay opened during boot stays on it.
  else write(viewPlace(view), true, state, true);
 },
 stop() { armed = mounted = false; restored = current = unclaimedBase = null; },
 land(state: ShellState) {
  const entry = state.shell;
  if (!entry) return claimBase(state);
  if (!mounted || entry.id === current?.id || pending.has(entry.id)) return false;
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
 /**
  * Push `state` over the current place; before `start`, over the boot entry.
  * False when nothing can be recorded (no app mounted). `failed` runs if the
  * router rejects the push after all: the caller owns no entry then.
  */
 overlay(state: App.PageState, failed?: () => void) {
  if (!armed) return false;
  if (current) write(current.place, false, state, false, failed);
  else {
   const key = latest = JSON.stringify(state);
   early.add(key);
   queue(async () => {
    try { await commit(state, false, !!failed); } finally { early.delete(key); }
   }, failed);
  }
  return true;
 },
 /**
  * Back off an overlay's own entries once every queued write has committed:
  * `depth` counts those under and including the landed one, so Back never
  * passes an entry the overlay did not push, nor one that never committed.
  * Settles once it landed.
  */
 retire(depth: (state: App.PageState) => number) {
  return queue(async () => {
   const steps = depth(page.state);
   if (!steps) return;
   const before = page.state;
   const deadline = Date.now() + 1000;
   history.go(-steps);
   // The next write waits for the landing, or it would be pushed under it.
   while (page.state === before && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 16));
  });
 },
 /** The landing of an own write that a later write already replaced: not a navigation. */
 superseded(state: ShellState) {
  const key = state.shell?.id ?? JSON.stringify(state);
  return (pending.has(key) || early.has(key)) && key !== latest;
 }
};

function viewPlace(view: ActiveView | null): Place {
 return !view || view.type === 'session' || view.type === 'board'
  ? { type: 'session', sessionId: view?.sessionId ?? null } : { type: view.type };
}

/** The push or replace; `verify` rejects one the router dropped (a cancelled navigation). */
async function commit(state: App.PageState, replace: boolean, verify: boolean) {
 await goto(location.href, { shallow: true, replace, persistState: true, state });
 if (verify && JSON.stringify(page.state) !== JSON.stringify(state)) throw new Error('Navigation was cancelled');
}

function queue(run: () => Promise<void>, failed?: () => void) {
 writes = writes.then(run).catch(error => {
  console.warn('Could not record navigation', error);
  failed?.();
 });
 return writes;
}

/** The first landing on the bare boot entry under an early overlay gives it the start place. */
function claimBase(state: ShellState) {
 if (!mounted || !unclaimedBase || Object.keys(state).length) return false;
 const place = unclaimedBase;
 unclaimedBase = null;
 const moved = JSON.stringify(place) !== JSON.stringify(current?.place);
 write(place, true);
 restored = current;
 return moved;
}

function write(place: Place, replace = false, overlay: App.PageState = {}, live = false, failed?: () => void) {
 if (!mounted) return;
 const id = randomUUID();
 const entry = { id, place };
 restored = null;
 current = entry;
 pending.add(id);
 latest = id;
 queue(async () => {
  try {
   let state = overlay;
   if (live) {
    const { shell: _, ...open } = page.state as ShellState;
    state = open;
    if (Object.keys(open).length) unclaimedBase = place;
   }
   await commit({ ...state, shell: entry } as ShellState, replace, !!failed);
  } finally { pending.delete(id); }
 }, failed);
}

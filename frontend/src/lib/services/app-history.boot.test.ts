import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { page } from '$app/state';
import { savedPlace, shellHistory, type ShellState } from './app-history.svelte';
import { layout, type ActiveView } from '$lib/stores/layout.svelte';

// A router that keeps page state the way SvelteKit does: a shallow goto with
// persistState survives a reload, while an overlay's pushState (Chat controls,
// the drawer) does not. A landed write also becomes page.state, and AppPage's
// page.state effect runs before the navigation resolves.
// `slow` is Kit's shape stretched out: route resolution before the native
// history write, then page.state published, then the navigation settling.
const router = vi.hoisted(() => ({
 entries: [] as { state: ShellState; persisted: boolean }[], index: 0,
 onState: null as ((state: ShellState) => void) | null,
 outcomes: [] as ('reject' | 'slow' | null)[]
}));
vi.mock('$app/navigation', () => ({ goto: async (_url: string, options: { replace: boolean; persistState: boolean; state: ShellState }) => {
 const outcome = router.outcomes.shift();
 const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, outcome === 'slow' ? ms : 0));
 if (outcome === 'reject') throw new Error('Cannot call goto(...) before router is initialized');
 if (outcome === 'slow') await wait(3000);
 const entry = { state: options.state, persisted: options.persistState };
 if (options.replace) router.entries[router.index] = entry;
 else router.entries.splice(++router.index, Infinity, entry);
 if (outcome === 'slow') await wait(500);
 page.state = options.state;
 router.onState?.(options.state);
 if (outcome === 'slow') await wait(500);
} }));
const phone = vi.hoisted(() => ({ app: false }));
vi.mock('$lib/stores/platform.svelte', () => ({ platform: { get isMobile() { return phone.app; } } }));
const here = () => router.entries[router.index].state;
const places = () => router.entries.map(({ state }) => state.shell?.place.sessionId ?? state.shell?.place.type);
const settle = () => new Promise(resolve => setTimeout(resolve));
/** An overlay's own entry, as the Chat controls sheet and the drawer push it. */
function pushOverlay(state: App.PageState) {
 router.entries.splice(++router.index, Infinity, { state: { ...page.state, ...state } as ShellState, persisted: false });
 page.state = here();
}
function go(delta: number) {
 router.index += delta;
 page.state = here();
 return shellHistory.land(here());
}
/** A reload on the current entry; the layout snapshot still holds `view`. */
function reload(view: ActiveView) {
 shellHistory.stop();
 router.entries = router.entries.map(entry => entry.persisted ? entry : { state: {}, persisted: false });
 page.state = here();
 shellHistory.start(page.state, view);
}
/** A fresh load on chat `a`, Chat controls opened over it while boot loads, then boot settling on `b`. */
async function arriveUnderOverlay() {
 shellHistory.start({}, { type: 'session', sessionId: 'a' });
 // A person opens the controls after the page has mounted.
 await settle();
 pushOverlay({ mobileControls: true });
 shellHistory.destination({ type: 'session', sessionId: 'b' }, 'reconcile');
 expect(shellHistory.place?.sessionId).toBe('b');
}

beforeEach(() => {
 router.index = 0; router.entries = [{ state: {}, persisted: false }]; page.state = {};
 router.onState = null; router.outcomes = []; phone.app = false;
 // SvelteKit numbers entries in history.state; Back and Forward go through the router above.
 vi.spyOn(history, 'state', 'get').mockImplementation(() => ({ 'sveltekit:metadata': { historyIndex: router.index } }));
 vi.spyOn(history, 'go').mockImplementation(delta => { go(delta ?? 0); });
 shellHistory.stop();
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });
const saved = (id: string, place: ShellState['shell'] extends infer E ? E extends { place: infer P } ? P : never : never): ShellState => ({ shell: { id, place } });
function startOn(...states: ShellState[]) {
 router.entries = states.map(state => ({ state, persisted: true }));
 router.index = states.length - 1; page.state = here();
 shellHistory.start(page.state, null);
}

it('never reads or writes an overlay’s entry; boot’s view reaches the arrival when Back returns there past it', async () => {
 await arriveUnderOverlay();
 const overlay = router.entries[1].state;
 shellHistory.destination({ type: 'settings' });
 await vi.waitFor(() => expect(router.entries).toHaveLength(3));
 await settle();
 // Each step lets queued writes run, as a person's pace does.
 const step = async (delta: number) => { const shows = go(delta); await settle(); return shows; };
 // The sheet's entry is its own, as on main: a step onto it shows nothing new.
 expect(await step(-1)).toBe(false);
 expect(shellHistory.place?.type).toBe('settings');
 expect(await step(-1)).toBe(true);
 expect(shellHistory.place?.sessionId).toBe('b');
 expect(await step(1)).toBe(false);
 expect(await step(1)).toBe(true);
 expect(shellHistory.place?.type).toBe('settings');
 await step(-2);
 expect(shellHistory.place?.sessionId).toBe('b');
 expect(places()).toEqual(['b', 'a', 'settings']);
 expect(router.entries[1].state).toBe(overlay);
});

it('shows boot’s view once the overlay closes, and Back from a later step returns there', async () => {
 await arriveUnderOverlay();
 expect(go(-1)).toBe(false);
 shellHistory.destination({ type: 'settings' });
 await vi.waitFor(() => expect(places()).toEqual(['b', 'settings']));
 expect(go(-1)).toBe(true);
 expect(shellHistory.place?.sessionId).toBe('b');
});

it('after a reload on the overlay, whose state the browser dropped, that entry is a fresh arrival', async () => {
 await arriveUnderOverlay();
 await settle();
 reload({ type: 'session', sessionId: 'b' });
 await settle();
 expect(shellHistory.place?.sessionId).toBe('b');
 expect(here()).toMatchObject({ shell: { place: { type: 'session', sessionId: 'b' } } });
 expect(here().mobileControls).toBeUndefined();
});

it('never lets boot move a stop the user made; on a saved entry it moves the screen only, never the browser’s entry', async () => {
 shellHistory.start({}, { type: 'session', sessionId: 'a' });
 shellHistory.destination({ type: 'session', sessionId: 'b' });
 expect(shellHistory.destination({ type: 'session', sessionId: 'c' }, 'reconcile')).toBe(false);
 await vi.waitFor(() => expect(places()).toEqual(['a', 'b']));
 expect(shellHistory.place?.sessionId).toBe('b');

 shellHistory.stop();
 const saved: ShellState = { shell: { id: 'saved', place: { type: 'session', sessionId: 'deleted' } } };
 router.index = 0; router.entries = [{ state: saved, persisted: true }]; page.state = saved;
 shellHistory.start(saved, { type: 'session', sessionId: 'deleted' });
 // The saved chat could not be restored: boot settles on a new chat.
 shellHistory.destination({ type: 'session', sessionId: null }, 'reconcile');
 await settle();
 expect(shellHistory.place?.sessionId).toBe(null);
 expect(router.entries).toEqual([{ state: saved, persisted: true }]);
});

it('does not let a refining replace overwrite the entry the browser is on after its push was rejected', async () => {
 const arrival: ShellState = { shell: { id: 'arrival', place: { type: 'session', sessionId: 'a' } } };
 router.entries[0] = { state: arrival, persisted: true }; page.state = arrival;
 shellHistory.start(arrival, null);
 router.outcomes = ['reject'];
 shellHistory.destination({ type: 'settings' });
 shellHistory.detail('settings', { settings: { category: 'general', view: 'index', depth: 0, item: null, tab: null } }, true);
 await settle(); await settle();
 expect(router.entries).toEqual([{ state: arrival, persisted: true }]);
});

it('keeps boot’s view for Back when writing it to the arrival entry was rejected', async () => {
 shellHistory.start({}, { type: 'session', sessionId: 'a' });
 await settle();
 router.outcomes = ['reject'];
 shellHistory.destination({ type: 'session', sessionId: 'b' }, 'reconcile');
 await settle();
 shellHistory.destination({ type: 'settings' });
 await vi.waitFor(() => expect(here().shell?.place.type).toBe('settings'));
 go(-1);
 expect(shellHistory.place?.sessionId).toBe('b');
});

it('keeps entries in order when a write commits late, with no timer letting a later write pass it', async () => {
 vi.useFakeTimers();
 const arrival: ShellState = { shell: { id: 'arrival', place: { type: 'session', sessionId: 'a' } } };
 router.entries[0] = { state: arrival, persisted: true }; page.state = arrival;
 shellHistory.start(arrival, null);
 router.outcomes = ['slow'];
 shellHistory.destination({ type: 'settings' });
 shellHistory.detail('settings', { panel: 'general' }, true);
 shellHistory.destination({ type: 'docs' });
 // Route resolution still holds the Settings write: nothing else has gone ahead.
 await vi.advanceTimersByTimeAsync(2500);
 expect(places()).toEqual(['a']);
 await vi.advanceTimersByTimeAsync(5000);
 expect(places()).toEqual(['a', 'settings', 'docs']);
 expect(router.entries[1].state.shell?.place.panel).toBe('general');
});

it('writes a destination again when its push was rejected, and never twice once it landed', async () => {
 const arrival: ShellState = { shell: { id: 'arrival', place: { type: 'session', sessionId: 'a' } } };
 router.entries[0] = { state: arrival, persisted: true }; page.state = arrival;
 shellHistory.start(arrival, null);
 router.outcomes = ['reject'];
 shellHistory.destination({ type: 'settings' });
 await settle();
 expect(places()).toEqual(['a']);
 // The router is writable again: selecting Settings again records it.
 shellHistory.destination({ type: 'settings' });
 await vi.waitFor(() => expect(places()).toEqual(['a', 'settings']));
 shellHistory.destination({ type: 'settings' });
 await settle();
 expect(places()).toEqual(['a', 'settings']);
});

it('after a step from the controls and a reload, going back two entries at once shows the chat (go(-2))', async () => {
 shellHistory.start({}, { type: 'session', sessionId: 'b' });
 await settle();
 pushOverlay({ mobileControls: true });
 shellHistory.destination({ type: 'settings' });
 await vi.waitFor(() => expect(router.entries).toHaveLength(3));
 await settle();
 // The overlay's own flags stay on its entry, as on main.
 expect(router.entries[1].state.mobileControls).toBe(true);
 reload({ type: 'settings' });
 expect(shellHistory.place?.type).toBe('settings');
 expect(go(-2)).toBe(true);
 expect(shellHistory.place?.sessionId).toBe('b');
 expect(go(2)).toBe(true);
 expect(shellHistory.place?.type).toBe('settings');
 // One step lands on the overlay's entry, which the reload left bare: nothing new shows, as on main.
 expect(go(-1)).toBe(false);
 expect(shellHistory.place?.type).toBe('settings');
});

it('restores a fresh load’s chat after a reload on the first step taken from it (desktop, no overlay)', async () => {
 shellHistory.start({}, { type: 'session', sessionId: 'a' });
 await settle();
 shellHistory.destination({ type: 'docs' });
 await vi.waitFor(() => expect(places()).toEqual(['a', 'docs']));
 reload({ type: 'docs' });
 go(-1);
 expect(shellHistory.place?.sessionId).toBe('a');
 go(1);
 expect(shellHistory.place?.type).toBe('docs');
});

it('writes the chat this tab shows as the landing, not the shared layout’s, and never moves the step the user chose', async () => {
 // The shared layout snapshot names a; this tab restored and shows b.
 shellHistory.start({}, { type: 'session', sessionId: 'a' });
 shellHistory.shows('b');
 shellHistory.destination({ type: 'settings' });
 shellHistory.shows('c');
 await vi.waitFor(() => expect(places()).toEqual(['b', 'settings']));
 expect(shellHistory.place?.type).toBe('settings');
 go(-1);
 expect(shellHistory.place?.sessionId).toBe('b');
});

it('corrects the saved entry on screen to New Chat once its chat proved unavailable, so a first send refines it', async () => {
 const saved: ShellState = { shell: { id: 'saved', place: { type: 'session', sessionId: 'deleted' } } };
 router.entries[0] = { state: saved, persisted: true }; page.state = saved;
 shellHistory.start(saved, { type: 'session', sessionId: 'deleted' });
 shellHistory.destination({ type: 'session', sessionId: null }, 'replace');
 await vi.waitFor(() => expect(here().shell?.place).toEqual({ type: 'session', sessionId: null }));
 expect(router.entries).toHaveLength(1);
 shellHistory.destination({ type: 'session', sessionId: 'created' });
 await vi.waitFor(() => expect(places()).toEqual(['created']));
});

it('does not land boot’s view over a step the user took while it was being written', async () => {
 router.onState = state => { shellHistory.land(state); };
 await arriveUnderOverlay();
 go(-1); // Back closes the controls...
 shellHistory.destination({ type: 'settings' }); // ...and Settings opens at once.
 await vi.waitFor(() => expect(here().shell?.place.type).toBe('settings'));
 await settle();
 expect(shellHistory.place?.type).toBe('settings');
 expect(places()).toEqual(['b', 'settings']);
});

it('writes nothing onto an overlay that opened after a write was queued; the place lands once it closes', async () => {
 startOn(saved('arrival', { type: 'session', sessionId: 'a' }));
 shellHistory.destination({ type: 'session', sessionId: 'b' });
 await vi.waitFor(() => expect(router.entries).toHaveLength(2));
 pushOverlay({ mobileDrawer: true });
 const overlay = here();
 shellHistory.detail('session', { panel: 'x' }, true);
 await settle(); await settle();
 expect(here()).toBe(overlay);
 expect(go(-1)).toBe(false);
 await vi.waitFor(() => expect(here().shell?.place.panel).toBe('x'));
 expect(router.entries).toHaveLength(3);
});

it('does not land an entry whose refining replace is still queued (a Settings deep link)', async () => {
 const landed: string[] = [];
 router.onState = (state) => { if (shellHistory.land(state)) landed.push(state.shell!.id); };
 const arrival: ShellState = { shell: { id: 'arrival', place: { type: 'automations', panel: null } } };
 router.entries[0] = { state: arrival, persisted: true }; page.state = arrival;
 shellHistory.start(arrival, null);
 // openSettings: the destination, Settings stamping its base, then go() to a detail.
 shellHistory.destination({ type: 'settings' });
 shellHistory.detail('settings', { settings: { category: 'general', view: 'index', depth: 0, item: null, tab: null } }, true);
 shellHistory.detail('settings', { settings: { category: 'providers', view: 'page', depth: 1, item: null, tab: null } });
 shellHistory.detail('settings', { settings: { category: 'providers', view: 'detail', depth: 2, item: 'provider:claude', tab: null } });
 await vi.waitFor(() => expect(router.entries).toHaveLength(4));
 await settle();
 expect(landed).toEqual([]);
 expect(shellHistory.place?.settings).toMatchObject({ view: 'detail', depth: 2 });
});

it('after a rejected push, selecting the committed place again adds no duplicate entry', async () => {
 startOn(saved('arrival', { type: 'session', sessionId: 'a' }));
 router.outcomes = ['reject'];
 shellHistory.destination({ type: 'notes' });
 await settle(); await settle();
 expect(shellHistory.place?.type).toBe('notes');
 shellHistory.destination({ type: 'session', sessionId: 'a' });
 await settle(); await settle();
 expect(router.entries).toHaveLength(1);
 expect(shellHistory.place?.sessionId).toBe('a');
 // The next step is an ordinary push again.
 shellHistory.destination({ type: 'notes' });
 await vi.waitFor(() => expect(places()).toEqual(['a', 'notes']));
});

it('retries an identical detail selection whose push was rejected, and settles a detail selection back on the committed one', async () => {
 startOn(saved('arrival', { type: 'notes', panel: 'one' }));
 router.outcomes = ['reject'];
 shellHistory.detail('notes', { panel: 'two' });
 await settle(); await settle();
 expect(router.entries).toHaveLength(1);
 shellHistory.detail('notes', { panel: 'two' });
 await vi.waitFor(() => expect(router.entries.map(({ state }) => state.shell?.place.panel)).toEqual(['one', 'two']));
 shellHistory.stop();
 startOn(saved('arrival', { type: 'notes', panel: 'one' }));
 router.outcomes = ['reject'];
 shellHistory.detail('notes', { panel: 'two' });
 await settle(); await settle();
 shellHistory.detail('notes', { panel: 'one' });
 await settle(); await settle();
 expect(router.entries).toHaveLength(1);
 expect(shellHistory.place?.panel).toBe('one');
});

it('a late “chat unavailable” for a chat Back already left touches neither the view nor the entry', async () => {
 startOn(saved('notes', { type: 'notes', panel: null }), saved('chat', { type: 'session', sessionId: 'a' }));
 // Back to Notes, reported to this module after the deletion arrives in the same flush.
 router.index = 0; page.state = here();
 expect(shellHistory.unavailable('a')).toBe(false);
 expect(shellHistory.land(here())).toBe(true);
 expect(shellHistory.unavailable('a')).toBe(false);
 await settle();
 expect(router.entries.map(({ state }) => state.shell?.place.type)).toEqual(['notes', 'session']);
});

it('a refused landing returns the browser to the entry it left, without a new stop', async () => {
 startOn(saved('a', { type: 'notes', panel: 'a' }), saved('draft', { type: 'notes', panel: 'new' }));
 expect(go(-1)).toBe(true);
 shellHistory.refuse(shellHistory.restored, shellHistory.returnPoint(shellHistory.restored));
 expect(router.index).toBe(1);
 expect(shellHistory.place?.panel).toBe('new');
 expect(router.entries).toHaveLength(2);
 // Back is ordinary again.
 expect(go(-1)).toBe(true);
 expect(shellHistory.place?.panel).toBe('a');
});

it('a refusal kept across further steps returns to the draft’s own entry, and only from the current landing', async () => {
 startOn(saved('a', { type: 'notes', panel: 'a' }), saved('b', { type: 'notes', panel: 'b' }), saved('draft', { type: 'notes', panel: 'new' }));
 expect(go(-1)).toBe(true);
 const draft = shellHistory.returnPoint(shellHistory.restored);
 const onB = shellHistory.restored;
 expect(go(-1)).toBe(true);
 // The landing on B is no longer current: refusing it moves nothing.
 shellHistory.refuse(onB, draft);
 expect(router.index).toBe(0);
 shellHistory.refuse(shellHistory.restored, draft);
 expect(router.index).toBe(2);
 expect(shellHistory.place?.panel).toBe('new');
 expect(router.entries).toHaveLength(3);
});

it('choosing the committed place drops a refinement held for its entry (S14)', async () => {
 shellHistory.start({}, { type: 'session', sessionId: 'a' });
 await settle();
 router.outcomes = ['reject'];
 shellHistory.destination({ type: 'session', sessionId: 'b' }, 'reconcile');
 await settle();
 // The user settles on A, the place the arrival entry already holds.
 shellHistory.destination({ type: 'session', sessionId: 'a' });
 shellHistory.destination({ type: 'settings' });
 await vi.waitFor(() => expect(places()).toEqual(['a', 'settings']));
 expect(go(-1)).toBe(true);
 expect(shellHistory.place?.sessionId).toBe('a');
 await settle();
 expect(places()).toEqual(['a', 'settings']);
});

it('the phone app: shell history reads and writes nothing, so main’s own writers are the only ones', async () => {
 phone.app = true;
 const arrival = saved('arrival', { type: 'session', sessionId: 'a' });
 startOn(arrival);
 shellHistory.destination({ type: 'session', sessionId: 'b' });
 shellHistory.destination({ type: 'notes' });
 shellHistory.detail('notes', { panel: 'one' });
 shellHistory.shows('c');
 expect(shellHistory.unavailable('a')).toBe(false);
 pushOverlay({ mobileDrawer: true });
 expect(go(-1)).toBe(false);
 await settle(); await settle();
 expect(router.entries).toEqual([{ state: arrival, persisted: true }, expect.anything()]);
 expect(shellHistory.place).toBeUndefined();
 expect(shellHistory.restored).toBeNull();
 // A reload on an entry the web UI saved boots the phone app as main does.
 expect(savedPlace(arrival)).toBeUndefined();
});

it('a detail selection whose replace was rejected is written again when chosen again (S20)', async () => {
 const settings = (tab: string) => ({ type: 'settings' as const, settings: { category: 'general' as never, view: 'detail' as never, item: 'x', tab, depth: 1 } });
 startOn(saved('detail', settings('one')));
 router.outcomes = ['reject'];
 shellHistory.detail('settings', settings('two'), true);
 await settle(); await settle();
 expect(here().shell?.place.settings?.tab).toBe('one');
 // Reselecting the tab on screen retries onto the same entry, so a reload restores it.
 shellHistory.detail('settings', settings('two'));
 await vi.waitFor(() => expect(here().shell?.place.settings?.tab).toBe('two'));
 expect(router.entries).toHaveLength(1);
});

it('narrow desktop: a deferred pick survives a draft note getting its id, and yields to Back or a newer pick (S18)', async () => {
 startOn(saved('notes', { type: 'notes', panel: null }), saved('draft', { type: 'notes', panel: 'new' }));
 const frames: FrameRequestCallback[] = [];
 vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => frames.push(callback));
 const frame = () => { for (const callback of frames.splice(0)) callback(0); };
 const deferred = () => { layout.setSidebarOverlay(true); layout.setSidebarCollapsed(false); const open = vi.fn(); layout.afterSidebarOverlay(open); return open; };
 try {
  const pick = deferred();
  shellHistory.detail('notes', { panel: 'n1' }, true); // the save resolves the draft's id: not navigation
  await settle();
  frame();
  expect(pick).toHaveBeenCalledOnce();
  const beforeBack = deferred();
  expect(go(-1)).toBe(true);
  frame();
  expect(beforeBack).not.toHaveBeenCalled();
  const older = deferred();
  const newer = deferred();
  frame();
  expect(older).not.toHaveBeenCalled();
  expect(newer).toHaveBeenCalledOnce();
 } finally {
  layout.setSidebarOverlay(false);
 }
});

it('narrow desktop: a first send’s chat adopted between a sidebar pick and its frame does not drop the pick (S18)', async () => {
 startOn(saved('new', { type: 'session', sessionId: null }));
 layout.openChat(null, 'reconcile');
 const frames: FrameRequestCallback[] = [];
 vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => frames.push(callback));
 try {
  layout.setSidebarOverlay(true); layout.setSidebarCollapsed(false);
  layout.afterSidebarOverlay(() => layout.openFeaturePage('docs'));
  // AppPage adopts the created chat (follow-created-chat) as the receipt arrives.
  layout.openChat('created', 'replace');
  await settle();
  for (const callback of frames.splice(0)) callback(0);
  expect(layout.current.activeView?.type).toBe('docs');
  await vi.waitFor(() => expect(places()).toEqual(['created', 'docs']));
 } finally {
  layout.setSidebarOverlay(false);
 }
});


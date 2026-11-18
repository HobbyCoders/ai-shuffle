import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { page } from '$app/state';
import { shellHistory, type ShellState } from './app-history.svelte';

// A router that keeps page state the way SvelteKit does: a shallow goto with
// persistState survives a reload, while an overlay's pushState (Chat controls,
// the drawer) does not. A landed write also becomes page.state, and AppPage's
// page.state effect runs before the navigation resolves.
type Outcome = 'reject' | 'hang';
const router = vi.hoisted(() => ({
 entries: [] as { state: ShellState; persisted: boolean }[], index: 0,
 onState: null as ((state: ShellState) => void) | null,
 outcomes: [] as ('reject' | 'hang' | null)[]
}));
vi.mock('$app/navigation', () => ({ goto: async (_url: string, options: { replace: boolean; persistState: boolean; state: ShellState }) => {
 const outcome = router.outcomes.shift();
 if (outcome === 'reject') throw new Error('Cannot call goto(...) before router is initialized');
 const entry = { state: options.state, persisted: options.persistState };
 if (options.replace) router.entries[router.index] = entry;
 else router.entries.splice(++router.index, Infinity, entry);
 page.state = options.state;
 router.onState?.(options.state);
 if (outcome === 'hang') await new Promise(() => {});
} }));
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
function reload(view: { type: 'session'; sessionId: string }) {
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
 router.onState = null; router.outcomes = [];
 shellHistory.stop();
});
afterEach(() => { vi.useRealTimers(); });

it('shows boot’s view on the overlay’s entry and the one beneath, Back and Forward, after a step taken from the overlay', async () => {
 await arriveUnderOverlay();
 shellHistory.destination({ type: 'settings' });
 await vi.waitFor(() => expect(router.entries).toHaveLength(3));
 // Each step lets queued writes run, as a person's pace does.
 const step = async (delta: number) => { go(delta); await settle(); return shellHistory.place; };
 await settle();
 expect((await step(-1))?.sessionId).toBe('b');
 expect((await step(-1))?.sessionId).toBe('b');
 expect((await step(1))?.sessionId).toBe('b');
 expect((await step(1))?.type).toBe('settings');
 expect((await step(-1))?.sessionId).toBe('b');
 expect((await step(-1))?.sessionId).toBe('b');
 // No entry holds the place boot replaced.
 expect(places()).not.toContain('a');
});

it('shows boot’s view once the overlay closes, and Back from a later step returns there', async () => {
 await arriveUnderOverlay();
 expect(go(-1)).toBe(false);
 shellHistory.destination({ type: 'settings' });
 await vi.waitFor(() => expect(places()).toEqual(['b', 'settings']));
 expect(go(-1)).toBe(true);
 expect(shellHistory.place?.sessionId).toBe('b');
});

it('keeps boot’s view after a reload on the overlay, whose own state the browser dropped', async () => {
 await arriveUnderOverlay();
 await settle();
 reload({ type: 'session', sessionId: 'b' });
 await settle();
 go(-1);
 expect(shellHistory.place?.sessionId).toBe('b');
 expect(places()).not.toContain('a');
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

it('lets later writes through when one never settles', async () => {
 vi.useFakeTimers();
 const arrival: ShellState = { shell: { id: 'arrival', place: { type: 'session', sessionId: 'a' } } };
 router.entries[0] = { state: arrival, persisted: true }; page.state = arrival;
 shellHistory.start(arrival, null);
 router.outcomes = ['hang'];
 shellHistory.destination({ type: 'settings' });
 shellHistory.destination({ type: 'docs' });
 await vi.advanceTimersByTimeAsync(5000);
 expect(places()).toEqual(['a', 'settings', 'docs']);
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

it('keeps an overlay that landed after the write was queued', async () => {
 const arrival: ShellState = { shell: { id: 'arrival', place: { type: 'session', sessionId: 'a' } } };
 router.entries[0] = { state: arrival, persisted: true }; page.state = arrival;
 shellHistory.start(arrival, null);
 shellHistory.destination({ type: 'session', sessionId: 'b' });
 await vi.waitFor(() => expect(router.entries).toHaveLength(2));
 pushOverlay({ mobileDrawer: true });
 shellHistory.detail('session', { panel: 'x' }, true);
 await vi.waitFor(() => expect(here().shell?.place.panel).toBe('x'));
 expect(here().mobileDrawer).toBe(true);
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

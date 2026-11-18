import { beforeEach, expect, it, vi } from 'vitest';
import { page } from '$app/state';
import { shellHistory, type ShellState } from './app-history.svelte';

// A router whose entries are what a phone overlay and the shell history write:
// a landed write also becomes page.state, as SvelteKit does.
const router = vi.hoisted(() => ({ entries: [] as ShellState[], index: 0 }));
vi.mock('$app/navigation', () => ({ goto: async (_url: string, options: { replace: boolean; state: ShellState }) => {
 if (options.replace) router.entries[router.index] = options.state;
 else router.entries.splice(++router.index, Infinity, options.state);
 page.state = options.state;
} }));
const here = () => router.entries[router.index];
/** An overlay's own entry, as the Chat controls sheet and the drawer push it. */
function pushOverlay(state: App.PageState) {
 router.entries.splice(++router.index, Infinity, { ...page.state, ...state } as ShellState);
 page.state = here();
}
function back() {
 router.index--;
 page.state = here();
 return shellHistory.land(here());
}

beforeEach(() => {
 router.index = 0; router.entries = [{}]; page.state = {};
 shellHistory.stop();
});

it('gives the arrival entry its place at once, so an overlay opened during boot sits on a stamped entry', async () => {
 shellHistory.start({}, { type: 'session', sessionId: 'saved' });
 await vi.waitFor(() => expect(here().shell?.place.sessionId).toBe('saved'));
 pushOverlay({ mobileControls: true });
 pushOverlay({ mobileControls: true, mobileControlsPage: 'usage' });
 expect(router.entries.map(e => e.shell?.id)).toEqual(Array(3).fill(shellHistory.entryId));
});

it('corrects the arrival entry under an overlay only once the overlay closes, so Back from a later step returns to the correction', async () => {
 shellHistory.start({}, { type: 'session', sessionId: 'saved' });
 await vi.waitFor(() => expect(here().shell).toBeDefined());
 const arrival = shellHistory.entryId;
 pushOverlay({ mobileControls: true });
 shellHistory.destination({ type: 'session', sessionId: 'restored' }, 'reconcile');
 expect(shellHistory.place?.sessionId).toBe('restored');
 await new Promise(resolve => setTimeout(resolve));
 // The overlay keeps its own entry: it is never a stand-in for the arrival.
 expect(router.entries.map(e => e.shell?.place.sessionId)).toEqual(['saved', 'saved']);
 // Back closes the overlay only, and the arrival entry now holds boot's view.
 expect(back()).toBe(false);
 await vi.waitFor(() => expect(router.entries[0].shell).toEqual({ id: arrival, place: { type: 'session', sessionId: 'restored' } }));
 shellHistory.destination({ type: 'settings' });
 await vi.waitFor(() => expect(router.entries.map(e => e.shell?.place.type)).toEqual(['session', 'settings']));
 expect(back()).toBe(true);
 expect(shellHistory.place?.sessionId).toBe('restored');
});

it('lets a step taken from the overlay keep the correction for when Back reaches the arrival', async () => {
 shellHistory.start({}, { type: 'session', sessionId: 'saved' });
 await vi.waitFor(() => expect(here().shell).toBeDefined());
 pushOverlay({ mobileControls: true });
 shellHistory.destination({ type: 'session', sessionId: 'restored' }, 'reconcile');
 shellHistory.destination({ type: 'settings' });
 await vi.waitFor(() => expect(router.entries).toHaveLength(3));
 // Back to the overlay's copy shows boot's view, not the copy's old one.
 expect(back()).toBe(true);
 expect(shellHistory.place?.sessionId).toBe('restored');
 expect(back()).toBe(false);
 await vi.waitFor(() => expect(router.entries[0].shell?.place.sessionId).toBe('restored'));
});

it('never lets boot correct a stop the user made, or an entry the browser restored', async () => {
 shellHistory.start({}, { type: 'session', sessionId: 'a' });
 shellHistory.destination({ type: 'session', sessionId: 'b' });
 shellHistory.destination({ type: 'session', sessionId: 'c' }, 'reconcile');
 await vi.waitFor(() => expect(router.entries).toHaveLength(2));
 expect(router.entries.map(e => e.shell?.place.sessionId)).toEqual(['a', 'b']);
 expect(shellHistory.place?.sessionId).toBe('b');

 shellHistory.stop();
 const saved: ShellState = { shell: { id: 'saved', place: { type: 'docs', doc: 'guide/start' } } };
 router.index = 0; router.entries = [saved]; page.state = saved;
 shellHistory.start(saved, { type: 'session', sessionId: 'a' });
 shellHistory.destination({ type: 'session', sessionId: 'a' }, 'reconcile');
 await new Promise(resolve => setTimeout(resolve));
 expect(router.entries).toEqual([saved]);
 expect(shellHistory.place).toEqual(saved.shell!.place);
});

it('keeps an overlay that landed after the write was queued', async () => {
 const arrival: ShellState = { shell: { id: 'arrival', place: { type: 'session', sessionId: 'a' } } };
 router.entries[0] = page.state = arrival;
 shellHistory.start(arrival, null);
 shellHistory.destination({ type: 'session', sessionId: 'b' });
 await vi.waitFor(() => expect(router.entries).toHaveLength(2));
 pushOverlay({ mobileDrawer: true });
 shellHistory.detail('session', { panel: 'x' }, true);
 await vi.waitFor(() => expect(here().shell?.place.panel).toBe('x'));
 expect(here().mobileDrawer).toBe(true);
});

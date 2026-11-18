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
 shellHistory.start({}, { type: 'session', sessionId: 'saved' }, true);
 await vi.waitFor(() => expect(here().shell?.place.sessionId).toBe('saved'));
 pushOverlay({ mobileControls: true });
 pushOverlay({ mobileControls: true, mobileControlsPage: 'usage' });
 expect(router.entries.map(e => e.shell?.id)).toEqual(Array(3).fill(shellHistory.entryId));
});

it('lets boot refine the arrival place under an open overlay without closing it or adding a stop', async () => {
 shellHistory.start({}, { type: 'session', sessionId: 'saved' }, true);
 await vi.waitFor(() => expect(here().shell).toBeDefined());
 const arrival = shellHistory.entryId;
 pushOverlay({ mobileControls: true });
 // Boot reconciles to the server's session, and a tap during boot moves on.
 shellHistory.destination({ type: 'session', sessionId: 'restored' });
 shellHistory.settle({ type: 'session', sessionId: 'restored' });
 await vi.waitFor(() => expect(here().shell?.place.sessionId).toBe('restored'));
 expect(router.entries).toHaveLength(2);
 expect(here()).toMatchObject({ mobileControls: true, shell: { id: arrival } });
 // Back closes the overlay only: the entry beneath is the same entry, not a move.
 expect(back()).toBe(false);
 expect(shellHistory.place?.sessionId).toBe('restored');
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

it('adds steps again once boot settled', async () => {
 shellHistory.start({}, { type: 'session', sessionId: 'a' }, true);
 shellHistory.settle({ type: 'session', sessionId: 'a' });
 shellHistory.destination({ type: 'session', sessionId: 'b' });
 await vi.waitFor(() => expect(router.entries).toHaveLength(2));
 expect(back()).toBe(true);
 expect(shellHistory.place?.sessionId).toBe('a');
});

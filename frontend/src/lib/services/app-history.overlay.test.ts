/**
 * Overlays (the phone Chat controls sheet and drawer) over the shell history.
 * They can open while boot still loads, before AppPage starts the shell
 * history, and every write lands asynchronously, in order. An overlay owns a
 * Back entry as soon as it can be used; the start place keeps it and claims
 * the boot entry under it without adding a stop the user never visited.
 */
import { beforeEach, expect, it, vi } from 'vitest';
import { shellHistory, type ShellState } from './app-history.svelte';

const router = vi.hoisted(() => ({
	entries: [] as ShellState[], index: 0, hold: null as Promise<void> | null, calls: 0, drop: false,
	page: { state: {} as ShellState }
}));
vi.mock('$app/state', () => ({ page: router.page }));
vi.mock('$app/navigation', () => ({ goto: async (_url: string, options: { replace: boolean; state: ShellState }) => {
	router.calls++;
	if (router.hold) await router.hold;
	if (router.drop) return; // a cancelled navigation resolves without a write
	if (options.replace) router.entries[router.index] = options.state;
	else router.entries.splice(++router.index, Infinity, options.state);
	router.page.state = { ...options.state };
} }));
const here = () => router.entries[router.index];
/** A browser Back or Forward: the entry lands and AppPage hears it. */
function traverse(delta: number) {
	router.index += delta;
	router.page.state = { ...here() };
	return shellHistory.land(here());
}
const settle = () => vi.waitFor(() => expect(router.calls).toBeGreaterThan(0)).then(() => new Promise(resolve => setTimeout(resolve, 0)));
const session = { type: 'session', sessionId: null } as const;

beforeEach(() => {
	router.entries = [{}]; router.index = 0; router.hold = null; router.calls = 0; router.drop = false;
	router.page.state = {};
	shellHistory.stop();
	vi.spyOn(history, 'go').mockImplementation(delta => { traverse(delta ?? 0); });
});

it('records no overlay before the app arms it, and says so', async () => {
	expect(shellHistory.overlay({ mobileDrawer: true })).toBe(false);
	await Promise.resolve();
	expect(router.calls).toBe(0);
});

it('gives an overlay opened during boot its Back entry, which the start place keeps', async () => {
	shellHistory.arm();
	expect(shellHistory.overlay({ mobileControls: true, mobileControlsId: 'list' })).toBe(true);
	await settle();
	expect(router.entries).toEqual([{}, { mobileControls: true, mobileControlsId: 'list' }]);

	// Boot finishes with the sheet still open: no stop is added, the overlay stays.
	shellHistory.start(router.page.state, session);
	await vi.waitFor(() => expect(here().shell).toBeDefined());
	expect(router.entries).toHaveLength(2);
	expect(here()).toMatchObject({ mobileControls: true, mobileControlsId: 'list', shell: { place: session } });

	// Back closes it onto the boot entry, which takes the start place: not a move.
	expect(traverse(-1)).toBe(false);
	await vi.waitFor(() => expect(router.entries[0].shell?.place).toEqual(session));
	expect(router.entries).toHaveLength(2);
	expect(router.entries[0].mobileControls).toBeUndefined();
	expect(shellHistory.entryId).toBe(router.entries[0].shell!.id);
	// Forward reopens the overlay over the same place.
	expect(traverse(1)).toBe(true);
	expect(shellHistory.place).toEqual(session);
});

it('lets Back close an overlay before boot is released, then starts on the boot entry', async () => {
	shellHistory.arm();
	shellHistory.overlay({ mobileControls: true, mobileControlsId: 'list' });
	await settle();
	expect(traverse(-1)).toBe(false);
	shellHistory.start(router.page.state, session);
	await vi.waitFor(() => expect(router.entries[0].shell?.place).toEqual(session));
	expect(router.index).toBe(0);
	expect(router.entries).toHaveLength(2);
	expect(router.entries[0].mobileControls).toBeUndefined();
});

it('reports an overlay push the router drops or rejects', async () => {
	shellHistory.arm();
	const failed = vi.fn();
	router.drop = true;
	vi.spyOn(console, 'warn').mockImplementation(() => {});
	expect(shellHistory.overlay({ mobileControls: true, mobileControlsId: 'early' }, failed)).toBe(true);
	await vi.waitFor(() => expect(failed).toHaveBeenCalledTimes(1));
	shellHistory.start({}, session);
	router.drop = false;
	router.hold = Promise.reject(new Error('router gone'));
	router.hold.catch(() => {});
	shellHistory.overlay({ mobileControls: true, mobileControlsId: 'started' }, failed);
	await vi.waitFor(() => expect(failed).toHaveBeenCalledTimes(2));
});

it('backs off only the entries an overlay owns, once they committed', async () => {
	shellHistory.arm();
	let release!: () => void;
	router.hold = new Promise(resolve => (release = resolve));
	shellHistory.overlay({ mobileControls: true, mobileControlsId: 'list' });
	shellHistory.overlay({ mobileControls: true, mobileControlsId: 'page', mobileControlsList: 'list' });
	const owned = (id?: string) => id === 'list' || id === 'page';
	// Closed while both pushes are still queued: nothing to pass yet.
	shellHistory.retire(state => (owned(state.mobileControlsId) ? 1 + Number(owned(state.mobileControlsList)) : 0));
	expect(history.go).not.toHaveBeenCalled();
	release();
	await vi.waitFor(() => expect(history.go).toHaveBeenCalledExactlyOnceWith(-2));
	expect(router.index).toBe(0);

	// A Back already taken: the landed entry is not the overlay's, so nothing more.
	shellHistory.retire(state => (owned(state.mobileControlsId) ? 1 : 0));
	await settle();
	expect(history.go).toHaveBeenCalledTimes(1);
});

it('knows the landing of an own write that a later write already replaced', async () => {
	let release!: () => void;
	router.hold = new Promise(resolve => (release = resolve));
	shellHistory.arm();
	const early = { mobileControls: true, mobileControlsId: 'list' };
	shellHistory.overlay(early);
	shellHistory.start({}, session);
	const first = shellHistory.entryId!;
	shellHistory.overlay({ mobileControls: true, mobileControlsId: 'page' });
	const firstLanding: ShellState = { shell: { id: first, place: session } };
	// Still queued under later writes: their landings are not navigations.
	expect(shellHistory.superseded(early)).toBe(true);
	expect(shellHistory.superseded(firstLanding)).toBe(true);
	release();
	await vi.waitFor(() => expect(router.entries).toHaveLength(3));
	// Settled, or a Back to it (land makes it current): a real landing.
	expect(shellHistory.superseded(early)).toBe(false);
	expect(shellHistory.superseded(firstLanding)).toBe(false);
	expect(shellHistory.superseded(here())).toBe(false);
	expect(shellHistory.superseded({})).toBe(false);
});

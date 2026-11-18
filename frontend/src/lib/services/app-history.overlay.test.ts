/**
 * Overlays (the phone Chat controls sheet's entries) over the shell history.
 * The sheet can open while boot still loads, before AppPage starts the shell
 * history, and shell writes land asynchronously, in order. The sheet must know
 * when nothing was recorded, and must not read an earlier write's landing as Back.
 */
import { beforeEach, expect, it, vi } from 'vitest';
import { shellHistory, type ShellState } from './app-history.svelte';

const router = vi.hoisted(() => ({ entries: [] as ShellState[], index: 0, hold: null as Promise<void> | null, calls: 0 }));
vi.mock('$app/navigation', () => ({ goto: async (_url: string, options: { replace: boolean; state: ShellState }) => {
	router.calls++;
	if (router.hold) await router.hold;
	if (options.replace) router.entries[router.index] = options.state;
	else router.entries.splice(++router.index, Infinity, options.state);
} }));
const here = () => router.entries[router.index];

beforeEach(() => {
	router.entries = [{}]; router.index = 0; router.hold = null; router.calls = 0;
	shellHistory.stop();
});

it('records no overlay before the shell history starts, and says so', async () => {
	expect(shellHistory.overlay({ mobileControls: true })).toBe(false);
	await Promise.resolve();
	expect(router.calls).toBe(0);

	shellHistory.start({}, { type: 'session', sessionId: null });
	expect(shellHistory.overlay({ mobileControls: true })).toBe(true);
	await vi.waitFor(() => expect(router.entries).toHaveLength(2));
	expect(here().mobileControls).toBe(true);
	expect(here().shell?.place).toEqual({ type: 'session', sessionId: null });
});

it('knows the landing of an own write that a later write already replaced', async () => {
	let release!: () => void;
	router.hold = new Promise(resolve => (release = resolve));
	shellHistory.start({}, { type: 'session', sessionId: null });
	const first = shellHistory.entryId!;
	shellHistory.overlay({ mobileControls: true });
	const firstLanding: ShellState = { shell: { id: first, place: { type: 'session', sessionId: null } } };
	// Still queued under the overlay: its landing is not a navigation.
	expect(shellHistory.superseded(firstLanding)).toBe(true);
	release();
	await vi.waitFor(() => expect(router.entries).toHaveLength(2));
	// Settled, or a Back to it (land makes it current): a real landing.
	expect(shellHistory.superseded(firstLanding)).toBe(false);
	expect(shellHistory.superseded(here())).toBe(false);
	expect(shellHistory.superseded({})).toBe(false);
});

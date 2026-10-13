import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	patchSession: vi.fn(),
	dismissSettledPrompts: vi.fn(),
	snapshotPrompts: vi.fn((id: string) => ({ attention: undefined, requestIds: new Set([`req-${id}`]) })),
}));

vi.mock('$lib/api/client', () => ({
	api: {
		get: mocks.get,
		post: vi.fn(),
		put: vi.fn(),
		delete: vi.fn(),
	},
	searchSessionsPaginated: vi.fn(),
	renameSession: vi.fn(),
	setSessionActive: mocks.patchSession,
}));

vi.mock('./active-session', () => ({
	activeSession: {
		get current() { return { sessionId: null as string | null }; },
		newChat: vi.fn(),
		validateSelectedProfile: vi.fn(),
		dismissSettledPrompts: mocks.dismissSettledPrompts,
		snapshotPrompts: mocks.snapshotPrompts,
	},
}));

import { appData } from './app-data.svelte';

// Server-side rows keyed by id, so a test can flip is_active mid-flight to
// simulate the PATCH landing while an older list request is still out.
const serverRows: Record<string, { id: string; is_active: boolean }> = {
	'session-a': { id: 'session-a', is_active: true },
	'session-b': { id: 'session-b', is_active: false },
};

beforeEach(() => {
	vi.clearAllMocks();
	for (const row of Object.values(serverRows)) row.is_active = row.id === 'session-a';

	// The cursor endpoint returns a snapshot of the CURRENT server rows. Each
	// call also takes at least one macrotask, so an in-flight load started
	// before a PATCH can observe the PRE-patch rows.
	mocks.get.mockImplementation(async (url: string) => {
		if (url.startsWith('/sessions/cursor')) {
			await new Promise((r) => setTimeout(r, 0));
			return {
				items: Object.values(serverRows).map((r) => ({ ...r })),
				next_cursor: null,
				next_cursor_id: null,
				has_more: false,
			};
		}
		return [];
	});
	mocks.patchSession.mockImplementation(async (_id: string, isActive: boolean) => {
		await new Promise((r) => setTimeout(r, 0));
		serverRows['session-b'].is_active = isActive;
		return { ...serverRows['session-b'] };
	});
});

describe('appData.setSessionActive vs stale list loads', () => {
	it('an is_active patch is merged into the list without a refetch', async () => {
		await appData.loadSessions();

		expect(appData.mergeSessionPatch('session-b', { is_active: true })).toBe(true);
		expect(appData.current.sessions.find((s) => s.id === 'session-b')?.is_active).toBe(true);
		expect(appData.mergeSessionPatch('missing', { is_active: true })).toBe(false);

		// A server-pushed patch is also held against in-flight list pages until a
		// load started after it commits (W7) — and the fake server here never
		// learns about it. Patch the row back so this test leaves the shared
		// module state exactly as it found it.
		appData.mergeSessionPatch('session-b', { is_active: false });
	});

	it('a paginated load already in flight when the user settles cannot flip the card back', async () => {
		await appData.loadSessions();
		expect(appData.current.sessions.find((s) => s.id === 'session-b')?.is_active).toBe(false);

		// Make the PATCH slow so the stale load's page fetch (fast) resolves
		// while it is still pending — exactly the round-trip window where the
		// user watches the card snap back.
		const fastPatch = mocks.patchSession.getMockImplementation();
		mocks.patchSession.mockImplementationOnce(async (id, active) => {
			await new Promise((r) => setTimeout(r, 30));
			return fastPatch!(id, active);
		});

		// Start a full list refresh (e.g. the turn-done reload) whose page
		// fetch resolves BEFORE the PATCH — carrying PRE-patch rows. It must
		// be discarded instead of committing them over the optimistic flip.
		const staleLoad = appData.loadSessions();
		await new Promise((r) => setTimeout(r, 0));
		const settle = appData.setSessionActive('session-b', true);

		await new Promise((r) => setTimeout(r, 10));
		// Mid-PATCH: the stale page has resolved by now. The card must still
		// show the optimistic flip — no transient reversion.
		expect(appData.current.sessions.find((s) => s.id === 'session-b')?.is_active).toBe(true);

		await settle;
		await staleLoad;
		// Still active after everything settles — no reversion.
		expect(appData.current.sessions.find((s) => s.id === 'session-b')?.is_active).toBe(true);
	});

	it('rolls the optimistic flip back when the PATCH fails', async () => {
		await appData.loadSessions();
		mocks.patchSession.mockRejectedValueOnce(new Error('network down'));

		await expect(appData.setSessionActive('session-b', true)).rejects.toThrow('network down');
		expect(appData.current.sessions.find((s) => s.id === 'session-b')?.is_active).toBe(false);
	});
});

// #2324: a confirmed settle dismisses the session's waits locally; unsettle
// and a failed settle leave them alone.
describe('appData.setSessionActive dismisses prompts on settle', () => {
	it('dismisses the settled session\'s prompts once the PATCH succeeds', async () => {
		await appData.loadSessions();
		await appData.setSessionActive('session-b', false);
		// Scoped to what existed when the settle was sent, not whatever is
		// pending once the PATCH resolves.
		expect(mocks.snapshotPrompts).toHaveBeenCalledWith('session-b');
		expect(mocks.dismissSettledPrompts).toHaveBeenCalledWith('session-b', {
			attention: undefined,
			requestIds: new Set(['req-session-b']),
		});
	});

	it('dismisses nothing on unsettle', async () => {
		await appData.loadSessions();
		await appData.setSessionActive('session-b', true);
		expect(mocks.dismissSettledPrompts).not.toHaveBeenCalled();
		await appData.setSessionActive('session-b', false);
	});

	it('dismisses nothing when the settle fails', async () => {
		await appData.loadSessions();
		mocks.patchSession.mockRejectedValueOnce(new Error('network down'));
		await expect(appData.setSessionActive('session-a', false)).rejects.toThrow('network down');
		expect(mocks.dismissSettledPrompts).not.toHaveBeenCalled();
	});
});

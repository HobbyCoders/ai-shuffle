/**
 * Session-list loading policy (PERFORMANCE_REVIEW_2026-08 W1 / S12).
 *
 * Pins the three properties the sidebar's refresh model depends on:
 *  - the initial load is CAPPED at one cursor page (the rest is behind an
 *    explicit "load more"), instead of paging through the whole history,
 *  - a load commits ONCE and keeps the previous array identity when nothing
 *    changed, instead of committing a fresh array per page,
 *  - optimistic local edits (rename, settle, delete) are re-applied on top of
 *    every committed page, so a load that was already in flight when the user
 *    acted cannot commit pre-mutation rows over them.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	delete: vi.fn(),
	renameSession: vi.fn(),
}));

vi.mock('$lib/api/client', () => ({
	api: {
		get: mocks.get,
		post: vi.fn(),
		put: vi.fn(),
		delete: mocks.delete,
	},
	searchSessionsPaginated: vi.fn(),
	renameSession: mocks.renameSession,
	setSessionActive: vi.fn(),
}));

vi.mock('./active-session', () => ({
	activeSession: {
		get current() { return { sessionId: null as string | null }; },
		newChat: vi.fn(),
		validateSelectedProfile: vi.fn(),
	},
}));

import { appData, sessionListsEqual } from './app-data.svelte';

interface Row { id: string; title: string; is_active: boolean }

/** Two 100-row pages, so an uncapped loader would make two round-trips. */
const page1: Row[] = Array.from({ length: 100 }, (_, i) => ({
	id: `s${i}`,
	title: `Session ${i}`,
	is_active: i === 0,
}));
/** An Active session old enough to sit on the SECOND page. */
const oldActive: Row = { id: 'old-active', title: 'Long-idle active chat', is_active: true };
const page2: Row[] = [
	...Array.from({ length: 39 }, (_, i) => ({
		id: `s${100 + i}`,
		title: `Session ${100 + i}`,
		is_active: false,
	})),
	oldActive,
];

/** Server-side titles, so a test can watch a rename land (or not). */
let serverTitles: Record<string, string> = {};
/** Delay applied to the cursor endpoint, to open the in-flight window. */
let listDelayMs = 0;

function rowsFor(page: Row[]): Row[] {
	return page.map((r) => ({ ...r, title: serverTitles[r.id] ?? r.title }));
}

beforeEach(() => {
	vi.clearAllMocks();
	serverTitles = {};
	listDelayMs = 0;
	mocks.get.mockImplementation(async (url: string) => {
		if (url.startsWith('/sessions/cursor')) {
			if (listDelayMs) await new Promise((r) => setTimeout(r, listDelayMs));
			// The Active group is pulled in separately so the page cap can never
			// hide a long-idle active session.
			if (url.includes('active_only=true')) {
				return {
					items: rowsFor([...page1, ...page2].filter((r) => r.is_active)),
					next_cursor: null,
					next_cursor_id: null,
					has_more: false,
				};
			}
			const isSecondPage = url.includes('cursor=c1');
			return isSecondPage
				? { items: rowsFor(page2), next_cursor: null, next_cursor_id: null, has_more: false }
				: { items: rowsFor(page1), next_cursor: 'c1', next_cursor_id: 'i1', has_more: true };
		}
		return [];
	});
	mocks.renameSession.mockImplementation(async (id: string, title: string) => {
		await new Promise((r) => setTimeout(r, 0));
		serverTitles[id] = title;
		return { id, title };
	});
	mocks.delete.mockResolvedValue(undefined);
});

describe('capped initial load (W1)', () => {
	it('fetches ONE page up front (plus the Active set) and reports more', async () => {
		await appData.loadSessions();

		// One page + one active-only request, in parallel — not ⌈N/100⌉
		// sequential round-trips.
		expect(mocks.get).toHaveBeenCalledTimes(2);
		expect(appData.current.sessions).toHaveLength(101);
		expect(appData.current.sessionsHasMore).toBe(true);
		expect(appData.current.sessionsCursor).toBe('c1');
	});

	it('keeps a long-idle Active session that the capped page missed', async () => {
		await appData.loadSessions();

		expect(appData.current.sessions.some((s) => s.id === 'old-active')).toBe(true);
		// …and the leading rows stay strictly most-recent-first.
		expect(appData.current.sessions[0].id).toBe('s0');
	});

	it('appends the next page without duplicating rows already held', async () => {
		await appData.loadSessions();
		await appData.loadMoreSessions();

		expect(appData.current.sessions).toHaveLength(140);
		expect(appData.current.sessions.filter((s) => s.id === 'old-active')).toHaveLength(1);
		expect(appData.current.sessionsHasMore).toBe(false);
	});

	it('keeps the previous array identity when a reload changes nothing', async () => {
		await appData.loadSessions();
		const first = appData.current.sessions;

		await appData.loadSessions();

		// Committing a fresh array per load hands every subscriber a new identity
		// and re-derives the whole sidebar (filter + map + sort) for nothing.
		expect(appData.current.sessions).toBe(first);
	});

	it('commits a new array when a row actually changed', async () => {
		await appData.loadSessions();
		const first = appData.current.sessions;

		serverTitles['s5'] = 'Changed on the server';
		await appData.loadSessions();

		expect(appData.current.sessions).not.toBe(first);
		expect(appData.current.sessions.find((s) => s.id === 's5')?.title).toBe('Changed on the server');
	});
});

describe('sessionListsEqual', () => {
	it('compares rows by value, including array fields', () => {
		const a = [{ id: 'x', tags: [{ id: 't' }] }] as never;
		const b = [{ id: 'x', tags: [{ id: 't' }] }] as never;
		expect(sessionListsEqual(a, b)).toBe(true);
		const c = [{ id: 'x', tags: [{ id: 'other' }] }] as never;
		expect(sessionListsEqual(a, c)).toBe(false);
	});
});

describe('optimistic edits vs in-flight loads (S12)', () => {
	it('a rename survives a list load that was already paging', async () => {
		await appData.loadSessions();

		// A load that will resolve with PRE-rename rows.
		listDelayMs = 20;
		const staleLoad = appData.loadSessions();
		await new Promise((r) => setTimeout(r, 0));

		const rename = appData.renameSession('s5', 'Renamed by the user');
		expect(appData.current.sessions.find((s) => s.id === 's5')?.title).toBe('Renamed by the user');

		await staleLoad;
		// The stale page carried "Session 5" — the optimistic title must survive.
		expect(appData.current.sessions.find((s) => s.id === 's5')?.title).toBe('Renamed by the user');

		await rename;
		listDelayMs = 0;
		await appData.loadSessions();
		expect(appData.current.sessions.find((s) => s.id === 's5')?.title).toBe('Renamed by the user');
	});

	it('rolls a failed rename back and lets the server title win again', async () => {
		await appData.loadSessions();
		mocks.renameSession.mockRejectedValueOnce(new Error('nope'));

		await expect(appData.renameSession('s5', 'Doomed')).rejects.toThrow('nope');
		expect(appData.current.sessions.find((s) => s.id === 's5')?.title).toBe('Session 5');

		await appData.loadSessions();
		expect(appData.current.sessions.find((s) => s.id === 's5')?.title).toBe('Session 5');
	});

	it('a delete is not resurrected by a load that was already in flight', async () => {
		await appData.loadSessions();

		listDelayMs = 20;
		const staleLoad = appData.loadSessions();
		await new Promise((r) => setTimeout(r, 0));

		const removal = appData.deleteSession('s7');
		expect(appData.current.sessions.some((s) => s.id === 's7')).toBe(false);

		await staleLoad;
		expect(appData.current.sessions.some((s) => s.id === 's7')).toBe(false);
		await removal;
		expect(appData.current.sessions.some((s) => s.id === 's7')).toBe(false);
	});

	it('a server-pushed row patch is not clobbered by an in-flight page (W7)', async () => {
		await appData.loadSessions();

		listDelayMs = 20;
		const staleLoad = appData.loadSessions();
		await new Promise((r) => setTimeout(r, 0));

		expect(appData.mergeSessionPatch('s3', { is_active: true })).toBe(true);

		await staleLoad;
		expect(appData.current.sessions.find((s) => s.id === 's3')?.is_active).toBe(true);
	});
});

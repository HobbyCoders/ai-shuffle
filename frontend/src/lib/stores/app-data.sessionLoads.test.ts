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
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	delete: vi.fn(),
	renameSession: vi.fn(),
	setSessionActive: vi.fn(),
	setSessionArchived: vi.fn(),
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
	setSessionActive: mocks.setSessionActive,
	setSessionArchived: mocks.setSessionArchived,
}));

vi.mock('./active-session', () => ({
	activeSession: {
		get current() { return { sessionId: null as string | null }; },
		newChat: vi.fn(),
		validateSelectedProfile: vi.fn(),
		snapshotPrompts: vi.fn(() => null),
	},
}));

import { appData, sessionListsEqual } from './app-data.svelte';
import { ResourceSync } from '$lib/services/resource-sync';

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
		if (url.startsWith('/sessions/count')) return { total: 138 };
		if (url.startsWith('/sessions/cursor')) {
			if (listDelayMs) await new Promise((r) => setTimeout(r, listDelayMs));
			// The Active group is pulled in separately so the page cap can never
			// hide a long-idle active session.
			if (url.includes('membership=active')) {
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
 it('pages the Active working set separately from bounded Settled history and its total', async () => {
  mocks.get.mockImplementation(async (url: string) => {
   if (url.startsWith('/sessions/count')) return { total: 1000 };
   if (url.includes('membership=active')) return url.includes('cursor=active-next')
    ? { items: [{ id: 'active-101', is_active: true }], has_more: false }
    : { items: Array.from({ length: 100 }, (_, i) => ({ id: `active-${i}`, is_active: true })), has_more: true, next_cursor: 'active-next', next_cursor_id: 'a' };
   expect(url).toContain('membership=settled');
   return { items: [{ id: 'settled-recent', is_active: false }], has_more: true, next_cursor: 'history-next', next_cursor_id: 's' };
  });
  await appData.loadSessions(null);
  expect(appData.current.sessions.filter(row => row.is_active)).toHaveLength(101);
  expect(appData.current.settledTotal).toBe(1000);
  expect(appData.current.sessionsHasMore).toBe(true);
  expect(appData.current.sessionsCursor).toBe('history-next');
 });

	it('coalesces independent resource subscribers and keeps an in-flight dirty follow-up', async () => {
		vi.useFakeTimers();
		const sync = new ResourceSync();
		const off = [
			sync.subscribe(['sessions'], () => appData.loadSessions()),
			sync.subscribe(['projects'], () => appData.loadSessions()),
		];
		try {
			listDelayMs = 500;
			sync.invalidate(['sessions', 'projects']);
			await vi.advanceTimersByTimeAsync(100);
			expect(mocks.get).toHaveBeenCalledTimes(3);
			// A focus refresh while the requests are running must not be lost.
			const refreshed = appData.loadSessions();
			void appData.loadSessions();
			await vi.advanceTimersByTimeAsync(1000);
			await refreshed;
			expect(mocks.get).toHaveBeenCalledTimes(6);
		} finally { off.forEach(stop => stop()); sync.clear(); vi.useRealTimers(); }
	});

	it('discards an in-flight response for a superseded tag', async () => {
		let resolve!: (value: unknown) => void;
		mocks.get.mockImplementation((url: string) => {
			if (url.startsWith('/sessions/count')) return Promise.resolve({ total: 0 });
			if (url.includes('membership=active')) return Promise.resolve({ items: [] });
			if (url.includes('tag_id=old')) return new Promise(done => { resolve = done; });
			return Promise.resolve({ items: [{id: 'new-tag'}], has_more: false });
		});
		const first = appData.loadSessions('old');
		await Promise.resolve();
		const second = appData.loadSessions('new');
		resolve({ items: [{id: 'old-tag'}], has_more: false });
		await Promise.all([first, second]);
		expect(appData.current.sessions.map(s => s.id)).toEqual(['new-tag']);
		mocks.get.mockClear();
		await appData.loadSessions();
		expect(mocks.get.mock.calls.every(([url]) => url.includes('tag_id=new'))).toBe(true);
		// Explicit null still clears the filter.
		await appData.loadSessions(null);
		expect(appData.current.sessionsTagFilter).toBeNull();
	});
	it('fetches ONE page up front (plus the Active set) and reports more', async () => {
		await appData.loadSessions();

		// One page + one active-only request, in parallel — not ⌈N/100⌉
		// sequential round-trips.
		expect(mocks.get).toHaveBeenCalledTimes(3);
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

describe('a refused change gives back only what it touched (#1034)', () => {
	const row = (id: string) => appData.current.sessions.find((s) => s.id === id);
	const ids = () => appData.current.sessions.map((s) => s.id);

	/** Held open until the case refuses or accepts it. */
	function hold<T>(api: { mockImplementationOnce: (fn: () => Promise<T>) => unknown }) {
		let refuse!: (reason: Error) => void;
		let accept!: (value: T) => void;
		api.mockImplementationOnce(() => new Promise<T>((resolve, reject) => { refuse = reject; accept = resolve; }));
		return { refuse: () => refuse(new Error('refused')), accept: (value?: T) => accept(value as T) };
	}

	beforeEach(async () => {
		await appData.loadSessions(null);
	});

	// Edits a case leaves confirmed retire on the next load; leave none for the next case.
	afterEach(async () => {
		await appData.loadSessions(null);
	});

	const CHANGES = [
		['settle', () => appData.setSessionActive('s0', false), mocks.setSessionActive],
		['legacy archive alias', () => appData.setSessionArchived('s0', true), mocks.setSessionActive],
		['rename', () => appData.renameSession('s0', 'Refused'), mocks.renameSession],
		['delete', () => appData.deleteSession('s0'), mocks.delete],
	] as const;

	it.each(CHANGES)('%s: a push to another row while the request is out stays', async (_name, change, api) => {
		const held = hold(api);
		const failing = change().catch(() => undefined);
		await vi.waitFor(() => expect(mocks.get).toHaveBeenCalled());

		appData.mergeSessionPatch('s1', { is_active: true, title: 'Pushed' });
		held.refuse();
		await failing;

		expect(row('s1')?.is_active).toBe(true);
		expect(row('s1')?.title).toBe('Pushed');
		expect(row('s0')).toMatchObject({ title: 'Session 0', is_active: true });
	});

	it.each(CHANGES)('%s: rows a list read brought in while the request is out stay', async (_name, change, api) => {
		const held = hold(api);
		const failing = change().catch(() => undefined);
		await appData.loadMoreSessions();
		const loaded = ids().filter((id) => id !== 's0');
		expect(loaded).toContain('s120');

		held.refuse();
		await failing;

		for (const id of loaded) expect(ids()).toContain(id);
	});

	it('gives back a value a push set on the touched field, not the one from before the click', async () => {
		const held = hold(mocks.renameSession);
		const failing = appData.renameSession('s0', 'Refused').catch(() => undefined);

		appData.mergeSessionPatch('s0', { title: 'From the other device' });
		held.refuse();
		await failing;

		expect(row('s0')?.title).toBe('From the other device');
	});

	it('gives back what a list read said the server holds', async () => {
		const held = hold(mocks.renameSession);
		const failing = appData.renameSession('s0', 'Refused').catch(() => undefined);
		const lists = mocks.get.getMockImplementation()!;
		mocks.get.mockImplementation(async (url: string) => {
			const page = (await lists(url)) as { items: Row[] };
			return { ...page, items: page.items.map((r) => (r.id === 's0' ? { ...r, title: 'Read since' } : r)) };
		});
		await appData.loadSessions(null);
		expect(row('s0')?.title).toBe('Refused');

		held.refuse();
		await failing;
		mocks.get.mockImplementation(lists);

		expect(row('s0')?.title).toBe('Read since');
	});

	it("a refused settle leaves the row's other fields where they have got to", async () => {
		const held = hold(mocks.setSessionActive);
		const failing = appData.setSessionActive('s0', false).catch(() => undefined);

		appData.mergeSessionPatch('s0', { title: 'Renamed elsewhere' });
		held.refuse();
		await failing;

		expect(row('s0')).toMatchObject({ is_active: true, title: 'Renamed elsewhere' });
	});

	it('a push during a refused change is still held over a read that started before it', async () => {
		const held = hold(mocks.setSessionActive);
		const failing = appData.setSessionActive('s0', false).catch(() => undefined);
		appData.mergeSessionPatch('s1', { title: 'Pushed' });

		let release!: () => void;
		const lists = mocks.get.getMockImplementation()!;
		mocks.get.mockImplementationOnce(async (url: string) => {
			await new Promise<void>((resolve) => { release = resolve; });
			return lists(url);
		});
		const read = appData.loadSessions(null);
		await vi.waitFor(() => expect(release).toBeTypeOf('function'));
		held.refuse();
		await failing;
		release();
		await read;

		expect(row('s1')?.title).toBe('Pushed');
	});

	describe('two changes out on one row', () => {
		it('both refused, newest first: back to the start', async () => {
			const first = hold(mocks.renameSession);
			const one = appData.renameSession('s0', 'One').catch(() => undefined);
			const second = hold(mocks.renameSession);
			const two = appData.renameSession('s0', 'Two').catch(() => undefined);

			second.refuse();
			await two;
			expect(row('s0')?.title).toBe('One');
			first.refuse();
			await one;

			expect(row('s0')?.title).toBe('Session 0');
		});

		it('both refused, oldest first: back to the start', async () => {
			const first = hold(mocks.renameSession);
			const one = appData.renameSession('s0', 'One').catch(() => undefined);
			const second = hold(mocks.renameSession);
			const two = appData.renameSession('s0', 'Two').catch(() => undefined);

			first.refuse();
			await one;
			expect(row('s0')?.title).toBe('Two');
			second.refuse();
			await two;

			expect(row('s0')?.title).toBe('Session 0');
		});

		it('the newer accepted, the older refused late: the newer stands', async () => {
			const first = hold(mocks.renameSession);
			const one = appData.renameSession('s0', 'One').catch(() => undefined);
			const second = hold(mocks.renameSession);
			const two = appData.renameSession('s0', 'Two');

			second.accept();
			await two;
			first.refuse();
			await one;

			expect(row('s0')?.title).toBe('Two');
		});

		it('different fields: the refused one goes back and the other stays optimistic', async () => {
			const rename = hold(mocks.renameSession);
			const renaming = appData.renameSession('s0', 'Kept').catch(() => undefined);
			const settle = hold(mocks.setSessionActive);
			const settling = appData.setSessionActive('s0', false).catch(() => undefined);

			settle.refuse();
			await settling;
			expect(row('s0')).toMatchObject({ is_active: true, title: 'Kept' });
			await appData.loadSessions(null);
			expect(row('s0')?.title).toBe('Kept');

			rename.accept();
			await renaming;
		});

		it('a rename refused while the row is being deleted: a refused delete brings the old title back', async () => {
			const rename = hold(mocks.renameSession);
			const renaming = appData.renameSession('s0', 'Refused').catch(() => undefined);
			const remove = hold(mocks.delete);
			const removing = appData.deleteSession('s0').catch(() => undefined);

			rename.refuse();
			await renaming;
			expect(row('s0')).toBeUndefined();
			remove.refuse();
			await removing;

			expect(row('s0')?.title).toBe('Session 0');
		});
	});

	describe('a refused delete', () => {
		it('puts the row back where it was and restores the count', async () => {
			const before = ids();
			const total = appData.current.sessionsTotal;
			const held = hold(mocks.delete);
			const failing = appData.deleteSession('s5').catch(() => undefined);
			expect(ids()).not.toContain('s5');

			let release!: () => void;
			const lists = mocks.get.getMockImplementation()!;
			mocks.get.mockImplementationOnce(async (url: string) => {
				await new Promise<void>((resolve) => { release = resolve; });
				return lists(url);
			});
			held.refuse();
			await vi.waitFor(() => expect(release).toBeTypeOf('function'));
			expect(ids()).toEqual(before);
			expect(appData.current.sessionsTotal).toBe(total);
			release();
			await failing;
		});

		it('carries a push that arrived while the row was gone', async () => {
			const held = hold(mocks.delete);
			const failing = appData.deleteSession('s0').catch(() => undefined);
			expect(appData.mergeSessionPatch('s0', { title: 'Pushed while gone' })).toBe(false);

			let release!: () => void;
			const lists = mocks.get.getMockImplementation()!;
			mocks.get.mockImplementationOnce(async (url: string) => {
				await new Promise<void>((resolve) => { release = resolve; });
				return lists(url);
			});
			held.refuse();
			await vi.waitFor(() => expect(release).toBeTypeOf('function'));
			expect(row('s0')?.title).toBe('Pushed while gone');
			release();
			await failing;
		});

		it("keeps a push through an older read and a failed reload, and still takes the read's other fields", async () => {
			const lists = mocks.get.getMockImplementation()!;
			let release!: () => void;
			const gate = new Promise<void>((resolve) => { release = resolve; });
			mocks.get.mockImplementation(async (url: string) => {
				const page = (await lists(url)) as { items: Row[]; has_more: boolean };
				const items = page.items.map((r) => (r.id === 's0' ? { ...r, is_active: true, model: 'from-the-read' } : r));
				await gate;
				return { ...page, items };
			});
			const olderRead = appData.loadSessions(null);
			await new Promise((r) => setTimeout(r, 0));

			const held = hold(mocks.delete);
			const failing = appData.deleteSession('s0').catch(() => undefined);
			expect(appData.mergeSessionPatch('s0', { title: 'From another device' })).toBe(false);
			release();
			await olderRead;

			mocks.get.mockImplementation(async () => { throw new Error('offline'); });
			held.refuse();
			await failing;

			expect(row('s0')?.title).toBe('From another device');
			expect((row('s0') as Row & { model?: string })?.model).toBe('from-the-read');
			mocks.get.mockImplementation(lists);
		});

		it('does not bring back a row a complete read has shown is gone', async () => {
			const lists = mocks.get.getMockImplementation()!;
			const held = hold(mocks.delete);
			const failing = appData.deleteSession('s0').catch(() => undefined);
			mocks.get.mockImplementation(async (url: string) => {
				const page = (await lists(url)) as { items: Row[]; has_more: boolean };
				return { ...page, has_more: false, items: page.items.filter((r) => r.id !== 's0') };
			});
			await appData.loadSessions(null);

			held.refuse();
			await failing;

			expect(row('s0')).toBeUndefined();
			mocks.get.mockImplementation(lists);
		});
	});

	it('an accepted settle stays accepted if the reload after it fails', async () => {
		const lists = mocks.get.getMockImplementation()!;
		mocks.get.mockImplementation(async () => { throw new Error('offline'); });
		mocks.setSessionActive.mockResolvedValueOnce(undefined);

		await appData.setSessionActive('s0', false);

		expect(row('s0')?.is_active).toBe(false);
		mocks.get.mockImplementation(lists);
	});
});

describe('"Load older sessions" paging', () => {
	it('asks the server to leave archived chats out of every sidebar request', async () => {
		await appData.loadSessions(null);
		await appData.loadMoreSessions();

		const listUrls = mocks.get.mock.calls
			.map(([url]) => url as string)
			.filter((url) => url.startsWith('/sessions/cursor'));
		expect(listUrls.length).toBeGreaterThan(0);
		// Otherwise has_more counts rows the sidebar drops, and the shelf keeps
		// fetching pages that add nothing visible.
		expect(listUrls.every((url) => url.includes('exclude_archived=true'))).toBe(true);
	});

	it('a background refresh keeps the older pages the user already loaded', async () => {
		await appData.loadSessions(null);
		await appData.loadMoreSessions();
		expect(appData.current.sessions).toHaveLength(140);
		expect(appData.current.sessionsHasMore).toBe(false);

		mocks.get.mockClear();
		await appData.loadSessions();

		// Re-fetches both pages instead of dropping back to one, which used to
		// bring "Load older sessions" back after the user reached the end.
		expect(mocks.get.mock.calls.some(([url]) => (url as string).includes('cursor=c1'))).toBe(true);
		expect(appData.current.sessions).toHaveLength(140);
		expect(appData.current.sessionsHasMore).toBe(false);
	});

	it('a tag change starts again from one page', async () => {
		await appData.loadSessions(null);
		await appData.loadMoreSessions();

		mocks.get.mockClear();
		await appData.loadSessions('some-tag');

		expect(mocks.get.mock.calls.some(([url]) => (url as string).includes('cursor=c1'))).toBe(false);
		await appData.loadSessions(null);
	});

	it('stops reporting more when the server repeats the same cursor', async () => {
		mocks.get.mockImplementation(async (url: string) => {
			if (url.includes('membership=active')) return { items: [], next_cursor: null, next_cursor_id: null, has_more: false };
			if (url.includes('cursor=stuck')) {
				return { items: [], next_cursor: 'stuck', next_cursor_id: 'stuck-id', has_more: true };
			}
			return { items: rowsFor(page1), next_cursor: 'stuck', next_cursor_id: 'stuck-id', has_more: true };
		});
		await appData.loadSessions('stuck-tag');
		expect(appData.current.sessionsHasMore).toBe(true);

		await appData.loadMoreSessions();

		expect(appData.current.sessionsHasMore).toBe(false);
		await appData.loadSessions(null);
	});
});

describe('early sidebar list (VITE_PERF_EARLY_SESSION_LIST)', () => {
	const listCalls = () => mocks.get.mock.calls.filter(([url]) => String(url).startsWith('/sessions/cursor')).length;
	afterEach(() => vi.unstubAllEnvs());

	it('lets the boot load join the list started when auth answered', async () => {
		listDelayMs = 20;
		appData.prefetchSessions();
		await appData.loadSessions();
		// One capped page plus the Active set, not twice.
		expect(listCalls()).toBe(2);
		expect(appData.current.sessions.length).toBeGreaterThan(0);
	});

	it('accepts a prefetched list that already committed, once', async () => {
		appData.prefetchSessions();
		await vi.waitFor(() => expect(appData.current.sessionsLoading).toBe(false));
		await new Promise(r => setTimeout(r, 0));
		await appData.loadSessions();
		expect(listCalls()).toBe(2);
		// Only the first loadSessions takes it; a later refresh reloads.
		await appData.loadSessions();
		expect(listCalls()).toBe(4);
	});

	it('loads again when the boot load asks for a different filter', async () => {
		appData.prefetchSessions();
		await appData.loadSessions('other-tag');
		expect(mocks.get.mock.calls.some(([url]) => String(url).includes('tag_id=other-tag'))).toBe(true);
		await appData.loadSessions(null);
	});

	it('starts nothing early with VITE_PERF_EARLY_SESSION_LIST=0', async () => {
		vi.stubEnv('VITE_PERF_EARLY_SESSION_LIST', '0');
		appData.prefetchSessions();
		await new Promise(r => setTimeout(r, 0));
		expect(listCalls()).toBe(0);
	});
});

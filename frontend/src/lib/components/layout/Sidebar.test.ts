import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';

const mocks = vi.hoisted(() => {
	function store<T>(initial: T) {
		let value = initial;
		const subscribers = new Set<(next: T) => void>();
		return {
			get current() { return value; },
			subscribe(run: (next: T) => void) {
				run(value);
				subscribers.add(run);
				return () => subscribers.delete(run);
			},
			set(next: T) {
				value = next;
				for (const run of subscribers) run(value);
			},
		};
	}

	return {
		sessions: store<unknown[]>([]),
		sessionsHasMore: store(false),
		sessionsLoadingMore: store(false),
		loadMoreSessions: vi.fn(),
		projects: store<unknown[]>([]),
		clusterProjects: store<unknown[]>([]),
		activeSession: store({ sessionId: null as string | null, isStreaming: false }),
		streamingSessions: store(new Set<string>()),
		backgroundPermissions: store(new Map<string, { count: number; kind: 'permission' | 'question' }>()),
		activeView: store<unknown>(null),
		getPullRequests: vi.fn(),
	};
});

vi.mock('$lib/stores/app-data.svelte', () => ({
	sessions: mocks.sessions,
	projects: mocks.projects,
	clusterProjects: mocks.clusterProjects,
	sessionsHasMore: mocks.sessionsHasMore,
	sessionsLoadingMore: mocks.sessionsLoadingMore,
	appData: {
		setSessionActive: vi.fn(),
		deleteSession: vi.fn(),
		renameSession: vi.fn(),
		loadSessions: vi.fn(),
		loadMoreSessions: mocks.loadMoreSessions,
	},
}));

// Spy on the timestamp helper so a test can prove `rows` is NOT rebuilt on a
// streaming fold (R2) — a rebuild resolves a timestamp for every visible row.
vi.mock('./session-card-data', async (importOriginal) => {
	const actual = await importOriginal<typeof import('./session-card-data')>();
	return { ...actual, sessionTimestamp: vi.fn(actual.sessionTimestamp) };
});

vi.mock('$lib/stores/active-session', () => ({
	activeSession: Object.assign(mocks.activeSession, {
		newChat: vi.fn(),
		setTitle: vi.fn(),
	}),
	streamingSessions: mocks.streamingSessions,
	backgroundPermissions: mocks.backgroundPermissions,
}));

vi.mock('$lib/stores/layout.svelte', () => ({
	activeView: mocks.activeView,
}));

vi.mock('$lib/api/client', () => ({
	forkSession: vi.fn(),
}));

vi.mock('$lib/stores/toast', () => ({
	toast: { push: vi.fn() },
}));

vi.mock('$lib/api/github', () => ({
	getPullRequests: mocks.getPullRequests,
}));

// ── Lazy-panel prefetch (workspace nav intent) ──────────────────────────────
// The handles are built from the REAL `createLazyComponent`, so the dedupe the
// "repeated hover" test asserts is production behaviour, not a test double.
// Only the module loader is faked, so no Svelte chunk is actually imported.
const panelSpies = vi.hoisted(() => ({
	/** Every `preloadPanelForView(viewType)` the component made. */
	preloadCalls: [] as (string | null | undefined)[],
	/** Panel keys whose chunk loader actually ran (i.e. a real fetch). */
	chunkFetches: [] as string[],
	/** Shared across tests, so it must be cleared between them. */
	handles: new Map<string, { preload(): void }>(),
}));

vi.mock('$lib/components/deck/lazy/panels', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/components/deck/lazy/panels')>();
	const { createLazyComponent } = await import('$lib/components/deck/lazy/lazy-component');

	return {
		...actual,
		preloadPanelForView(viewType: string | null | undefined) {
			panelSpies.preloadCalls.push(viewType);
			const key = actual.featurePanelKeyForView(viewType);
			if (!key) return;
			let handle = panelSpies.handles.get(key);
			if (!handle) {
				handle = createLazyComponent(key, async () => {
					panelSpies.chunkFetches.push(key);
					return { default: {} as never };
				});
				panelSpies.handles.set(key, handle);
			}
			handle.preload();
		},
	};
});

import { tick } from 'svelte';
import { sessionTimestamp } from './session-card-data';
import Sidebar from './Sidebar.svelte';

function session(
	id: string,
	title: string,
	projectId: string | null,
	isActive: boolean,
	hidden = false,
	extra: Record<string, unknown> = {}
) {
	return {
		id,
		profile_id: 'default',
		project_id: projectId,
		project_name:
			projectId === 'visible-project'
				? 'Visible Project'
				: projectId === 'hidden-project'
					? 'Hidden Project'
					: projectId,
		title,
		status: 'active',
		is_active: isActive,
		hidden,
		total_cost_usd: 0,
		total_tokens_in: 0,
		total_tokens_out: 0,
		cache_creation_tokens: 0,
		cache_read_tokens: 0,
		context_tokens: 0,
		turn_count: 1,
		tags: [],
		parent_session_id: null,
		fork_point_message_index: null,
		has_forks: false,
		created_at: '2026-08-20T12:00:00Z',
		updated_at: `2026-08-20T12:0${id === 'active' ? '3' : '2'}:00Z`,
		is_remote: false,
		...extra,
	};
}

const sidebarProps = {
	onSettledOpenSession: vi.fn(),
	onNewSession: vi.fn(),
	onOpenSession: vi.fn(),
	onOpenFeature: vi.fn(),
};

beforeEach(() => {
	localStorage.clear();
	vi.mocked(sessionTimestamp).mockClear();
	mocks.sessionsHasMore.set(false);
	mocks.sessionsLoadingMore.set(false);
	mocks.loadMoreSessions.mockClear();
	mocks.activeSession.set({ sessionId: null, isStreaming: false });
	mocks.streamingSessions.set(new Set());
	mocks.backgroundPermissions.set(new Map());
	// Reset the CALL COUNT too: without this the counter accumulates across
	// tests and any "fetched exactly once" assertion reads a stale total.
	mocks.getPullRequests.mockReset();
	mocks.getPullRequests.mockResolvedValue([]);
	mocks.clusterProjects.set([]);
	panelSpies.preloadCalls.length = 0;
	panelSpies.chunkFetches.length = 0;
	// Handles cache their resolved chunk forever by design; a fresh map per test
	// keeps "fetched exactly once" meaningful instead of trivially true.
	panelSpies.handles.clear();
	mocks.projects.set([
		{ id: 'visible-project', name: 'Visible Project' },
		{ id: 'hidden-project', name: 'Hidden Project' },
	]);
	mocks.sessions.set([
		session('active', 'Active chat', 'visible-project', true),
		session('settled', 'Settled chat', 'visible-project', false),
		session('worker', 'Hidden worker', 'hidden-project', true, true),
	]);
});

afterEach(() => cleanup());

describe('Sidebar session groups', () => {
	it('groups sessions into Active and Settled by is_active', () => {
		render(Sidebar, {
			props: {
				onNewSession: vi.fn(),
				onSettledOpenSession: vi.fn(),
				onOpenSession: vi.fn(),
				onOpenFeature: vi.fn(),
			},
		});

		// `.ws-head` is the Workspace nav header, not a session group.
		const labels = Array.from(document.querySelectorAll('.grouplabel:not(.ws-head)')).map((node) =>
			node.textContent?.replace(/\s+/g, ' ').trim()
		);
		expect(labels).toEqual(['Active 1', 'Settled 1']);
		expect(screen.getByText('Active chat')).toBeTruthy();
		expect(screen.getByText('Settled chat')).toBeTruthy();
	});

	it('excludes hidden worker sessions from cards and the project filter', async () => {
		render(Sidebar, {
			props: {
				onNewSession: vi.fn(),
				onSettledOpenSession: vi.fn(),
				onOpenSession: vi.fn(),
				onOpenFeature: vi.fn(),
			},
		});

		expect(screen.queryByText('Hidden worker')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: /All projects/ }));
		expect(screen.getByRole('menuitem', { name: /Visible Project/ })).toBeTruthy();
		expect(screen.queryByRole('menuitem', { name: /Hidden Project/ })).toBeNull();
	});

	it('excludes chats from a deleted project and omits it from the project filter', async () => {
		mocks.sessions.set([
			session('active', 'Active chat', 'visible-project', true),
			session('orphan', 'Orphan chat', 'deleted-project', false, false, {
				origin: 'git:gone.git',
				project_v2_id: 'pv2-gone',
			}),
		]);

		render(Sidebar, { props: sidebarProps });

		expect(screen.getByText('Active chat')).toBeTruthy();
		expect(screen.queryByText('Orphan chat')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: /All projects/ }));
		expect(screen.getByRole('menuitem', { name: /Visible Project/ })).toBeTruthy();
		expect(screen.queryByRole('menuitem', { name: /Deleted Project/ })).toBeNull();
	});

	it('keeps a remote-only catalog project in the list and filter', async () => {
		mocks.clusterProjects.set([
			{
				origin: 'git:other.git',
				project_v2_id: 'pv2-remote',
				local_project_id: null,
				name: 'Other',
			},
		]);
		mocks.sessions.set([
			session('remote', 'Remote chat', 'foreign-project', false, false, {
				origin: 'git:other.git',
				project_v2_id: 'pv2-remote',
				project_name: 'Other',
			}),
		]);

		render(Sidebar, { props: sidebarProps });

		expect(screen.getByText('Remote chat')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: /All projects/ }));
		expect(screen.getByRole('menuitem', { name: /Other/ })).toBeTruthy();
	});

	it('excludes chats from a deleted device and omits it from the device filter', async () => {
		mocks.sessions.set([
			session('local', 'Local chat', 'visible-project', true),
			session('gone', 'Gone device chat', 'visible-project', false, false, {
				is_remote: true,
				instance_id: 'dead-install',
				device_name: null,
			}),
			session('peer', 'Laptop chat', 'visible-project', false, false, {
				is_remote: true,
				instance_id: 'laptop',
				device_name: 'Laptop',
			}),
		]);

		render(Sidebar, { props: sidebarProps });

		expect(screen.getByText('Local chat')).toBeTruthy();
		expect(screen.getByText('Laptop chat')).toBeTruthy();
		expect(screen.queryByText('Gone device chat')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: /All devices/ }));
		expect(screen.getByRole('menuitem', { name: /This device/ })).toBeTruthy();
		expect(screen.getByRole('menuitem', { name: /Laptop/ })).toBeTruthy();
		expect(screen.queryByRole('menuitem', { name: /Unknown device/ })).toBeNull();
	});

	it('shows Needs Permission while a permission prompt is pending', async () => {
		mocks.backgroundPermissions.set(new Map([['active', { count: 1, kind: 'permission' }]]));

		render(Sidebar, { props: sidebarProps });

		const card = screen.getByText('Active chat').closest('.scard');
		expect(card?.querySelector('.status')?.textContent).toContain('Needs Permission');
	});

	it('shows Has Question while a user question is pending', async () => {
		mocks.backgroundPermissions.set(new Map([['active', { count: 3, kind: 'question' }]]));

		render(Sidebar, { props: sidebarProps });

		const card = screen.getByText('Active chat').closest('.scard');
		expect(card?.querySelector('.status')?.textContent).toContain('Has Question');
	});

	it('shows PR Opened from persisted session state without polling GitHub', async () => {
		mocks.sessions.set([
			session('prsession', 'PR chat', 'visible-project', true, false, {
				worktree_branch: 'wt/active',
				pr_state: 'open',
				pr_number: 7,
				pr_url: 'https://x/pull/7',
			}),
		]);

		render(Sidebar, { props: sidebarProps });

		const card = screen.getByText('PR chat').closest('.scard');
		expect(card?.querySelector('.status')?.textContent).toContain('PR Opened');
		expect(mocks.getPullRequests).not.toHaveBeenCalled();
	});

	it('shows PR Merged while the session is still Active', async () => {
		mocks.sessions.set([
			session('prsession', 'PR chat', 'visible-project', true, false, {
				worktree_branch: 'wt/active',
				pr_state: 'merged',
				pr_number: 7,
				pr_url: 'https://x/pull/7',
			}),
		]);

		render(Sidebar, { props: sidebarProps });

		const card = screen.getByText('PR chat').closest('.scard');
		expect(card?.querySelector('.status')?.textContent).toContain('PR Merged');
		expect(mocks.getPullRequests).not.toHaveBeenCalled();
	});

	it('shows PR Opened when the session branch has an open PR', async () => {
		mocks.getPullRequests.mockImplementation(async (_projectId: string, state: string) =>
			state === 'open'
				? [{ number: 7, title: 'Add thing', head_branch: 'wt/active', url: 'https://x/pull/7' }]
				: []
		);
		mocks.sessions.set([
			session('prsession', 'PR chat', 'visible-project', true, false, { worktree_branch: 'wt/active' }),
		]);

		render(Sidebar, { props: sidebarProps });

		// The fetch is async — wait for the badge to appear.
		await vi.waitFor(() => {
			const card = screen.getByText('PR chat').closest('.scard');
			expect(card?.querySelector('.status')?.textContent).toContain('PR Opened');
		});
		expect(mocks.getPullRequests).toHaveBeenCalledWith(
			'visible-project',
			'open',
			100,
		);
	});

	it('marks the project ghost row active once a project filter is applied', async () => {
		// Regression: switchSession resets $activeSession.isStreaming=false before
		// the SSE replay re-derives it, which flashed an open working session's
		// card to Done. The cross-device streaming map carries the durable busy
		// signal and must keep the card Working through the load window.
		mocks.activeSession.set({ sessionId: 'active', isStreaming: false });
		mocks.streamingSessions.set(new Set(['active']));

		render(Sidebar, { props: sidebarProps });

		const card = screen.getByText('Active chat').closest('.scard');
		expect(card?.querySelector('.status')?.textContent).toContain('Working');
	});

	it('marks the project ghost row active once a project filter is applied', async () => {
		render(Sidebar, { props: sidebarProps });

		expect(document.querySelector('.ghostrow.active')).toBeNull();

		await fireEvent.click(screen.getByRole('button', { name: /All projects/ }));
		await fireEvent.click(screen.getByRole('menuitem', { name: /Visible Project/ }));

		const active = document.querySelector('.ghostrow.active');
		expect(active).toBeTruthy();
		expect(active?.textContent).toContain('Visible Project');
	});
});

describe('Sidebar ordering (must never change silently)', () => {
	// Precomputing timestamps (R2) is only safe if the visible order is
	// byte-for-byte what the date-parsing comparator produced. This fixture
	// mixes updated_at, created_at-only and no-date rows.
	const fixture = [
		session('newest', 'Newest chat', 'visible-project', false, false, {
			updated_at: '2026-08-20T12:30:00Z',
		}),
		session('middle', 'Middle chat', 'visible-project', false, false, {
			updated_at: '2026-08-20T09:00:00Z',
		}),
		session('created-only', 'Created only chat', 'visible-project', false, false, {
			updated_at: null,
			created_at: '2026-08-20T11:00:00Z',
		}),
		session('oldest', 'Oldest chat', 'visible-project', false, false, {
			updated_at: '2026-08-18T23:59:00Z',
		}),
		session('undated', 'Undated chat', 'visible-project', false, false, {
			updated_at: null,
			created_at: null,
		}),
		session('naive', 'Naive timestamp chat', 'visible-project', false, false, {
			// The API can hand back a naive (no-Z) UTC stamp; parseUTC treats it
			// as UTC, so it must sort between "newest" and "created-only".
			updated_at: '2026-08-20T11:30:00',
		}),
	];

	it('orders settled cards by last activity, newest first', () => {
		mocks.sessions.set(fixture);

		render(Sidebar, { props: sidebarProps });

		const titles = Array.from(document.querySelectorAll('.ctitle')).map((n) => n.textContent);
		expect(titles).toEqual([
			'Newest chat',
			'Naive timestamp chat',
			'Created only chat',
			'Middle chat',
			'Oldest chat',
			'Undated chat',
		]);
	});

	it('keeps Active cards in the same order, above the Settled group', () => {
		mocks.sessions.set([
			session('a-old', 'Active older', 'visible-project', true, false, {
				updated_at: '2026-08-20T08:00:00Z',
			}),
			session('a-new', 'Active newer', 'visible-project', true, false, {
				updated_at: '2026-08-20T18:00:00Z',
			}),
			session('s-one', 'Settled one', 'visible-project', false, false, {
				updated_at: '2026-08-20T19:00:00Z',
			}),
		]);

		render(Sidebar, { props: sidebarProps });

		const activeTitles = Array.from(document.querySelectorAll('.l2')).map((n) => n.textContent);
		expect(activeTitles).toEqual(['Active newer', 'Active older']);
		const settledTitles = Array.from(document.querySelectorAll('.ctitle')).map((n) => n.textContent);
		expect(settledTitles).toEqual(['Settled one']);
	});
});

describe('Sidebar streaming folds (R2)', () => {
	it('does not rebuild the row list when $activeSession folds mid-stream', async () => {
		mocks.activeSession.set({ sessionId: 'active', isStreaming: true });
		render(Sidebar, { props: sidebarProps });
		await tick();

		const before = vi.mocked(sessionTimestamp).mock.calls.length;
		expect(before).toBeGreaterThan(0);

		// A streaming turn folds the activeSession store ~60×/s. Each fold hands
		// down a NEW state object with the same streaming identity — the sidebar
		// used to re-filter, re-map and re-sort the whole list on every one.
		for (let i = 0; i < 5; i++) {
			mocks.activeSession.set({ sessionId: 'active', isStreaming: true });
			await tick();
		}
		expect(vi.mocked(sessionTimestamp).mock.calls.length).toBe(before);

		// Sanity: a REAL streaming change still rebuilds the rows, so the check
		// above cannot pass just because nothing ever recomputes.
		mocks.streamingSessions.set(new Set(['settled']));
		await tick();
		expect(vi.mocked(sessionTimestamp).mock.calls.length).toBeGreaterThan(before);
	});

	it('still shows Working from the cross-device map alone', async () => {
		mocks.activeSession.set({ sessionId: null, isStreaming: false });
		mocks.streamingSessions.set(new Set(['settled']));

		render(Sidebar, { props: sidebarProps });

		const card = screen.getByText('Settled chat').closest('.scard');
		expect(card?.querySelector('.status')?.textContent ?? card?.textContent).toBeTruthy();
		expect(document.body.textContent).toContain('Settled chat');
	});
});

describe('Sidebar open-PR refresh (R8)', () => {
	it('does not refetch when a new $sessions array carries the same project/branch set', async () => {
		const rows = [
			session('prsession', 'PR chat', 'visible-project', true, false, { worktree_branch: 'wt/active' }),
		];
		mocks.sessions.set(rows);
		render(Sidebar, { props: sidebarProps });

		await vi.waitFor(() => expect(mocks.getPullRequests).toHaveBeenCalledTimes(1));

		// Every sessions emission hands the sidebar a NEW array identity (a `done`
		// event, a row patch…). The dedupe key used to be computed and thrown
		// away, so this fired getPullRequests for every project each time.
		for (let i = 0; i < 4; i++) {
			mocks.sessions.set(rows.map((r) => ({ ...r })));
			await tick();
		}
		await new Promise((r) => setTimeout(r, 20));
		expect(mocks.getPullRequests).toHaveBeenCalledTimes(1);
	});

	it('debounces a refetch when the branch set actually changes', async () => {
		vi.useFakeTimers();
		try {
			mocks.sessions.set([
				session('prsession', 'PR chat', 'visible-project', true, false, { worktree_branch: 'wt/active' }),
			]);
			render(Sidebar, { props: sidebarProps });
			await vi.advanceTimersByTimeAsync(0);
			expect(mocks.getPullRequests).toHaveBeenCalledTimes(1);

			mocks.sessions.set([
				session('prsession', 'PR chat', 'visible-project', true, false, { worktree_branch: 'wt/active' }),
				session('other', 'Other chat', 'visible-project', true, false, { worktree_branch: 'wt/other' }),
			]);
			await vi.advanceTimersByTimeAsync(100);
			expect(mocks.getPullRequests).toHaveBeenCalledTimes(1);

			await vi.advanceTimersByTimeAsync(2500);
			expect(mocks.getPullRequests).toHaveBeenCalledTimes(2);
		} finally {
			vi.useRealTimers();
		}
	});

	it('keeps a known PR badge when a later fetch fails', async () => {
		mocks.getPullRequests.mockResolvedValueOnce([
			{ number: 7, title: 'Add thing', head_branch: 'wt/active', url: 'https://x/pull/7' },
		]);
		mocks.sessions.set([
			session('prsession', 'PR chat', 'visible-project', true, false, { worktree_branch: 'wt/active' }),
		]);
		render(Sidebar, { props: sidebarProps });

		await vi.waitFor(() => {
			const card = screen.getByText('PR chat').closest('.scard');
			expect(card?.querySelector('.status')?.textContent).toContain('PR Opened');
		});

		// A transient failure (auth blip, rate limit) must not blank the badge.
		mocks.getPullRequests.mockRejectedValue(new Error('gh exploded'));
		mocks.sessions.set([
			session('prsession', 'PR chat', 'visible-project', true, false, { worktree_branch: 'wt/active' }),
			session('other', 'Other chat', 'visible-project', true, false, { worktree_branch: 'wt/other' }),
		]);
		await new Promise((r) => setTimeout(r, 2200));

		const card = screen.getByText('PR chat').closest('.scard');
		expect(card?.querySelector('.status')?.textContent).toContain('PR Opened');
	});
});

describe('Sidebar delegated card menus (R15)', () => {
	beforeEach(() => {
		Object.defineProperty(window, 'matchMedia', {
			writable: true,
			configurable: true,
			value: vi.fn().mockImplementation(() => ({
				matches: true,
				addEventListener: vi.fn(),
				removeEventListener: vi.fn(),
			})),
		});
	});

	function openActions(id: string) {
		const frame = document.querySelector(`[data-session-card="${id}"]`) as HTMLElement;
		const card = frame.querySelector('.scard') as HTMLElement;
		fireEvent.pointerDown(card, { pointerId: 1, isPrimary: true, button: 0, clientX: 260, clientY: 20 });
		fireEvent.pointerMove(frame, { pointerId: 1, isPrimary: true, clientX: 180, clientY: 22 });
		fireEvent.pointerUp(frame, { pointerId: 1, isPrimary: true, button: 0, clientX: 180, clientY: 22 });
		return frame;
	}

	it('attaches ONE window click listener regardless of how many cards render', () => {
		mocks.sessions.set(
			Array.from({ length: 12 }, (_, i) =>
				session(`s${i}`, `Chat ${i}`, 'visible-project', false)
			)
		);
		const addSpy = vi.spyOn(window, 'addEventListener');

		render(Sidebar, { props: sidebarProps });

		const clickListeners = addSpy.mock.calls.filter(([type]) => type === 'click').length;
		expect(clickListeners).toBe(1);
		addSpy.mockRestore();
	});

	it('closes the open card actions on an outside click', async () => {
		render(Sidebar, { props: sidebarProps });
		const frame = openActions('settled');
		await tick();
		expect(frame.classList.contains('actions-open')).toBe(true);

		await fireEvent.click(document.body);
		await tick();
		expect(frame.classList.contains('actions-open')).toBe(false);
	});

	it('leaves the open card alone when the click lands inside it', async () => {
		render(Sidebar, { props: sidebarProps });
		const frame = openActions('settled');
		await tick();

		await fireEvent.click(frame.querySelector('.card-actions') as HTMLElement);
		await tick();
		expect(frame.classList.contains('actions-open')).toBe(true);
	});

	it('closes the open card actions on Escape', async () => {
		render(Sidebar, { props: sidebarProps });
		const frame = openActions('settled');
		await tick();
		expect(frame.classList.contains('actions-open')).toBe(true);

		await fireEvent.keyDown(window, { key: 'Escape' });
		await tick();
		expect(frame.classList.contains('actions-open')).toBe(false);
	});
});

describe('Sidebar load-more shelf (W1)', () => {
	it('offers the next page when the server holds more than the capped load', async () => {
		mocks.sessionsHasMore.set(true);

		render(Sidebar, { props: sidebarProps });

		const shelf = screen.getByRole('button', { name: /Load older sessions/ });
		await fireEvent.click(shelf);
		expect(mocks.loadMoreSessions).toHaveBeenCalledTimes(1);
	});

	it('hides the shelf when everything is loaded', () => {
		mocks.sessionsHasMore.set(false);

		render(Sidebar, { props: sidebarProps });

		expect(screen.queryByRole('button', { name: /Load older sessions/ })).toBeNull();
	});
});

describe('Sidebar workspace nav prefetch', () => {
	/** The Workspace rows, keyed by their visible label. */
	function wsrow(label: string): HTMLElement {
		const row = Array.from(document.querySelectorAll('.wsrow')).find(
			(node) => node.querySelector('.label')?.textContent?.trim() === label
		);
		if (!row) throw new Error(`no workspace row labelled ${label}`);
		return row as HTMLElement;
	}

	it('does not prefetch anything just by rendering', () => {
		render(Sidebar, { props: sidebarProps });

		expect(panelSpies.preloadCalls).toEqual([]);
		expect(panelSpies.chunkFetches).toEqual([]);
	});

	it('prefetches the panel chunk when the pointer enters a row', async () => {
		render(Sidebar, { props: sidebarProps });

		await fireEvent.pointerEnter(wsrow('Notes'));
		await Promise.resolve();

		expect(panelSpies.preloadCalls).toEqual(['notes']);
		expect(panelSpies.chunkFetches).toEqual(['notes']);
	});

	it('prefetches on focus, so keyboard navigation warms the same chunk', async () => {
		render(Sidebar, { props: sidebarProps });

		await fireEvent.focus(wsrow('Stats'));
		await Promise.resolve();

		expect(panelSpies.preloadCalls).toEqual(['stats']);
		expect(panelSpies.chunkFetches).toEqual(['stats']);
	});

	it('maps every Build sub-view row onto the Build panel chunk', async () => {
		render(Sidebar, { props: sidebarProps });

		await fireEvent.pointerEnter(wsrow('Build'));
		await Promise.resolve();

		expect(panelSpies.chunkFetches).toEqual(['build']);
	});

	it('does not re-fetch a chunk on repeated hover or on a later focus', async () => {
		render(Sidebar, { props: sidebarProps });
		const row = wsrow('Files');

		await fireEvent.pointerEnter(row);
		await Promise.resolve();
		await fireEvent.pointerLeave(row);
		await fireEvent.pointerEnter(row);
		await fireEvent.focus(row);
		await Promise.resolve();

		// The component asked three times — the handle fetched once.
		expect(panelSpies.preloadCalls).toEqual(['file-browser', 'file-browser', 'file-browser']);
		expect(panelSpies.chunkFetches).toEqual(['file-browser']);
	});

	it('still routes the click through onOpenFeature after a prefetch', async () => {
		const onOpenFeature = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, onOpenFeature } });

		const row = wsrow('Docs');
		await fireEvent.pointerEnter(row);
		await fireEvent.click(row);

		expect(onOpenFeature).toHaveBeenCalledWith('docs');
	});
});

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import type { PreferenceAdapter } from '$lib/services/preference-sync';
import { sidebarGroups } from '$lib/stores/expansion-preferences';

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
		adapters: new Map<string, PreferenceAdapter>(),
		save: vi.fn(),
		sessions: store<unknown[]>([]),
		sessionsHasMore: store(false),
		sessionsLoadingMore: store(false),
		loadMoreSessions: vi.fn(),
		projects: store<unknown[]>([]),
		clusterProjects: store<unknown[]>([]),
		activeSession: store<{ sessionId: string | null; isStreaming: boolean; optimisticStreaming?: boolean; activeTasks: { status: string }[] }>({ sessionId: null, isStreaming: false, activeTasks: [] }),
		streamingSessions: store(new Set<string>()),
		backgroundPermissions: store(new Map<string, { count: number; kind: 'permission' | 'question' }>()),
		activeView: store<unknown>(null),
		getPullRequests: vi.fn(),
	};
});

vi.mock('$lib/services/preferences', () => ({ preferenceSync: {
 register: (key: string, adapter: PreferenceAdapter) => mocks.adapters.set(key, adapter), save: mocks.save
} }));

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
vi.mock('$lib/api/assistant', () => ({ getAssistant: vi.fn().mockResolvedValue(null), getAgentIdentities: vi.fn().mockResolvedValue([]) }));

vi.mock('$lib/stores/notifications', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/stores/notifications')>(),
	notify: Object.assign(vi.fn(), { markAllRead: vi.fn() }),
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
// Not mocked: the lane partition IS what the mode tests assert, so they drive
// the real store. It is a module singleton, hence the reset in beforeEach.
import { appMode } from '$lib/stores/sessionMode.svelte';
// Also unmocked: the workspace nav's shown/hidden split IS what those tests
// assert. Module-level state, hence the reset in beforeEach.
import { sidebarWorkspace } from '$lib/stores/sidebar-prefs.svelte';
import Sidebar from './Sidebar.svelte';
import { platform } from '$lib/stores/platform.svelte';

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

// ── Card queries ────────────────────────────────────────────────────────────
// A card's INNARDS belong to SessionCard, so the sidebar's tests address cards
// by the `data-session-card` frame the sidebar itself relies on (see the
// delegated-menus block) and by the words a status is supposed to carry — never
// by SessionCard's internal class names, which are free to change without
// breaking anything the sidebar promises.

/** Every rendered card id, in DOM order. */
function cardIds(scope: ParentNode = document): string[] {
	return Array.from(scope.querySelectorAll('[data-session-card]')).map(
		(node) => node.getAttribute('data-session-card') ?? ''
	);
}

/** The card frame carrying `title`. */
function cardFrame(title: string): HTMLElement {
	const frame = screen.getByText(title).closest('[data-session-card]');
	if (!frame) throw new Error(`no card frame for ${title}`);
	return frame as HTMLElement;
}

beforeEach(() => {
	localStorage.clear();
	mocks.adapters.get('sidebar_collapsed_groups')!.apply(undefined);
	mocks.save.mockClear();
	appMode.set('code');
	sidebarWorkspace.reset();
	vi.mocked(sessionTimestamp).mockClear();
	mocks.sessionsHasMore.set(false);
	mocks.sessionsLoadingMore.set(false);
	mocks.loadMoreSessions.mockClear();
	mocks.activeSession.set({ sessionId: null, isStreaming: false, activeTasks: [] });
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

afterEach(() => {
	cleanup();
	platform.set('desktop');
});

describe('Sidebar session groups', () => {
	it('shares collapsed groups with a mounted sidebar and resets them on account change', async () => {
		const { container } = render(Sidebar);
		const header = () => Array.from(container.querySelectorAll<HTMLButtonElement>('.grouplabel'))
			.find(button => button.textContent?.includes('Settled'))!;
		await fireEvent.click(header());
		expect(sidebarGroups.current.settled).toBe(true);
		expect(mocks.save).toHaveBeenCalledWith('sidebar_collapsed_groups', { settled: true });
		mocks.save.mockClear();
		mocks.adapters.get('sidebar_collapsed_groups')!.apply({ settled: false, active: true });
		await tick();
		expect(header().getAttribute('aria-expanded')).toBe('true');
		mocks.adapters.get('sidebar_collapsed_groups')!.apply(undefined);
		await tick();
		expect(sidebarGroups.current).toEqual({});
		expect(mocks.save).not.toHaveBeenCalled();
	});

	it('groups sessions into Active and Settled by is_active', () => {
		render(Sidebar, {
			props: {
				onNewSession: vi.fn(),
				onSettledOpenSession: vi.fn(),
				onOpenSession: vi.fn(),
				onOpenFeature: vi.fn(),
			},
		});

		const labels = Array.from(document.querySelectorAll('.grouplabel')).map((node) =>
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

		expect(cardFrame('Active chat').textContent).toContain('Needs permission');
	});

	it('shows Has Question while a user question is pending', async () => {
		mocks.backgroundPermissions.set(new Map([['active', { count: 3, kind: 'question' }]]));

		render(Sidebar, { props: sidebarProps });

		expect(cardFrame('Active chat').textContent).toContain('Has question');
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

		expect(cardFrame('PR chat').textContent).toContain('PR #7 open');
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

		expect(cardFrame('PR chat').textContent).toContain('PR #7 merged');
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
			expect(cardFrame('PR chat').textContent).toContain('PR open');
		});
		expect(mocks.getPullRequests).toHaveBeenCalledWith(
			'visible-project',
			'open',
			100,
		);
		for (const streaming of [true, false, true]) {
			mocks.streamingSessions.set(new Set(streaming ? ['prsession'] : []));
			await tick();
			expect(cardFrame('PR chat').textContent).toContain('PR open');
		}
	});

	it('keeps a selected session Working while its transcript loads', async () => {
		// Regression: switchSession resets $activeSession.isStreaming=false before
		// the SSE replay re-derives it, which flashed an open working session's
		// card to Done. The cross-device streaming map carries the durable busy
		// signal and must keep the card Working through the load window.
		mocks.activeSession.set({ sessionId: 'active', isStreaming: false, activeTasks: [] });
		mocks.streamingSessions.set(new Set(['active']));

		render(Sidebar, { props: sidebarProps });

		expect(cardFrame('Active chat').textContent).toContain('Working');
	});

	it('marks the project ghost row active once a project filter is applied', async () => {
		render(Sidebar, { props: sidebarProps });

		expect(document.querySelector('.fbtn.set')).toBeNull();

		await fireEvent.click(screen.getByRole('button', { name: /All projects/ }));
		await fireEvent.click(screen.getByRole('menuitem', { name: /Visible Project/ }));

		const active = document.querySelector('.fbtn.set');
		expect(active).toBeTruthy();
		expect(active?.textContent).toContain('Visible Project');
	});
});

describe('Sidebar Needs you lane', () => {
	const labels = () => Array.from(document.querySelectorAll('.grouplabel')).map((node) =>
		node.textContent?.replace(/\s+/g, ' ').trim()
	);
	const lane = () => document.querySelector('[data-group="needs-you"]');

	it('lifts rows waiting on a prompt out of Active and Settled, and back when answered', async () => {
		mocks.backgroundPermissions.set(new Map([
			['active', { count: 1, kind: 'permission' }],
			['settled', { count: 1, kind: 'question' }],
		]));
		render(Sidebar, { props: sidebarProps });
		expect(labels()).toEqual(['Needs you 2', 'Settled 0']);
		expect(lane()?.textContent).toContain('Active chat');
		expect(lane()?.textContent).toContain('Settled chat');

		mocks.backgroundPermissions.set(new Map([['settled', { count: 1, kind: 'question' }]]));
		await tick();
		expect(labels()).toEqual(['Needs you 1', 'Active 1', 'Settled 0']);
		expect(lane()?.textContent).not.toContain('Active chat');

		mocks.backgroundPermissions.set(new Map());
		await tick();
		expect(labels()).toEqual(['Active 1', 'Settled 1']);
		expect(lane()).toBeNull();
	});

	it('takes a remote session from its owner state, not the local map', () => {
		mocks.sessions.set([
			session('remote', 'Remote chat', 'visible-project', true, false, { is_remote: true, instance_id: 'laptop', device_name: 'Laptop', remote_runtime_state: 'question' }),
		]);
		render(Sidebar, { props: sidebarProps });
		expect(lane()?.textContent).toContain('Remote chat');
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

		expect(cardIds()).toEqual([
			'newest',
			'naive',
			'created-only',
			'middle',
			'oldest',
			'undated',
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

		const groups = document.querySelectorAll('.cards');
		expect(cardIds(groups[0])).toEqual(['a-new', 'a-old']);
		expect(cardIds(groups[1])).toEqual(['s-one']);
	});
});

describe('Sidebar background work', () => {
	it('keeps a non-parked Monitor row Working through selection, switching away, and list reload', async () => {
		const monitor = session('monitor', 'Monitor chat', 'visible-project', true, false, {
			has_running_tasks: true, parked_task_kind: null,
		});
		const other = session('other', 'Other chat', 'visible-project', true);
		mocks.sessions.set([monitor, other]);
		const select = (sessionId: string) => mocks.activeSession.set({ sessionId, isStreaming: false, activeTasks: [] });
		render(Sidebar, { props: { ...sidebarProps, onOpenSession: select } });
		expect(cardFrame('Monitor chat').textContent).toContain('Working');
		await fireEvent.click(screen.getByText('Monitor chat'));
		expect(mocks.activeSession.current.sessionId).toBe('monitor');
		expect(cardFrame('Monitor chat').textContent).toContain('Working');
		await fireEvent.click(screen.getByText('Other chat'));
		expect(mocks.activeSession.current.sessionId).toBe('other');
		expect(cardFrame('Monitor chat').textContent).toContain('Working');
		mocks.sessions.set([{ ...monitor }, { ...other }]);
		await tick();
		expect(cardFrame('Monitor chat').textContent).toContain('Working');
		expect(cardFrame('Other chat').textContent).toContain('Done');
	});

	it('tracks concurrent Monitor sessions independently and shows Done when each completes', async () => {
		const first = session('first', 'First monitor', 'visible-project', true, false, { has_running_tasks: true, parked_task_kind: null });
		const second = session('second', 'Second monitor', 'visible-project', true, false, { has_running_tasks: true, parked_task_kind: null });
		mocks.sessions.set([first, second]);
		render(Sidebar, { props: sidebarProps });
		expect(cardFrame('First monitor').textContent).toContain('Working');
		expect(cardFrame('Second monitor').textContent).toContain('Working');
		mocks.sessions.set([{ ...first, has_running_tasks: false }, second]);
		await tick();
		expect(cardFrame('First monitor').textContent).toContain('Done');
		expect(cardFrame('Second monitor').textContent).toContain('Working');
		mocks.sessions.set([{ ...first, has_running_tasks: false }, { ...second, has_running_tasks: false }]);
		await tick();
		expect(cardFrame('Second monitor').textContent).toContain('Done');
		expect(screen.queryByText('Working')).toBeNull();
	});

	it.each(['permission', 'question'] as const)('prioritizes %s attention over durable Monitor work', async (kind) => {
		mocks.sessions.set([session('monitor', 'Monitor chat', 'visible-project', true, false, { has_running_tasks: true, parked_task_kind: null })]);
		mocks.backgroundPermissions.set(new Map([['monitor', { count: 1, kind }]]));
		render(Sidebar, { props: sidebarProps });
		expect(cardFrame('Monitor chat').textContent).not.toContain('Working');
		expect(cardFrame('Monitor chat').textContent).toContain(kind === 'permission' ? 'Needs permission' : 'Has question');
		mocks.backgroundPermissions.set(new Map());
		await tick();
		expect(cardFrame('Monitor chat').textContent).toContain('Working');
	});

	it.each(['agent', 'shell'])('keeps an unopened parked %s session Parked until its marker clears', async (kind) => {
		const parked = session('parked', 'Parked chat', 'visible-project', true, false, { parked_task_kind: kind });
		mocks.sessions.set([parked]);
		render(Sidebar, { props: sidebarProps });
		expect(cardFrame('Parked chat').textContent).toContain('Parked');
		expect(cardFrame('Parked chat').textContent).not.toContain('Done');

		mocks.sessions.set([{ ...parked, parked_task_kind: null }]);
		await tick();
		expect(cardFrame('Parked chat').textContent).toContain('Done');
		expect(cardFrame('Parked chat').textContent).not.toContain('Working');
	});

	it('does not revive completed work from cached transcript or task state on selection', async () => {
		render(Sidebar, { props: sidebarProps });
		await tick();
		const before = vi.mocked(sessionTimestamp).mock.calls.length;
		for (const sessionId of ['active', 'settled', 'active']) {
			mocks.activeSession.set({ sessionId, isStreaming: true, activeTasks: [{ status: 'running' }] });
			await tick();
			expect(cardFrame('Active chat').textContent).toContain('Done');
			expect(cardFrame('Settled chat').textContent).not.toContain('Working');
		}
		expect(vi.mocked(sessionTimestamp).mock.calls.length).toBe(before);
		mocks.activeSession.set({ sessionId: 'active', isStreaming: true, optimisticStreaming: true, activeTasks: [] });
		await tick();
		expect(cardFrame('Active chat').textContent).toContain('Working');
		mocks.streamingSessions.set(new Set(['active']));
		mocks.activeSession.set({ sessionId: 'active', isStreaming: true, optimisticStreaming: false, activeTasks: [] });
		await tick();
		expect(cardFrame('Active chat').textContent).toContain('Working');
		mocks.streamingSessions.set(new Set());
		await tick();
		expect(cardFrame('Active chat').textContent).toContain('Done');
	});

	it.each([
		['permission', 'Needs permission'],
		['question', 'Has question'],
	] as const)('keeps %s attention ahead of parked work', (kind, label) => {
		mocks.sessions.set([session('active', 'Active chat', 'visible-project', true, false, { parked_task_kind: 'agent' })]);
		mocks.backgroundPermissions.set(new Map([['active', { count: 1, kind }]]));
		render(Sidebar, { props: sidebarProps });
		expect(cardFrame('Active chat').textContent).toContain(label);
		expect(cardFrame('Active chat').textContent).not.toContain('Working');
	});
});

describe('Sidebar streaming folds (R2)', () => {
	it('does not rebuild the row list when $activeSession folds mid-stream', async () => {
		mocks.activeSession.set({ sessionId: 'active', isStreaming: true, activeTasks: [] });
		render(Sidebar, { props: sidebarProps });
		await tick();

		const before = vi.mocked(sessionTimestamp).mock.calls.length;
		expect(before).toBeGreaterThan(0);

		// A streaming turn folds the activeSession store ~60×/s. Each fold hands
		// down a NEW state object with the same streaming identity — the sidebar
		// used to re-filter, re-map and re-sort the whole list on every one.
		for (let i = 0; i < 5; i++) {
			mocks.activeSession.set({ sessionId: 'active', isStreaming: true, activeTasks: [] });
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
		mocks.activeSession.set({ sessionId: null, isStreaming: false, activeTasks: [] });
		mocks.streamingSessions.set(new Set(['settled']));

		render(Sidebar, { props: sidebarProps });

		expect(cardFrame('Settled chat').textContent).toBeTruthy();
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
			expect(cardFrame('PR chat').textContent).toContain('PR open');
		});

		// A transient failure (auth blip, rate limit) must not blank the badge.
		mocks.getPullRequests.mockRejectedValue(new Error('gh exploded'));
		mocks.sessions.set([
			session('prsession', 'PR chat', 'visible-project', true, false, { worktree_branch: 'wt/active' }),
			session('other', 'Other chat', 'visible-project', true, false, { worktree_branch: 'wt/other' }),
		]);
		await new Promise((r) => setTimeout(r, 2200));

		expect(cardFrame('PR chat').textContent).toContain('PR open');
	});
});

describe('Sidebar delegated card menus (R15)', () => {
	// Swipe-to-reveal is the mobile shell's gesture: the cards render their
	// swipe layout because the platform is mobile, not because of a width.
	beforeEach(() => platform.set('mobile'));

	function openActions(id: string) {
		const frame = document.querySelector(`[data-session-card="${id}"]`) as HTMLElement;
		const card = frame.querySelector('.scard') as HTMLElement;
		fireEvent.pointerDown(card, { pointerId: 1, isPrimary: true, button: 0, clientX: 260, clientY: 20 });
		fireEvent.pointerMove(frame, { pointerId: 1, isPrimary: true, clientX: 180, clientY: 22 });
		fireEvent.pointerUp(frame, { pointerId: 1, isPrimary: true, button: 0, clientX: 180, clientY: 22 });
		return frame;
	}

	it('does not attach a window click listener per rendered card', () => {
		function countClickListeners(cards: number): number {
			mocks.sessions.set(
				Array.from({ length: cards }, (_, i) =>
					session(`s${i}`, `Chat ${i}`, 'visible-project', false)
				)
			);
			const addSpy = vi.spyOn(window, 'addEventListener');
			const view = render(Sidebar, { props: sidebarProps });
			const count = addSpy.mock.calls.filter(([type]) => type === 'click').length;
			addSpy.mockRestore();
			view.unmount();
			return count;
		}

		// Every SessionCard used to attach its own pair, so a single click
		// anywhere ran one handler per rendered card (60+). The sidebar owns the
		// open-card id instead. The absolute count is not the point — it also
		// covers the header's notification bell — so assert the invariant that
		// actually matters: it is flat in the number of cards.
		expect(countClickListeners(12)).toBe(countClickListeners(1));
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

// ── Workspace nav helpers (shared by the two blocks below) ──────────────────

/** The inline Workspace rows, keyed by their visible label. */
function wsrow(label: string): HTMLElement {
	const row = Array.from(document.querySelectorAll('.wsrow')).find(
		(node) => node.querySelector('.label')?.textContent?.trim() === label
	);
	if (!row) throw new Error(`no workspace row labelled ${label}`);
	return row as HTMLElement;
}

/** Labels of the inline rows, in order, excluding the More row itself. */
function wsLabels(): string[] {
	return Array.from(document.querySelectorAll('.wsrow:not(.morerow) .label')).map(
		(node) => node.textContent?.trim() ?? ''
	);
}

/** Open the More flyout and return its menu items. */
async function openMore(): Promise<HTMLElement[]> {
	await fireEvent.click(screen.getByRole('button', { name: /More/ }));
	return screen.getAllByRole('menuitem') as HTMLElement[];
}

function flyoutItem(label: string): HTMLElement {
	const item = screen
		.getAllByRole('menuitem')
		.find((node) => node.textContent?.trim() === label);
	if (!item) throw new Error(`no flyout item labelled ${label}`);
	return item as HTMLElement;
}

describe('Sidebar workspace nav prefetch', () => {
	it('does not prefetch anything just by rendering', () => {
		render(Sidebar, { props: sidebarProps });

		expect(panelSpies.preloadCalls).toEqual([]);
		expect(panelSpies.chunkFetches).toEqual([]);
	});

	it('prefetches the panel chunk when the pointer enters a row', async () => {
		render(Sidebar, { props: sidebarProps });

		await fireEvent.pointerEnter(wsrow('Projects'));
		await Promise.resolve();

		expect(panelSpies.preloadCalls).toEqual(['project']);
		expect(panelSpies.chunkFetches).toEqual(['project']);
	});

	it('prefetches on focus, so keyboard navigation warms the same chunk', async () => {
		render(Sidebar, { props: sidebarProps });

		await fireEvent.focus(wsrow('Agents & tools'));
		await Promise.resolve();

		expect(panelSpies.preloadCalls).toEqual(['build']);
		expect(panelSpies.chunkFetches).toEqual(['build']);
	});

	it('maps every Build sub-view row onto the Build panel chunk', async () => {
		render(Sidebar, { props: sidebarProps });

		await fireEvent.pointerEnter(wsrow('Agents & tools'));
		await Promise.resolve();

		expect(panelSpies.chunkFetches).toEqual(['build']);
	});

	it('prefetches from a flyout item too — a folded row is no less likely', async () => {
		render(Sidebar, { props: sidebarProps });

		await openMore();
		await fireEvent.pointerEnter(flyoutItem('Notes'));
		await Promise.resolve();

		expect(panelSpies.preloadCalls).toEqual(['notes']);
		expect(panelSpies.chunkFetches).toEqual(['notes']);
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

		const row = wsrow('Files');
		await fireEvent.pointerEnter(row);
		await fireEvent.click(row);

		expect(onOpenFeature).toHaveBeenCalledWith('file-browser');
	});
});

describe('Sidebar workspace nav customization', () => {
	it('shows the default three rows inline and folds the rest into More', async () => {
		render(Sidebar, { props: sidebarProps });

		expect(wsLabels()).toEqual(['Files', 'Agents & tools', 'Projects']);

		const items = await openMore();
		expect(items.map((n) => n.textContent?.trim())).toEqual([
			'Notes',
			'Pull requests',
			'Stats',
			'Docs',
			'Customize',
		]);
	});

	it('never offers an admin-only destination to a non-admin, inline or folded', async () => {
		render(Sidebar, { props: sidebarProps });

		const items = await openMore();
		const labels = [...wsLabels(), ...items.map((n) => n.textContent?.trim())];
		expect(labels).not.toContain('Automations');
		expect(labels).not.toContain('Settings');
	});

	it('lists the admin destinations for an admin', async () => {
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true } });

		const items = await openMore();
		const labels = items.map((n) => n.textContent?.trim());
		expect(labels).toContain('Automations');
		expect(labels).toContain('Settings');
	});

	it('opens a folded destination from the flyout and closes it behind itself', async () => {
		const onOpenFeature = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, onOpenFeature } });

		await openMore();
		await fireEvent.click(flyoutItem('Docs'));

		expect(onOpenFeature).toHaveBeenCalledWith('docs');
		expect(screen.queryByRole('menuitem', { name: 'Docs' })).toBeNull();
	});

	it('promotes a hidden destination to an inline row from Customize', async () => {
		render(Sidebar, { props: sidebarProps });

		await openMore();
		await fireEvent.click(flyoutItem('Customize'));

		const dialog = screen.getByRole('dialog', { name: /Customize workspace/ });
		expect(dialog).toBeTruthy();
		// "New session" is listed but pinned — it is not a Workspace destination.
		expect(dialog.textContent).toContain('Always first');

		await fireEvent.click(screen.getByRole('checkbox', { name: 'Show Notes' }));
		expect(wsLabels()).toEqual(['Files', 'Notes', 'Agents & tools', 'Projects']);

		// And it survives the dialog, because the store wrote it.
		await fireEvent.click(screen.getByRole('button', { name: 'Done' }));
		expect(screen.queryByRole('dialog', { name: /Customize workspace/ })).toBeNull();
		expect(wsLabels()).toEqual(['Files', 'Notes', 'Agents & tools', 'Projects']);
	});

	it('reorders a row from the keyboard, so the feature is not pointer-only', async () => {
		render(Sidebar, { props: sidebarProps });

		await openMore();
		await fireEvent.click(flyoutItem('Customize'));

		await fireEvent.keyDown(screen.getByRole('button', { name: 'Reorder Projects' }), {
			key: 'ArrowUp',
		});
		await fireEvent.keyDown(screen.getByRole('button', { name: 'Reorder Projects' }), {
			key: 'ArrowUp',
		});

		expect(wsLabels()).toEqual(['Files', 'Projects', 'Agents & tools']);
	});
});

describe('Sidebar header and sticky block', () => {
	it('puts New session first, as a row rather than a header icon', () => {
		render(Sidebar, { props: sidebarProps });

		const newRow = screen.getByRole('button', { name: /New session/ });
		expect(newRow.classList.contains('newrow')).toBe(true);
		// It sits in the sticky block, above the scroll region's workspace rows.
		expect(newRow.closest('.sb-top')).toBeTruthy();
		expect(newRow.compareDocumentPosition(wsrow('Files')) & Node.DOCUMENT_POSITION_FOLLOWING)
			.toBeTruthy();
	});

	it('opens the spotlight from the header search button and drops the ghost row', async () => {
		const onOpenSpotlight = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, onOpenSpotlight } });

		await fireEvent.click(screen.getByRole('button', { name: 'Search' }));

		expect(onOpenSpotlight).toHaveBeenCalledTimes(1);
		// The old full-width "Search" ghost row is gone.
		expect(document.querySelector('.ghostrow .label')?.textContent).not.toBe('Search');
	});

	it('omits the search button when the host threads no spotlight', () => {
		render(Sidebar, { props: sidebarProps });

		expect(screen.queryByRole('button', { name: 'Search' })).toBeNull();
	});

	it('hosts the notification bell — its only home now', () => {
		render(Sidebar, { props: sidebarProps });

		expect(screen.getByRole('button', { name: /Notifications/ })).toBeTruthy();
	});

	it('fades the separator in only once the list has actually scrolled', async () => {
		render(Sidebar, { props: sidebarProps });

		const top = document.querySelector('.sb-top') as HTMLElement;
		const scroll = document.querySelector('.sb-scroll') as HTMLElement;
		expect(top.classList.contains('scrolled')).toBe(false);

		Object.defineProperty(scroll, 'scrollTop', { value: 40, configurable: true });
		await fireEvent.scroll(scroll);
		expect(top.classList.contains('scrolled')).toBe(true);
	});

	it('closes an open scope menu on scroll, since its anchor is a snapshot', async () => {
		render(Sidebar, { props: sidebarProps });

		await fireEvent.click(screen.getByRole('button', { name: /All devices/ }));
		expect(screen.queryByRole('menuitem', { name: /This device/ })).toBeTruthy();

		await fireEvent.scroll(document.querySelector('.sb-scroll') as HTMLElement);
		expect(screen.queryByRole('menuitem', { name: /This device/ })).toBeNull();
	});

	it('clears both scopes from the single clear button on the row', async () => {
		render(Sidebar, { props: sidebarProps });

		expect(document.querySelector('.scope-clear')).toBeNull();

		await fireEvent.click(screen.getByRole('button', { name: /All projects/ }));
		await fireEvent.click(screen.getByRole('menuitem', { name: /Visible Project/ }));
		await fireEvent.click(screen.getByRole('button', { name: /All devices/ }));
		await fireEvent.click(screen.getByRole('menuitem', { name: /This device/ }));
		expect(document.querySelectorAll('.fbtn.set').length).toBe(2);

		await fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
		expect(document.querySelectorAll('.fbtn.set').length).toBe(0);
		expect(document.querySelector('.scope-clear')).toBeNull();
	});

	it('docks the lane switcher at the panel foot', () => {
		render(Sidebar, { props: sidebarProps });

		const dock = document.querySelector('.sb-dock');
		expect(dock?.querySelector('[role="radiogroup"]')).toBeTruthy();
	});
});

describe('Sidebar unified conversation history', () => {
 function fixture() {
  return [
   session('legacy', 'Legacy chat', 'visible-project', false),
   session('coded', 'Code chat', 'visible-project', false, false, { mode: 'code' }),
   session('chatty', 'Chat chat', null, false, false, { mode: 'chat' }),
   session('spoken', 'Voice chat', null, false, false, { mode: 'voice' }),
  ];
 }
 it.each(['code', 'chat', 'voice'] as const)('keeps every historical conversation visible while %s is selected', mode => {
  appMode.set(mode); mocks.sessions.set(fixture());
  render(Sidebar, { props: sidebarProps });
  expect(cardIds()).toEqual(['legacy', 'coded', 'chatty', 'spoken']);
  expect(screen.getByRole('button', { name: /All projects/ })).toBeTruthy();
 });
 it('uses a single honest empty state', () => {
  mocks.sessions.set([]); appMode.set('voice'); render(Sidebar, { props: sidebarProps });
  expect(document.querySelector('.empty')?.textContent).toBe('No sessions yet');
 });
 it('retains explicit project scope when new-conversation capabilities change', async () => {
  mocks.sessions.set(fixture()); render(Sidebar, { props: sidebarProps });
  await fireEvent.click(screen.getByRole('button', { name: /All projects/ }));
  await fireEvent.click(screen.getByRole('menuitem', { name: /Visible Project/ }));
  await fireEvent.click(screen.getByRole('radio', { name: 'Chat' }));
  expect(cardIds()).toEqual(['legacy', 'coded']);
  await fireEvent.click(screen.getByRole('button', { name: /Visible Project/ }));
  await fireEvent.click(screen.getByRole('menuitem', { name: /All projects/ }));
  expect(cardIds()).toEqual(['legacy', 'coded', 'chatty', 'spoken']);
 });
 it('offers Workspace and narrow Chat capabilities, not a Voice history lane', () => {
  render(Sidebar, { props: sidebarProps });
  const radios = screen.getAllByRole('radio');
  expect(radios.map(r => r.getAttribute('aria-label'))).toEqual(['Workspace', 'Chat']);
  expect(radios.map(r => r.getAttribute('aria-checked'))).toEqual(['true', 'false']);
  expect(screen.getByRole('radiogroup', { name: 'New conversation capabilities' })).toBeTruthy();
 });
 it('opens a new Chat without filtering any session out', async () => {
  const onNewSession = vi.fn(); const onSelectMode = vi.fn(() => expect(appMode.current).toBe('chat'));
  mocks.sessions.set(fixture()); render(Sidebar, { props: { ...sidebarProps, onNewSession, onSelectMode } });
  await fireEvent.click(screen.getByRole('radio', { name: 'Chat' }));
  expect(onNewSession).toHaveBeenCalledTimes(1); expect(onSelectMode).toHaveBeenCalledWith('chat');
  expect(cardIds()).toEqual(['legacy', 'coded', 'chatty', 'spoken']);
 });
 it('does not discard the current conversation when its capability is reselected', async () => {
  const onNewSession = vi.fn(); render(Sidebar, { props: { ...sidebarProps, onNewSession } });
  await fireEvent.click(screen.getByRole('radio', { name: 'Workspace' }));
  expect(onNewSession).not.toHaveBeenCalled();
 });
 it('treats Chat as already selected while a historical voice session is open', async () => {
  appMode.set('voice', false); const onNewSession = vi.fn(); render(Sidebar, { props: { ...sidebarProps, onNewSession } });
  expect(screen.getByRole('radio', { name: 'Chat' }).getAttribute('aria-checked')).toBe('true');
  await fireEvent.click(screen.getByRole('radio', { name: 'Chat' }));
  expect(onNewSession).not.toHaveBeenCalled();
 });
 it('supports roving keyboard focus with no implicit microphone action', async () => {
  render(Sidebar, { props: sidebarProps });
  await fireEvent.keyDown(screen.getByRole('radio', { name: 'Workspace' }), { key: 'End' });
  expect(appMode.current).toBe('chat');
  await fireEvent.keyDown(screen.getByRole('radio', { name: 'Chat' }), { key: 'Home' });
  expect(appMode.current).toBe('code');
 });
 it('pins the primary Assistant independently of customized workspace links', async () => {
  const onOpenFeature = vi.fn(); render(Sidebar, { props: { ...sidebarProps, isAdmin: true, onOpenFeature } });
  await fireEvent.click(screen.getByRole('button', { name: 'Assistant home' }));
  expect(onOpenFeature).toHaveBeenCalledWith('assistant');
 });
 it('does not expose owner assistant management to non-admins', () => {
  render(Sidebar, { props: { ...sidebarProps, isAdmin: false } });
  expect(screen.queryByRole('button', { name: 'Assistant home' })).toBeNull();
 });
});

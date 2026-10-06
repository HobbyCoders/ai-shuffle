import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import { privateAccessPending } from '$lib/stores/chat-access';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
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
		setSessionActive: vi.fn(),
		setSessionArchived: vi.fn(),
		banner: vi.fn(),
		apiGet: vi.fn(),
		apiPost: vi.fn(),
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
		setSessionActive: mocks.setSessionActive,
		setSessionArchived: mocks.setSessionArchived,
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
	// The update button in the header reads the update policy (#1623).
	api: { get: mocks.apiGet, post: mocks.apiPost },
	getErrorMessage: (error: Error, fallback: string) => error?.message || fallback,
}));
vi.mock('$lib/api/assistant', () => ({ getAssistant: vi.fn().mockResolvedValue(null), getAgentIdentities: vi.fn().mockResolvedValue([]), openAssistant: vi.fn() }));

vi.mock('$lib/stores/notifications', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/stores/notifications')>(),
	notify: Object.assign(vi.fn(), {
		markAllRead: vi.fn(),
		banner: mocks.banner,
		dismissLive: vi.fn(),
		info: vi.fn(),
		warning: vi.fn(),
		error: vi.fn(),
	}),
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
import { acceptAssistant, assistantState } from '$lib/stores/assistant';
import type { AssistantState } from '$lib/api/assistant';

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

describe('Archived sessions', () => {
	it('keeps archived history out of the sidebar; Settings owns the archive', () => {
		mocks.sessions.set([session('history', 'Archived work', null, false, false, { status: 'archived' })]);
		render(Sidebar, sidebarProps);
		expect(screen.queryByText('Archived work')).toBeNull();
		expect(screen.queryByRole('button', { name: /^Archived/ })).toBeNull();
	});
});

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
	mocks.loadMoreSessions.mockReset().mockResolvedValue(undefined);
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
	mocks.setSessionActive.mockReset().mockResolvedValue(undefined);
	mocks.setSessionArchived.mockReset().mockResolvedValue(undefined);
	mocks.banner.mockReset().mockReturnValue('banner-id');
	mocks.apiGet.mockReset().mockResolvedValue({ allow_updates: false, update_all_workers: false, workers: [] });
	mocks.apiPost.mockReset();
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
	assistantState.set(null);
});

// ── Filter sheet ────────────────────────────────────────────────────────────

async function openFilter(): Promise<HTMLElement> {
	await fireEvent.click(screen.getByRole('button', { name: 'Filter' }));
	return screen.getByRole('dialog', { name: 'Filter sessions' });
}

function filterOption(label: string): HTMLElement {
	const sheet = screen.getByRole('dialog', { name: 'Filter sessions' });
	return within(sheet).getByRole('button', { name: label });
}

function filterLabels(): string[] {
	const sheet = screen.getByRole('dialog', { name: 'Filter sessions' });
	return within(sheet).getAllByRole('button').map((node) => node.textContent?.trim() ?? '');
}

/** Section headers, whitespace-normalized: "Active 1". */
function sectLabels(): string[] {
	return Array.from(document.querySelectorAll('.sect')).map((node) =>
		node.textContent?.replace(/\s+/g, ' ').trim() ?? ''
	);
}

describe('Sidebar session groups', () => {
	it('shares collapsed groups with a mounted sidebar and resets them on account change', async () => {
		const { container } = render(Sidebar);
		const header = () => Array.from(container.querySelectorAll<HTMLButtonElement>('.sect'))
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

		expect(sectLabels()).toEqual(['Active 1', 'Settled 1']);
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
		await openFilter();
		expect(filterLabels()).toContain('Visible Project');
		expect(filterLabels()).not.toContain('Hidden Project');
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
		await openFilter();
		expect(filterLabels()).toContain('Visible Project');
		expect(filterLabels().join('|')).not.toMatch(/deleted/i);
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
		await openFilter();
		expect(filterLabels()).toContain('Other');
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
		await openFilter();
		expect(filterLabels()).toEqual(expect.arrayContaining(['All devices', 'This device', 'Laptop']));
		expect(filterLabels()).not.toContain('Unknown device');
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

	it('marks the filter button and shows a clearable chip once a filter is applied', async () => {
		mocks.sessions.set([
			session('active', 'Active chat', 'visible-project', true),
			session('other', 'Other project chat', 'other-project', false, false, { project_name: 'Other Project' }),
			session('peer', 'Laptop chat', 'visible-project', false, false, {
				is_remote: true,
				instance_id: 'laptop',
				device_name: 'Laptop',
			}),
		]);
		mocks.projects.set([
			{ id: 'visible-project', name: 'Visible Project' },
			{ id: 'other-project', name: 'Other Project' },
		]);
		render(Sidebar, { props: sidebarProps });

		expect(document.querySelector('.filter-btn.set')).toBeNull();

		await openFilter();
		await fireEvent.click(filterOption('Visible Project'));
		expect(filterOption('Visible Project').getAttribute('aria-pressed')).toBe('true');
		await fireEvent.click(filterOption('Laptop'));

		expect(document.querySelector('.filter-btn.set')).toBeTruthy();
		expect(cardIds()).toEqual(['peer']);

		await fireEvent.click(screen.getByRole('button', { name: 'Clear device filter Laptop' }));
		expect(cardIds()).toEqual(['active', 'peer']);
		await fireEvent.click(screen.getByRole('button', { name: 'Clear project filter Visible Project' }));
		expect(cardIds()).toEqual(['active', 'other', 'peer']);
		expect(document.querySelector('.filter-btn.set')).toBeNull();
	});

	it('closes the filter sheet on Escape and on the scrim', async () => {
		render(Sidebar, { props: sidebarProps });

		await openFilter();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByRole('dialog', { name: 'Filter sessions' })).toBeNull();

		await openFilter();
		await fireEvent.click(document.querySelector('.sheet-scrim.open') as HTMLElement);
		expect(screen.queryByRole('dialog', { name: 'Filter sessions' })).toBeNull();
	});
});

describe('Sidebar Needs you lane', () => {
	const labels = sectLabels;
	const lane = () => document.querySelector('[data-group="needs-you"]');

	// #1807: a private-access request blocks the agent on the owner like a
	// permission prompt, so it lifts the row the same way.
	it('lifts a row waiting on a private-access request, and back when answered', async () => {
		privateAccessPending.set(new Map([['active', 1]]));
		try {
			render(Sidebar, { props: sidebarProps });
			expect(labels()).toEqual(['Needs you 1', 'Active 0', 'Settled 1']);
			expect(lane()?.textContent).toContain('Active chat');
			expect(cardFrame('Active chat').textContent).toContain('Needs permission');

			privateAccessPending.set(new Map());
			await tick();
			expect(labels()).toEqual(['Active 1', 'Settled 1']);
		} finally {
			privateAccessPending.set(new Map());
		}
	});

	it('lifts rows waiting on a prompt out of Active and Settled, and back when answered', async () => {
		mocks.backgroundPermissions.set(new Map([
			['active', { count: 1, kind: 'permission' }],
			['settled', { count: 1, kind: 'question' }],
		]));
		render(Sidebar, { props: sidebarProps });
		expect(labels()).toEqual(['Needs you 2', 'Active 0', 'Settled 0']);
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
	it('keeps a Monitor row with no parked marker Parked (not Working) through selection, switching away, and list reload', async () => {
		const monitor = session('monitor', 'Monitor chat', 'visible-project', true, false, {
			has_running_tasks: true, parked_task_kind: null,
		});
		const other = session('other', 'Other chat', 'visible-project', true);
		mocks.sessions.set([monitor, other]);
		const select = (sessionId: string) => mocks.activeSession.set({ sessionId, isStreaming: false, activeTasks: [] });
		render(Sidebar, { props: { ...sidebarProps, onOpenSession: select } });
		expect(cardFrame('Monitor chat').textContent).toContain('Parked');
		await fireEvent.click(screen.getByText('Monitor chat'));
		expect(mocks.activeSession.current.sessionId).toBe('monitor');
		expect(cardFrame('Monitor chat').textContent).toContain('Parked');
		await fireEvent.click(screen.getByText('Other chat'));
		expect(mocks.activeSession.current.sessionId).toBe('other');
		expect(cardFrame('Monitor chat').textContent).toContain('Parked');
		mocks.sessions.set([{ ...monitor }, { ...other }]);
		await tick();
		expect(cardFrame('Monitor chat').textContent).toContain('Parked');
		expect(cardFrame('Other chat').textContent).toContain('Done');
	});

	it('tracks concurrent Monitor sessions independently as Parked and shows Done when each completes', async () => {
		const first = session('first', 'First monitor', 'visible-project', true, false, { has_running_tasks: true, parked_task_kind: null });
		const second = session('second', 'Second monitor', 'visible-project', true, false, { has_running_tasks: true, parked_task_kind: null });
		mocks.sessions.set([first, second]);
		render(Sidebar, { props: sidebarProps });
		expect(cardFrame('First monitor').textContent).toContain('Parked');
		expect(cardFrame('Second monitor').textContent).toContain('Parked');
		mocks.sessions.set([{ ...first, has_running_tasks: false }, second]);
		await tick();
		expect(cardFrame('First monitor').textContent).toContain('Done');
		expect(cardFrame('Second monitor').textContent).toContain('Parked');
		mocks.sessions.set([{ ...first, has_running_tasks: false }, { ...second, has_running_tasks: false }]);
		await tick();
		expect(cardFrame('Second monitor').textContent).toContain('Done');
		expect(screen.queryByText('Parked')).toBeNull();
		expect(screen.queryByText('Working')).toBeNull();
	});

	it.each(['permission', 'question'] as const)('prioritizes %s attention over durable Monitor work', async (kind) => {
		mocks.sessions.set([session('monitor', 'Monitor chat', 'visible-project', true, false, { has_running_tasks: true, parked_task_kind: null })]);
		mocks.backgroundPermissions.set(new Map([['monitor', { count: 1, kind }]]));
		render(Sidebar, { props: sidebarProps });
		expect(cardFrame('Monitor chat').textContent).not.toContain('Parked');
		expect(cardFrame('Monitor chat').textContent).not.toContain('Working');
		expect(cardFrame('Monitor chat').textContent).toContain(kind === 'permission' ? 'Needs permission' : 'Has question');
		mocks.backgroundPermissions.set(new Map());
		await tick();
		expect(cardFrame('Monitor chat').textContent).toContain('Parked');
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

describe('Sidebar multi-select (R15)', () => {
	// Long press is a touch gesture, so selection starts from the mobile shell.
	beforeEach(() => platform.set('mobile'));

	async function longPress(id: string) {
		const frame = document.querySelector(`[data-session-card="${id}"]`) as HTMLElement;
		const card = frame.querySelector('.scard') as HTMLElement;
		await fireEvent.pointerDown(card, { pointerId: 1, isPrimary: true, button: 0, pointerType: 'touch', clientX: 100, clientY: 20 });
		await vi.advanceTimersByTimeAsync(520);
		await fireEvent.pointerUp(frame, { pointerId: 1, isPrimary: true, button: 0, pointerType: 'touch', clientX: 100, clientY: 20 });
		return card;
	}

	const selbar = () => document.querySelector('.selbar') as HTMLElement;

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

		// Every SessionCard used to attach its own listener, so a single click
		// anywhere ran one handler per rendered card. Assert the invariant that
		// matters: the count is flat in the number of cards.
		expect(countClickListeners(12)).toBe(countClickListeners(1));
	});

	it('starts selecting on a long press, then settles the picked rows with Undo', async () => {
		vi.useFakeTimers();
		try {
			const onOpenSession = vi.fn();
			render(Sidebar, { props: { ...sidebarProps, onOpenSession } });
			expect(selbar().getAttribute('aria-hidden')).toBe('true');

			const card = await longPress('active');
			await fireEvent.click(card);
			expect(selbar().getAttribute('aria-hidden')).toBeNull();
			expect(selbar().textContent).toContain('1 selected');
			expect(onOpenSession).not.toHaveBeenCalled();

			// Only an active row is picked: Settle is offered, Active is not.
			expect(within(selbar()).queryByRole('button', { name: /Active/ })).toBeNull();
			await fireEvent.click(within(selbar()).getByRole('button', { name: /Settle/ }));
			await vi.advanceTimersByTimeAsync(0);

			expect(mocks.setSessionActive).toHaveBeenCalledWith('active', false);
			expect(mocks.banner).toHaveBeenCalledTimes(1);
			const [, options] = mocks.banner.mock.calls[0] as [string, { action?: { label: string } }];
			expect(options.action?.label).toBe('Undo');
			expect(selbar().getAttribute('aria-hidden')).toBe('true');
		} finally {
			vi.useRealTimers();
		}
	});

	it('adds rows with a tap while selecting and offers both moves for a mixed pick', async () => {
		vi.useFakeTimers();
		try {
			const onOpenSession = vi.fn();
			render(Sidebar, { props: { ...sidebarProps, onOpenSession } });

			await longPress('active');
			await fireEvent.click(document.querySelector('[data-session-card="settled"] .scard') as HTMLElement);

			expect(selbar().textContent).toContain('2 selected');
			expect(onOpenSession).not.toHaveBeenCalled();
			expect(within(selbar()).getByRole('button', { name: /Active/ })).toBeTruthy();
			expect(within(selbar()).getByRole('button', { name: /Settle/ })).toBeTruthy();

			await fireEvent.click(within(selbar()).getByRole('button', { name: 'Archive' }));
			await vi.advanceTimersByTimeAsync(0);
			expect(mocks.setSessionArchived).toHaveBeenCalledWith('active', true, true);
			expect(mocks.setSessionArchived).toHaveBeenCalledWith('settled', true, false);
		} finally {
			vi.useRealTimers();
		}
	});

	it('cancels selection on Escape and from the bar', async () => {
		vi.useFakeTimers();
		try {
			render(Sidebar, { props: sidebarProps });

			await longPress('settled');
			await fireEvent.keyDown(window, { key: 'Escape' });
			expect(selbar().getAttribute('aria-hidden')).toBe('true');

			await longPress('settled');
			await fireEvent.click(within(selbar()).getByRole('button', { name: 'Cancel selection' }));
			expect(selbar().getAttribute('aria-hidden')).toBe('true');
			expect(mocks.setSessionActive).not.toHaveBeenCalled();
		} finally {
			vi.useRealTimers();
		}
	});
});

// Each test renders the full sidebar and clicks through a multi-page run; the
// default 5 s budget is too tight when the whole suite shares the CPU.
describe('Sidebar load-more shelf (W1)', { timeout: 20_000 }, () => {
	const older = (n: number, extra: Record<string, unknown> = {}) =>
		session(`old-${n}`, `Older chat ${n}`, 'visible-project', false, false, {
			updated_at: `2026-07-${String(10 + (n % 18)).padStart(2, '0')}T12:00:00Z`,
			...extra,
		});

	/** Make the mocked store append `rows` per call, ending after `pages`. */
	function serverPages(pages: unknown[][]) {
		let served = 0;
		mocks.loadMoreSessions.mockImplementation(async () => {
			const rows = pages[served++] ?? [];
			mocks.sessions.set([...(mocks.sessions.current as unknown[]), ...rows]);
			mocks.sessionsHasMore.set(served < pages.length);
		});
	}

	const shelf = (name: RegExp) => screen.queryByRole('button', { name });
	/** Let a click's whole multi-page run finish (it awaits once per page). */
	const settle = async () => {
		await new Promise((done) => setTimeout(done, 0));
		await tick();
	};

	it('loads the next page, shows it, and goes away once the server has no more', async () => {
		mocks.sessionsHasMore.set(true);
		serverPages([[older(1), older(2)]]);
		render(Sidebar, { props: sidebarProps });

		await fireEvent.click(screen.getByRole('button', { name: /Load older sessions/ }));
		await settle();

		expect(mocks.loadMoreSessions).toHaveBeenCalledTimes(1);
		expect(screen.getByText('Older chat 1')).toBeTruthy();
		expect(shelf(/Load older sessions/)).toBeNull();
		// Nothing would collapse: three settled rows fit under the cap.
		expect(shelf(/Show fewer/)).toBeNull();
	});

	it('hides the shelf when everything is loaded', () => {
		mocks.sessionsHasMore.set(false);

		render(Sidebar, { props: sidebarProps });

		expect(shelf(/Load older sessions/)).toBeNull();
	});

	it('keeps paging past rows this list drops instead of leaving an endless button', async () => {
		// Pages of rows from a deleted project: nothing the sidebar can show.
		const gone = (n: number) => older(n, { project_id: 'deleted-project', project_name: null });
		mocks.sessionsHasMore.set(true);
		serverPages([[gone(1)], [gone(2)], [older(3)]]);
		render(Sidebar, { props: sidebarProps });

		await fireEvent.click(screen.getByRole('button', { name: /Load older sessions/ }));
		await settle();

		// One click walked to the first page with a visible row.
		expect(mocks.loadMoreSessions).toHaveBeenCalledTimes(3);
		expect(screen.getByText('Older chat 3')).toBeTruthy();
		expect(shelf(/Load older sessions/)).toBeNull();
		expect(shelf(/Show fewer/)).toBeNull();
	});

	it('stops after a bounded run of empty pages and never turns into "Show fewer"', async () => {
		mocks.sessionsHasMore.set(true);
		// The store reports more but every page adds nothing visible.
		mocks.loadMoreSessions.mockImplementation(async () => {});
		render(Sidebar, { props: sidebarProps });

		await fireEvent.click(screen.getByRole('button', { name: /Load older sessions/ }));
		await settle();

		expect(mocks.loadMoreSessions).toHaveBeenCalledTimes(5);
		expect(shelf(/Load older sessions/)).toBeTruthy();
		expect(shelf(/Show fewer/)).toBeNull();

		// Once the server runs out, the shelf leaves for good.
		mocks.sessionsHasMore.set(false);
		await tick();
		expect(shelf(/Load older sessions|Show fewer|more/)).toBeNull();
	});

	it('runs one load per burst of clicks and keeps a stable loading label', async () => {
		// Freeze the clock: "the same instant as the load" must not depend on
		// how fast the machine renders.
		vi.useFakeTimers({ toFake: ['Date'] });
		onTestFinished(() => {
			vi.useRealTimers();
		});
		mocks.sessionsHasMore.set(true);
		let release!: () => void;
		mocks.loadMoreSessions.mockImplementation(
			() =>
				new Promise<void>((done) => {
					release = () => {
						mocks.sessions.set([
							...(mocks.sessions.current as unknown[]),
							...Array.from({ length: 40 }, (_, i) => older(i + 1)),
						]);
						mocks.sessionsHasMore.set(false);
						done();
					};
				})
		);
		render(Sidebar, { props: sidebarProps });

		const button = screen.getByRole('button', { name: /Load older sessions/ });
		await fireEvent.click(button);
		await tick();
		const loading = screen.getByRole('button', { name: /Loading older sessions/ });
		expect((loading as HTMLButtonElement).disabled).toBe(true);
		for (let i = 0; i < 5; i++) await fireEvent.click(loading);
		expect(mocks.loadMoreSessions).toHaveBeenCalledTimes(1);

		release();
		await settle();

		// 41 settled rows are all on screen, so the shelf offers to collapse…
		const fewer = screen.getByRole('button', { name: /Show fewer/ });
		// …but a click landing in the same instant as the load is ignored.
		await fireEvent.click(fewer);
		expect(screen.getByText('Older chat 40')).toBeTruthy();
		expect(shelf(/Show fewer/)).toBeTruthy();
		expect(mocks.loadMoreSessions).toHaveBeenCalledTimes(1);
	});

	it('collapses on a deliberate "Show fewer" and offers the rest again', async () => {
		vi.useFakeTimers({ toFake: ['Date'] });
		try {
			mocks.sessionsHasMore.set(true);
			serverPages([Array.from({ length: 40 }, (_, i) => older(i + 1))]);
			render(Sidebar, { props: sidebarProps });

			await fireEvent.click(screen.getByRole('button', { name: /Load older sessions/ }));
			await tick();
			vi.setSystemTime(Date.now() + 1000);

			await fireEvent.click(screen.getByRole('button', { name: /Show fewer/ }));
			expect(screen.getByRole('button', { name: /Show 11 more/ })).toBeTruthy();
			expect(shelf(/Load older sessions/)).toBeNull();
		} finally {
			vi.useRealTimers();
		}
	});
});

// ── Rail helpers (shared by the blocks below) ──────────────────────────────

/** A rail destination button, by its label. */
function railItem(label: string): HTMLElement {
	const rail = screen.getByRole('navigation', { name: 'Workspace' });
	return within(rail).getByRole('button', { name: label });
}

/** Labels of the rail's destinations, in order (New, More and Settings excluded). */
function railLabels(): string[] {
	return Array.from(document.querySelectorAll('[data-rail-item]')).map(
		(node) => node.getAttribute('aria-label') ?? ''
	);
}

/** Open the More menu and return its items. */
async function openMore(): Promise<HTMLElement[]> {
	await fireEvent.click(railItem('More'));
	const menu = screen.getByRole('menu', { name: 'More destinations' });
	return within(menu).getAllByRole('menuitem') as HTMLElement[];
}

function menuItem(label: string): HTMLElement {
	const menu = screen.getByRole('menu', { name: 'More destinations' });
	const item = within(menu)
		.getAllByRole('menuitem')
		.find((node) => node.textContent?.trim().startsWith(label));
	if (!item) throw new Error(`no More item labelled ${label}`);
	return item as HTMLElement;
}

const itemLabel = (node: HTMLElement) => node.querySelector('span')?.textContent?.trim();

async function openCustomize(): Promise<HTMLElement> {
	await openMore();
	await fireEvent.click(menuItem('Customize the rail'));
	return screen.getByRole('dialog', { name: 'Customize the rail' });
}

describe('Sidebar rail prefetch', () => {
	it('does not prefetch anything just by rendering', () => {
		render(Sidebar, { props: sidebarProps });

		expect(panelSpies.preloadCalls).toEqual([]);
		expect(panelSpies.chunkFetches).toEqual([]);
	});

	it('prefetches the panel chunk when the pointer enters a rail button', async () => {
		render(Sidebar, { props: sidebarProps });

		await fireEvent.pointerEnter(railItem('Projects'));
		await Promise.resolve();

		expect(panelSpies.preloadCalls).toEqual(['project']);
		expect(panelSpies.chunkFetches).toEqual(['project']);
	});

	it('prefetches on focus, so keyboard navigation warms the same chunk', async () => {
		render(Sidebar, { props: sidebarProps });

		await fireEvent.focus(railItem('Agents'));
		await Promise.resolve();

		expect(panelSpies.preloadCalls).toEqual(['build']);
		expect(panelSpies.chunkFetches).toEqual(['build']);
	});

	it('prefetches from a More item too — a folded destination is no less likely', async () => {
		render(Sidebar, { props: sidebarProps });

		await openMore();
		await fireEvent.pointerEnter(menuItem('Stats'));
		await Promise.resolve();

		// Opening the menu focuses its first item, and focus is intent too, so
		// the preload may be asked twice; the chunk itself is fetched once.
		expect(new Set(panelSpies.preloadCalls)).toEqual(new Set(['stats']));
		expect(panelSpies.chunkFetches).toEqual(['stats']);
	});

	it('does not re-fetch a chunk on repeated hover or on a later focus', async () => {
		render(Sidebar, { props: sidebarProps });
		const button = railItem('Files');

		await fireEvent.pointerEnter(button);
		await Promise.resolve();
		await fireEvent.pointerLeave(button);
		await fireEvent.pointerEnter(button);
		await fireEvent.focus(button);
		await Promise.resolve();

		// The component asked three times — the handle fetched once.
		expect(panelSpies.preloadCalls).toEqual(['file-browser', 'file-browser', 'file-browser']);
		expect(panelSpies.chunkFetches).toEqual(['file-browser']);
	});

	it('still routes the click through onOpenFeature after a prefetch', async () => {
		const onOpenFeature = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, onOpenFeature } });

		const button = railItem('Files');
		await fireEvent.pointerEnter(button);
		await fireEvent.click(button);

		expect(onOpenFeature).toHaveBeenCalledWith('file-browser');
	});
});

describe('Sidebar rail customization', () => {
	it('shows the default five destinations on the rail and folds the rest into More', async () => {
		render(Sidebar, { props: sidebarProps });

		expect(railLabels()).toEqual(['Files', 'Notes', 'Agents', 'Projects', 'Pull requests']);

		const items = await openMore();
		expect(items.map((n) => itemLabel(n))).toEqual(['Stats', 'Docs', 'Customize the rail']);
		expect(menuItem('Customize the rail').textContent).toContain('5 on the rail');
	});

	it('never offers an admin-only destination to a non-admin, on the rail or folded', async () => {
		render(Sidebar, { props: sidebarProps });

		const items = await openMore();
		const labels = [...railLabels(), ...items.map((n) => itemLabel(n))];
		expect(labels).not.toContain('Automations');
		expect(screen.queryByRole('button', { name: 'Settings' })).toBeNull();
	});

	it('pins Settings at the rail foot and folds Automations into More for an admin', async () => {
		const onOpenFeature = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true, onOpenFeature } });

		await fireEvent.click(railItem('Settings'));
		expect(onOpenFeature).toHaveBeenCalledWith('settings');
		const items = await openMore();
		expect(items.map((n) => itemLabel(n))).toContain('Automations');
	});

	it('opens a folded destination from More and closes the menu behind itself', async () => {
		const onOpenFeature = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, onOpenFeature } });

		await openMore();
		expect(railItem('More').getAttribute('aria-expanded')).toBe('true');
		await fireEvent.click(menuItem('Docs'));

		expect(onOpenFeature).toHaveBeenCalledWith('docs');
		expect(screen.queryByRole('menu', { name: 'More destinations' })).toBeNull();
		expect(railItem('More').getAttribute('aria-expanded')).toBe('false');
	});

	it('closes More on Escape', async () => {
		render(Sidebar, { props: sidebarProps });

		await openMore();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByRole('menu', { name: 'More destinations' })).toBeNull();
	});

	it('puts a folded destination on the rail from Customize', async () => {
		render(Sidebar, { props: sidebarProps });

		const dialog = await openCustomize();
		// New is listed but pinned: it is not a destination.
		expect(dialog.textContent).toContain('Always first');

		await fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Show Stats on the rail' }));
		expect(railLabels()).toEqual(['Files', 'Notes', 'Agents', 'Projects', 'Pull requests', 'Stats']);

		// And it survives the dialog, because the store wrote it.
		await fireEvent.click(within(dialog).getByRole('button', { name: 'Done' }));
		expect(screen.queryByRole('dialog', { name: 'Customize the rail' })).toBeNull();
		expect(railLabels()).toEqual(['Files', 'Notes', 'Agents', 'Projects', 'Pull requests', 'Stats']);
	});

	it('moves a destination off the rail into More', async () => {
		render(Sidebar, { props: sidebarProps });

		const dialog = await openCustomize();
		const box = within(dialog).getByRole('checkbox', { name: 'Move Notes to More' });
		expect(box.getAttribute('aria-checked')).toBe('true');
		await fireEvent.click(box);
		expect(railLabels()).not.toContain('Notes');
	});

	it('reorders a destination from the keyboard, so the feature is not pointer-only', async () => {
		render(Sidebar, { props: sidebarProps });

		const dialog = await openCustomize();
		const grip = within(dialog).getByRole('button', { name: 'Reorder Projects' });
		await fireEvent.keyDown(grip, { key: 'ArrowUp' });
		await fireEvent.keyDown(grip, { key: 'ArrowUp' });

		expect(railLabels()).toEqual(['Files', 'Projects', 'Notes', 'Agents', 'Pull requests']);
	});

	it('lists the pinned Settings row only for an admin', async () => {
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true } });

		const dialog = await openCustomize();
		expect(dialog.textContent).toContain('Always last');
	});

	it('closes Customize on Escape before anything else', async () => {
		render(Sidebar, { props: sidebarProps });

		await openCustomize();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByRole('dialog', { name: 'Customize the rail' })).toBeNull();
	});

	it('opens Customize from a long press on a rail button, without opening it', async () => {
		vi.useFakeTimers();
		try {
			platform.set('mobile');
			const onOpenFeature = vi.fn();
			render(Sidebar, { props: { ...sidebarProps, onOpenFeature } });
			const button = railItem('Files');

			await fireEvent.pointerDown(button, { pointerId: 1, isPrimary: true, pointerType: 'touch', clientX: 10, clientY: 10 });
			await vi.advanceTimersByTimeAsync(600);
			await fireEvent.pointerUp(button, { pointerId: 1, isPrimary: true, pointerType: 'touch' });
			await fireEvent.click(button);

			expect(screen.getByRole('dialog', { name: 'Customize the rail' })).toBeTruthy();
			expect(onOpenFeature).not.toHaveBeenCalled();
		} finally {
			vi.useRealTimers();
		}
	});

	it('does not long-press with a mouse', async () => {
		vi.useFakeTimers();
		try {
			render(Sidebar, { props: sidebarProps });
			await fireEvent.pointerDown(railItem('Files'), { pointerId: 1, isPrimary: true, pointerType: 'mouse' });
			await vi.advanceTimersByTimeAsync(600);
			expect(screen.queryByRole('dialog', { name: 'Customize the rail' })).toBeNull();
		} finally {
			vi.useRealTimers();
		}
	});
});

describe('Sidebar Check for updates (#1623)', () => {
	it('offers a manual check under More for an admin while updates are allowed', async () => {
		mocks.apiGet.mockResolvedValue({ allow_updates: true, update_all_workers: false, workers: [] });
		mocks.apiPost.mockResolvedValue({ results: [{ device_name: 'This worker', accepted: true, reason: 'Request queued' }] });
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true } });

		await vi.waitFor(() => expect(mocks.apiGet).toHaveBeenCalled());
		await openMore();
		await vi.waitFor(() => expect(menuItem('Check for updates')).toBeTruthy());
		await fireEvent.click(menuItem('Check for updates'));

		await vi.waitFor(() =>
			expect(mocks.apiPost).toHaveBeenCalledWith('/settings/application-updates', { action: 'check' })
		);
		expect(screen.queryByRole('menu', { name: 'More destinations' })).toBeNull();
	});

	it('leaves it out while updates are disabled', async () => {
		mocks.apiGet.mockResolvedValue({ allow_updates: false, update_all_workers: false, workers: [] });
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true } });

		await vi.waitFor(() => expect(mocks.apiGet).toHaveBeenCalled());
		const items = await openMore();
		expect(items.map((n) => itemLabel(n))).not.toContain('Check for updates');
	});

	it('never offers it to a non-admin', async () => {
		mocks.apiGet.mockResolvedValue({ allow_updates: true, update_all_workers: false, workers: [] });
		render(Sidebar, { props: sidebarProps });

		await vi.waitFor(() => expect(mocks.apiGet).toHaveBeenCalled());
		await tick();
		const items = await openMore();
		expect(items.map((n) => itemLabel(n))).not.toContain('Check for updates');
	});
});

describe('Sidebar header', () => {
	it('starts a new session from the rail, named for the selected capability', async () => {
		const onNewSession = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, onNewSession } });

		await fireEvent.click(railItem('New workspace session'));
		expect(onNewSession).toHaveBeenCalledTimes(1);

		appMode.set('chat');
		await tick();
		expect(railItem('New chat')).toBeTruthy();
	});

	it('opens the spotlight from the search field', async () => {
		const onOpenSpotlight = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, onOpenSpotlight } });

		await fireEvent.click(screen.getByRole('button', { name: /Search/ }));

		expect(onOpenSpotlight).toHaveBeenCalledTimes(1);
	});

	it('disables search when the host threads no spotlight', () => {
		render(Sidebar, { props: sidebarProps });

		expect((screen.getByRole('button', { name: /Search/ }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('shows the shortcut hint on desktop only', () => {
		render(Sidebar, { props: sidebarProps });
		expect(screen.getByRole('button', { name: /Search/ }).querySelector('kbd')).toBeTruthy();
		cleanup();

		platform.set('mobile');
		render(Sidebar, { props: sidebarProps });
		expect(screen.getByRole('button', { name: /Search/ }).querySelector('kbd')).toBeNull();
	});

	it('hosts the notification bell — its only home now', () => {
		render(Sidebar, { props: sidebarProps });

		expect(screen.getByRole('button', { name: /Notifications/ })).toBeTruthy();
	});

	it('keeps the capability switcher in the header', () => {
		render(Sidebar, { props: sidebarProps });

		const head = document.querySelector('.p-head');
		expect(head?.querySelector('[role="radiogroup"]')).toBeTruthy();
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
	it.each(['code', 'chat', 'voice'] as const)('keeps every historical conversation visible while %s is selected', (mode) => {
		appMode.set(mode);
		mocks.sessions.set(fixture());
		render(Sidebar, { props: sidebarProps });
		expect(cardIds()).toEqual(['legacy', 'coded', 'chatty', 'spoken']);
		expect(screen.getByRole('button', { name: 'Filter' })).toBeTruthy();
	});
	it('names the empty state of each lane', () => {
		mocks.sessions.set([]);
		appMode.set('voice');
		render(Sidebar, { props: sidebarProps });
		const empties = Array.from(document.querySelectorAll('.empty')).map((n) => n.textContent?.trim());
		expect(empties).toEqual(['Nothing active. Swipe a settled session right to bring it back.', 'No sessions yet']);
	});
	it('retains explicit project scope when new-conversation capabilities change', async () => {
		mocks.sessions.set(fixture());
		render(Sidebar, { props: sidebarProps });
		await openFilter();
		await fireEvent.click(filterOption('Visible Project'));
		await fireEvent.click(screen.getByRole('radio', { name: 'Chat' }));
		expect(cardIds()).toEqual(['legacy', 'coded']);
		await fireEvent.click(screen.getByRole('button', { name: 'Clear project filter Visible Project' }));
		expect(cardIds()).toEqual(['legacy', 'coded', 'chatty', 'spoken']);
	});
	it('offers Workspace and narrow Chat capabilities, not a Voice history lane', () => {
		render(Sidebar, { props: sidebarProps });
		const radios = screen.getAllByRole('radio');
		expect(radios.map((r) => r.getAttribute('aria-label'))).toEqual(['Workspace', 'Chat']);
		expect(radios.map((r) => r.getAttribute('aria-checked'))).toEqual(['true', 'false']);
		expect(screen.getByRole('radiogroup', { name: 'New conversation capabilities' })).toBeTruthy();
	});
	it('opens a new Chat without filtering any session out', async () => {
		const onNewSession = vi.fn();
		const onSelectMode = vi.fn(() => expect(appMode.current).toBe('chat'));
		mocks.sessions.set(fixture());
		render(Sidebar, { props: { ...sidebarProps, onNewSession, onSelectMode } });
		await fireEvent.click(screen.getByRole('radio', { name: 'Chat' }));
		expect(onNewSession).toHaveBeenCalledTimes(1);
		expect(onSelectMode).toHaveBeenCalledWith('chat');
		expect(cardIds()).toEqual(['legacy', 'coded', 'chatty', 'spoken']);
	});
	it('does not discard the current conversation when its capability is reselected', async () => {
		const onNewSession = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, onNewSession } });
		await fireEvent.click(screen.getByRole('radio', { name: 'Workspace' }));
		expect(onNewSession).not.toHaveBeenCalled();
	});
	it('treats Chat as already selected while a historical voice session is open', async () => {
		appMode.set('voice', false);
		const onNewSession = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, onNewSession } });
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
});

describe('Sidebar assistant row', () => {
	const ready = {
		configured: true,
		status: 'ready',
		home_session_id: 'home',
		assistant_name: 'Juniper',
		avatar_id: 'robot',
	} as unknown as AssistantState;

	afterEach(() => assistantState.set(null));

	it('opens the assistant settings while it is not set up', async () => {
		const onOpenFeature = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true, onOpenFeature } });
		const row = screen.getByRole('button', { name: 'Open your assistant' });
		expect(row.textContent).toContain('Set up your assistant');
		await fireEvent.click(row);
		expect(onOpenFeature).toHaveBeenCalledWith('assistant');
	});

	it('opens its one conversation once set up, and keeps it out of every lane', async () => {
		mocks.sessions.set([
			session('home', 'Assistant home chat', null, true),
			session('active', 'Active chat', 'visible-project', true),
		]);
		const onOpenSession = vi.fn();
		const onOpenFeature = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true, onOpenSession, onOpenFeature } });
		acceptAssistant(ready);
		await tick();

		const row = screen.getByRole('button', { name: 'Open your assistant' });
		expect(row.textContent).toContain('Juniper');
		expect(cardIds()).toEqual(['active']);
		await fireEvent.keyDown(row, { key: 'Enter' });
		expect(onOpenSession).toHaveBeenCalledWith('home');
		expect(onOpenFeature).not.toHaveBeenCalled();
	});

	it('opens the conversation without research sources, creating the home when there is none', async () => {
		const { openAssistant } = await import('$lib/api/assistant');
		vi.mocked(openAssistant).mockResolvedValue({ session_id: 'new-home', home_session_id: 'new-home' });
		const onOpenSession = vi.fn();
		const onOpenFeature = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true, onOpenSession, onOpenFeature } });
		acceptAssistant({ ...ready, status: 'no_sources', research_sources: 0, home_session_id: null } as unknown as AssistantState);
		await tick();

		const row = screen.getByRole('button', { name: 'Open your assistant' });
		expect(row.textContent).not.toContain('Set up your assistant');
		await fireEvent.click(row);
		await vi.waitFor(() => expect(onOpenSession).toHaveBeenCalledWith('new-home'));
		expect(openAssistant).toHaveBeenCalledTimes(1);
		expect(onOpenFeature).not.toHaveBeenCalled();
	});

	it('opens an existing home directly even when no source is switched on', async () => {
		const { openAssistant } = await import('$lib/api/assistant');
		vi.mocked(openAssistant).mockClear();
		const onOpenSession = vi.fn();
		const onOpenFeature = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true, onOpenSession, onOpenFeature } });
		acceptAssistant({ ...ready, status: 'no_sources', research_sources: 0 } as unknown as AssistantState);
		await tick();
		await fireEvent.click(screen.getByRole('button', { name: 'Open your assistant' }));
		expect(onOpenSession).toHaveBeenCalledWith('home');
		expect(openAssistant).not.toHaveBeenCalled();
		expect(onOpenFeature).not.toHaveBeenCalled();
	});

	it('marks the row current while its conversation is on screen', async () => {
		mocks.activeView.set({ type: 'session' });
		mocks.activeSession.set({ sessionId: 'home', isStreaming: false, activeTasks: [] });
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true } });
		acceptAssistant(ready);
		await tick();
		expect(screen.getByRole('button', { name: 'Open your assistant' }).getAttribute('aria-current')).toBe('page');
		mocks.activeView.set(null);
	});

	it('does not expose the assistant to non-admins', () => {
		render(Sidebar, { props: { ...sidebarProps, isAdmin: false } });
		expect(screen.queryByRole('button', { name: 'Open your assistant' })).toBeNull();
	});
});

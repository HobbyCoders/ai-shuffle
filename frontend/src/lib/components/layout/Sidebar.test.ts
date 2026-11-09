import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import { privateAccessPending } from '$lib/stores/chat-access';
import { _resetProseQuestions, observeLastUserMessage, rememberLeavingProseQuestion } from '$lib/stores/session/prose-questions';
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
		projectCatalogReady: store(true),
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
 register: (key: string, adapter: PreferenceAdapter) => mocks.adapters.set(key, adapter), registerPrefix: vi.fn(), save: mocks.save
} }));

vi.mock('$lib/stores/app-data.svelte', () => ({
	sessions: mocks.sessions,
	projects: mocks.projects,
	clusterProjects: mocks.clusterProjects,
	projectCatalogReady: mocks.projectCatalogReady,
	sessionsHasMore: mocks.sessionsHasMore,
	settledTotal: { subscribe: (run: (count: number) => void) => mocks.sessions.subscribe(rows => run(rows.filter((row: any) => !row.hidden && !row.is_active && row.status !== 'archived').length)) },
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

vi.mock('$lib/stores/layout.svelte', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/stores/layout.svelte')>(),
	activeView: mocks.activeView,
}));

vi.mock('$lib/api/client', () => ({
	forkSession: vi.fn(),
	// The update button in the header reads the update policy (#1623).
	api: { get: mocks.apiGet, post: mocks.apiPost },
	getErrorMessage: (error: Error, fallback: string) => error?.message || fallback,
}));
vi.mock('$lib/api/agent-identity', () => ({ getAgentIdentities: vi.fn().mockResolvedValue([]) }));

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

// The Terminal destination is gated on the worker's terminal probe. Refused
// by default (as for a viewer or the preview cookie); a test that wants the
// item resolves a supported worker instead.
const terminalTargets = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: { ...actual.terminalsApi, targets: terminalTargets } };
});

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

// jsdom has no Web Animations, and Svelte asks an `animate:` block's leaving rows about theirs.
Element.prototype.getAnimations ??= () => [];
import { sessionTimestamp } from './session-card-data';
// Not mocked: the lane partition IS what the mode tests assert, so they drive
// the real store. It is a module singleton, hence the reset in beforeEach.
import { appMode } from '$lib/stores/sessionMode.svelte';
// Also unmocked: the workspace nav's shown/hidden split IS what those tests
// assert. Module-level state, hence the reset in beforeEach.
import { sidebarWorkspace } from '$lib/stores/sidebar-prefs.svelte';
import { terminals } from '$lib/stores/terminals.svelte';
import { TerminalApiError } from '$lib/api/terminals';
import Sidebar from './Sidebar.svelte';
import { layout } from '$lib/stores/layout.svelte';
import { viewPanel } from '$lib/stores/view-panel.svelte';
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

describe('Archived sessions', () => {
	it('has no separate archive lane for legacy metadata', () => {
		mocks.sessions.set([session('history', 'Archived work', null, false, false, { status: 'archived' })]);
		render(Sidebar, sidebarProps);
		expect(screen.queryByText('Archived work')).toBeTruthy();
		expect(screen.queryByRole('button', { name: /^Archived$/ })).toBeNull();
	});
});

describe('A new chat changing the order of the project groups (#1307)', () => {
	const row = (id: string, title: string, project: string | null, minute: number) =>
		session(id, title, project, true, false, { updated_at: `2026-08-20T12:${minute}:00Z` });
	const projectChat = row('project', 'Project chat', 'visible-project', 20);
	const looseChat = row('loose', 'Loose chat', null, 10);
	const newLooseChat = row('new', 'New loose chat', null, 30);

	/** Lets the groups have a place on the page, and records what asks to move. */
	function watchMotion(reducedMotion = false, listBottom?: number) {
		const proto = Element.prototype as unknown as { animate?: unknown; };
		const moved: { el: Element; frames: Keyframe[] }[] = [];
		proto.animate = vi.fn(function (this: Element, frames: Keyframe[]) {
			if (frames.length) moved.push({ el: this, frames });
			const animation = { cancel() {}, finish() {}, onfinish: null as null | (() => void) };
			queueMicrotask(() => animation.onfinish?.());
			return animation;
		});
		const rects = vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
			if (listBottom !== undefined && this.classList.contains('list')) return new DOMRect(0, 0, 240, listBottom);
			const place = Array.from(this.parentElement?.children ?? []).indexOf(this);
			return new DOMRect(0, place * 400, 240, 400);
		});
		vi.stubGlobal('matchMedia', () => ({ matches: reducedMotion }));
		onTestFinished(() => {
			delete proto.animate;
			rects.mockRestore();
			vi.unstubAllGlobals();
		});
		return moved;
	}

	it('keeps the rows on the page when a second project joins the lane', async () => {
		mocks.sessions.set([projectChat]);
		render(Sidebar, sidebarProps);
		const before = cardFrame('Project chat');

		mocks.sessions.set([newLooseChat, projectChat]);
		await tick();

		expect(cardFrame('Project chat')).toBe(before);
		expect(cardIds()).toEqual(['new', 'project']);
	});

	it('slides the groups a new chat reorders instead of jumping them', async () => {
		const moved = watchMotion();
		mocks.sessions.set([projectChat, looseChat]);
		render(Sidebar, sidebarProps);
		expect(cardIds()).toEqual(['project', 'loose']);

		mocks.sessions.set([newLooseChat, projectChat, looseChat]);
		await tick();
		// Svelte first holds each group where it was, then plays the glide.
		const glides = () => moved.filter(({ frames }) => frames.length > 2);
		await vi.waitFor(() => expect(glides().length).toBe(2));

		expect(cardIds()).toEqual(['new', 'loose', 'project']);
		for (const { el, frames } of glides()) {
			expect(el.classList.contains('proj-group')).toBe(true);
			expect(String(frames[0].transform)).toMatch(/translateY\(-?[1-9]/);
			expect(String(frames.at(-1)?.transform)).toBe('translateY(0px)');
		}
	});

	it('stops a far group at the bottom of the list so the panel never empties', async () => {
		const moved = watchMotion(false, 300);
		mocks.sessions.set([projectChat, looseChat]);
		render(Sidebar, sidebarProps);

		mocks.sessions.set([newLooseChat, projectChat, looseChat]);
		await tick();
		const glides = () => moved.filter(({ frames }) => frames.length > 2);
		await vi.waitFor(() => expect(glides().length).toBe(2));

		const travel = glides().map(({ frames }) => [frames[0].transform, frames.at(-1)?.transform]);
		// Rising from 400 to 0: starts at the edge, not at its true place below it.
		expect(travel).toContainEqual(['translateY(300px)', 'translateY(0px)']);
		// Sinking from 0 to 400: starts where it was and leaves past the edge.
		expect(travel).toContainEqual(['translateY(-400px)', 'translateY(-100px)']);
	});

	it('puts the groups straight in place for reduced motion', async () => {
		const moved = watchMotion(true);
		mocks.sessions.set([projectChat, looseChat]);
		render(Sidebar, sidebarProps);

		mocks.sessions.set([newLooseChat, projectChat, looseChat]);
		await tick();

		expect(cardIds()).toEqual(['new', 'loose', 'project']);
		expect(moved).toEqual([]);
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
	layout.setSidebarOverlay(false);
	mocks.activeView.set(null);
	localStorage.clear();
	mocks.adapters.get('sidebar_collapsed_groups')!.apply({ settled: false });
	mocks.save.mockClear();
	appMode.set('code');
	sidebarWorkspace.reset();
	terminals.reset();
	terminalTargets.mockReset().mockRejectedValue(new TerminalApiError('Only the owner can use terminals.', 403, 'http_403'));
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
	mocks.projectCatalogReady.set(true);
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
});

// ── Filter sheet ────────────────────────────────────────────────────────────

async function openFilter(): Promise<HTMLElement> {
	await fireEvent.click(screen.getByRole('button', { name: 'Filter' }));
	return screen.getByRole('dialog', { name: 'Filter chats' });
}

function filterOption(label: string): HTMLElement {
	const sheet = screen.getByRole('dialog', { name: 'Filter chats' });
	return within(sheet).getByRole('button', { name: label });
}

function filterLabels(): string[] {
	const sheet = screen.getByRole('dialog', { name: 'Filter chats' });
	return within(sheet).getAllByRole('button').map((node) => node.textContent?.trim() ?? '');
}

/** Section headers, whitespace-normalized: "Active 1". */
function sectLabels(): string[] {
	return Array.from(document.querySelectorAll('.sect')).map((node) =>
		node.textContent?.replace(/\s+/g, ' ').trim() ?? ''
	);
}

describe('Sidebar session groups', () => {
 it('defaults Settled to collapsed without writing over an explicit saved choice', async () => {
  mocks.adapters.get('sidebar_collapsed_groups')!.apply(undefined);
  const { container } = render(Sidebar);
  const header = () => Array.from(container.querySelectorAll<HTMLButtonElement>('.sect')).find(button => button.textContent?.includes('Settled'))!;
  expect(header().getAttribute('aria-expanded')).toBe('false');
  expect(screen.queryByText('Settled chat')).toBeNull();
  expect(mocks.save.mock.calls.some(([key]) => key === 'sidebar_collapsed_groups')).toBe(false);
  mocks.adapters.get('sidebar_collapsed_groups')!.apply({ settled: false });
  await tick();
  expect(header().getAttribute('aria-expanded')).toBe('true');
  expect(screen.getByText('Settled chat')).toBeTruthy();
 });

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

	// #804: before the project catalog answers, an empty catalog proves no
	// project was deleted. Hiding project chats until it landed made them pop
	// in above the first paint's rows on a full load, shifting every card.
	it('lists project chats in their final order before the project catalog answers', async () => {
		mocks.projectCatalogReady.set(false);
		mocks.projects.set([]);
		mocks.sessions.set([
			session('newer', 'Project chat', 'late-project', true),
			session('older', 'Loose chat', null, true),
		]);

		render(Sidebar, { props: sidebarProps });

		const titles = () => [...document.querySelectorAll('.scard-frame')].map((el) => el.textContent ?? '');
		const before = titles();
		expect(before.findIndex((t) => t.includes('Project chat'))).toBe(0);
		expect(before.findIndex((t) => t.includes('Loose chat'))).toBe(1);

		mocks.projects.set([{ id: 'late-project', name: 'Late project' }]);
		mocks.projectCatalogReady.set(true);
		await tick();
		expect(titles()).toEqual(before);
	});

	// A project swapping places moves a screenful of rows, so its newest chat
	// leaving a lane, for good or until the server refuses it, must not do that.
	it('keeps a project in its place when its newest chat leaves the lane', async () => {
		const at = (minutes: number) => ({ updated_at: new Date(Date.now() - minutes * 60_000).toISOString() });
		const visible = (id: string, title: string, isActive: boolean, minutes: number, extra = {}) =>
			session(id, title, 'visible-project', isActive, false, { ...at(minutes), ...extra });
		const other = (id: string, title: string, minutes: number) =>
			session(id, title, 'other-project', true, false, { ...at(minutes), project_name: 'Other Project' });
		mocks.projects.set([
			{ id: 'visible-project', name: 'Visible Project' },
			{ id: 'other-project', name: 'Other Project' },
		]);
		const before = [visible('newest', 'Newest', true, 1), other('middle', 'Middle', 2), visible('oldest', 'Oldest', true, 3)];
		mocks.sessions.set(before);
		render(Sidebar, { props: sidebarProps });
		const activeTitles = () =>
			[...document.querySelectorAll('[data-group="active"] .scard-frame .title')].map((el) => el.textContent);
		expect(activeTitles()).toEqual(['Newest', 'Oldest', 'Middle']);

		// Settled: the project keeps its place, though Middle is now newer than
		// anything of that project left in this lane.
		mocks.sessions.set([visible('newest', 'Newest', false, 1), before[1], before[2]]);
		await tick();
		expect(activeTitles()).toEqual(['Oldest', 'Middle']);

		// Archived, until the server has answered: the row is still in the list.
		mocks.sessions.set([visible('newest', 'Newest', false, 1, { status: 'archived' }), before[1], before[2]]);
		await tick();
		expect(activeTitles()).toEqual(['Oldest', 'Middle']);

		// Refused: it comes back to where it was, and nothing else moved.
		mocks.sessions.set(before);
		await tick();
		expect(activeTitles()).toEqual(['Newest', 'Oldest', 'Middle']);
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
		// Visible Project holds the newest chat, so it comes first in every lane.
		expect(cardIds()).toEqual(['active', 'peer', 'other']);
		expect(document.querySelector('.filter-btn.set')).toBeNull();
	});

	it('closes the filter sheet on Escape and on the scrim', async () => {
		render(Sidebar, { props: sidebarProps });

		await openFilter();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByRole('dialog', { name: 'Filter chats' })).toBeNull();

		await openFilter();
		await fireEvent.click(document.querySelector('.sheet-scrim.open') as HTMLElement);
		expect(screen.queryByRole('dialog', { name: 'Filter chats' })).toBeNull();
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

	it('lifts an active row waiting on a prompt out of Active, and back when answered', async () => {
		mocks.backgroundPermissions.set(new Map([['active', { count: 1, kind: 'permission' }]]));
		render(Sidebar, { props: sidebarProps });
		expect(labels()).toEqual(['Needs you 1', 'Active 0', 'Settled 1']);
		expect(lane()?.textContent).toContain('Active chat');

		mocks.backgroundPermissions.set(new Map());
		await tick();
		expect(labels()).toEqual(['Active 1', 'Settled 1']);
		expect(lane()).toBeNull();
	});

	it('leaves rows carrying an old suggested-task count where they are, unmarked', async () => {
		mocks.sessions.set([
			session('active', 'Active chat', 'visible-project', true, false, { pending_task_chips: 2 }),
			session('settled', 'Settled chat', 'visible-project', false, false, { pending_task_chips: 1 }),
			session('remote', 'Remote chat', 'visible-project', true, false, {
				is_remote: true, instance_id: 'laptop', device_name: 'Laptop', remote_runtime_state: 'idle', pending_task_chips: 1
			})
		]);
		render(Sidebar, { props: sidebarProps });
		await tick();
		expect(labels()).toEqual(['Active 2', 'Settled 1']);
		expect(lane()).toBeNull();
		expect(document.body.textContent).not.toMatch(/tasks? suggested/i);
		expect(document.querySelector('.scard-frame .warn')).toBeNull();
	});

	// #2324: settling is the user saying "not now" — a settled row never sits
	// under Needs you, even while the server still reports a wait.
	it('keeps a settled row that still reports a wait under Settled', () => {
		mocks.backgroundPermissions.set(new Map([['settled', { count: 1, kind: 'question' }]]));
		render(Sidebar, { props: sidebarProps });
		expect(labels()).toEqual(['Active 1', 'Settled 1']);
		expect(lane()).toBeNull();
		expect(document.querySelector('[data-group="settled"]')?.textContent).toContain('Settled chat');
	});

	it('moves a waiting row from Needs you to Settled the moment it is settled', async () => {
		mocks.backgroundPermissions.set(new Map([['active', { count: 1, kind: 'permission' }]]));
		render(Sidebar, { props: sidebarProps });
		expect(labels()).toEqual(['Needs you 1', 'Active 0', 'Settled 1']);

		// The optimistic settle flips is_active before the attention entry clears.
		mocks.sessions.set((mocks.sessions.current as { id: string }[]).map((r) => (r.id === 'active' ? { ...r, is_active: false } : r)));
		await tick();
		expect(labels()).toEqual(['Active 0', 'Settled 2']);
		expect(lane()).toBeNull();
		expect(document.querySelector('[data-group="settled"]')?.textContent).toContain('Active chat');
	});

	it('keeps a settled mirror whose owner still reports a question under Settled', () => {
		mocks.sessions.set([
			session('remote', 'Remote chat', 'visible-project', false, false, { is_remote: true, instance_id: 'laptop', device_name: 'Laptop', remote_runtime_state: 'question' }),
		]);
		render(Sidebar, { props: sidebarProps });
		expect(lane()).toBeNull();
		expect(document.querySelector('[data-group="settled"]')?.textContent).toContain('Remote chat');
	});

	// Explicit prompts can block a running turn; prose only asks for attention
	// after the turn ends (#155), until answered or dismissed.
	it('keeps prose behind Working, then lifts an idle question until answered', async () => {
		type OpenState = Parameters<typeof mocks.activeSession.set>[0] & Record<string, unknown>;
		const set = (state: Record<string, unknown>) => mocks.activeSession.set({
			sessionId: 'active', isStreaming: true, optimisticStreaming: true, activeTasks: [], ...state
		} as OpenState);
		const asked = [
			{ id: 'u1', role: 'user', content: 'Deploy it' },
			{ id: 'a1', role: 'assistant', type: 'text', content: 'Staging too, or prod only?' }
		];
		render(Sidebar, { props: sidebarProps });
		set({ messages: asked, pendingPermissions: [], pendingQuestions: [] });
		await tick();
		expect(labels()).toEqual(['Active 1', 'Settled 1']);
		expect(cardFrame('Active chat').textContent).toContain('Working');
		expect(cardFrame('Active chat').textContent).not.toContain('Awaiting input');

		set({ messages: asked, pendingPermissions: [], pendingQuestions: [], isStreaming: false, optimisticStreaming: false });
		await tick();
		expect(labels()).toEqual(['Needs you 1', 'Active 0', 'Settled 1']);
		expect(cardFrame('Active chat').textContent).toContain('Awaiting input');

		set({ messages: asked, pendingPermissions: [], pendingQuestions: [{ request_id: 'q1' }] });
		await tick();
		expect(cardFrame('Active chat').textContent).toContain('Has question');

		set({ messages: [...asked, { id: 'u2', role: 'user', content: 'Prod only' }], pendingPermissions: [], pendingQuestions: [] });
		await tick();
		expect(labels()).toEqual(['Active 1', 'Settled 1']);
		expect(cardFrame('Active chat').textContent).toContain('Working');
	});

	// Spec §5: a question this tab saw before leaving a session keeps that row
	// in Needs you; working-time changes on its row never clear it.
	it('keeps a left session with an unanswered prose question in the lane through row updates', async () => {
		const asked = [
			{ id: 'u1', role: 'user' as const, content: 'Deploy it' },
			{ id: 'a1', role: 'assistant' as const, type: 'text' as const, content: 'Staging too, or prod only?' }
		];
		rememberLeavingProseQuestion('active', asked);
		onTestFinished(_resetProseQuestions);
		render(Sidebar, { props: sidebarProps });
		expect(labels()).toEqual(['Needs you 1', 'Active 0', 'Settled 1']);
		expect(cardFrame('Active chat').textContent).toContain('Awaiting input');

		mocks.sessions.set([
			session('active', 'Active chat', 'visible-project', true, false, { work_started_at: '2026-10-11T00:00:00Z' }),
			session('settled', 'Settled chat', 'visible-project', false),
			session('worker', 'Hidden worker', 'hidden-project', true, true),
		]);
		await tick();
		expect(labels()).toEqual(['Needs you 1', 'Active 0', 'Settled 1']);
		// Reopened here with the answer loaded, then left: the switcher drops it.
		rememberLeavingProseQuestion('active', [...asked, { id: 'u2', role: 'user' as const, content: 'Prod only' }]);
		await tick();
		expect(labels()).toEqual(['Active 1', 'Settled 1']);
	});

	// #2249: an answer sent from another device clears the lane without
	// reopening the session here.
	it('moves a left session out of Needs you when a later user message arrives', async () => {
		const asked = [
			{ id: 'u1', role: 'user' as const, content: 'Deploy it', seq: 4, epoch: 'evt-1' },
			{ id: 'a1', role: 'assistant' as const, type: 'text' as const, content: 'Staging too, or prod only?', seq: 5, epoch: 'evt-1' }
		];
		const rev = (seq: number) => ({ last_user_message_seq: seq, last_user_message_epoch: 'evt-1' });
		rememberLeavingProseQuestion('active', asked, rev(4));
		onTestFinished(_resetProseQuestions);
		render(Sidebar, { props: sidebarProps });
		expect(labels()).toEqual(['Needs you 1', 'Active 0', 'Settled 1']);

		observeLastUserMessage('active', rev(5));
		await tick();
		expect(labels()).toEqual(['Needs you 1', 'Active 0', 'Settled 1']);

		observeLastUserMessage('active', rev(6));
		await tick();
		expect(labels()).toEqual(['Active 1', 'Settled 1']);
	});

	it('marks a never-opened session from its row question and clears it on the answer (#2263)', async () => {
		onTestFinished(_resetProseQuestions);
		render(Sidebar, { props: sidebarProps });
		expect(labels()).toEqual(['Active 1', 'Settled 1']);

		const pair = { last_assistant_question_seq: 12, last_assistant_question_epoch: 'evt-1' };
		observeLastUserMessage('active', { ...pair, last_user_message_seq: 9, last_user_message_epoch: 'evt-1' });
		await tick();
		expect(labels()).toEqual(['Needs you 1', 'Active 0', 'Settled 1']);

		observeLastUserMessage('active', { last_user_message_seq: 13, last_user_message_epoch: 'evt-1' });
		await tick();
		expect(labels()).toEqual(['Active 1', 'Settled 1']);

		// A question position from another log is never compared: unmarked.
		observeLastUserMessage('active', { ...pair, last_assistant_question_epoch: 'evt-2' });
		await tick();
		expect(labels()).toEqual(['Active 1', 'Settled 1']);
	});

	// #2324: a settled session's prose question keeps it in Settled.
	it('keeps a settled session with a remembered prose question under Settled', () => {
		rememberLeavingProseQuestion('settled', [
			{ id: 'u1', role: 'user' as const, content: 'Deploy it' },
			{ id: 'a1', role: 'assistant' as const, type: 'text' as const, content: 'Staging too, or prod only?' }
		]);
		onTestFinished(_resetProseQuestions);
		render(Sidebar, { props: sidebarProps });
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

	it('starts selecting on a long press, then settles the picked rows without an Undo banner', async () => {
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
			expect(mocks.banner).not.toHaveBeenCalled();
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

			expect(within(selbar()).queryByRole('button', { name: 'Archive' })).toBeNull();
			await fireEvent.click(within(selbar()).getByRole('button', { name: /Settle/ }));
			await vi.advanceTimersByTimeAsync(0);
			expect(mocks.setSessionActive).toHaveBeenCalledWith('active', false);
			expect(mocks.setSessionArchived).not.toHaveBeenCalled();
			expect(mocks.banner).not.toHaveBeenCalled();
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
	it('offers Terminal in More to the owner once the worker can run shells', async () => {
		terminalTargets.mockResolvedValue({
			worker: { id: 'w', name: 'ServerWorker', platform: 'linux' }, supported: true, unsupported_reason: null, targets: [],
			shells: [{ name: 'bash', default: true }], default_shell: 'bash',
			default_folder: { path: '/workspace', source: 'workspace' }, limit: 8, open: 0,
		});
		const onOpenFeature = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true, onOpenFeature } });
		await terminals.ensureCapability();
		await tick();

		// The worker-wide probe: no chat id.
		expect(terminalTargets).toHaveBeenCalledWith(null);
		const items = await openMore();
		expect(items.map((n) => itemLabel(n))).toContain('Terminal');
		await fireEvent.click(menuItem('Terminal'));
		expect(onOpenFeature).toHaveBeenCalledWith('terminal');
	});

	it('hides Terminal when the worker refuses terminals to this viewer', async () => {
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true } });
		await terminals.ensureCapability();
		await tick();

		expect(terminals.capability.status).toBe('denied');
		const items = await openMore();
		expect(items.map((n) => itemLabel(n))).not.toContain('Terminal');
	});

	it('never probes or offers Terminal to a non-admin', async () => {
		render(Sidebar, { props: sidebarProps });
		await tick();

		expect(terminalTargets).not.toHaveBeenCalled();
		const items = await openMore();
		expect(items.map((n) => itemLabel(n))).not.toContain('Terminal');
	});

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
	it('starts a new session from the rail, one kind whatever lane was last shown', async () => {
		const onNewSession = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, onNewSession } });

		await fireEvent.click(railItem('New chat'));
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

	it('names the list instead of offering a lane switcher (#371 step 4)', () => {
		render(Sidebar, { props: sidebarProps });

		const head = document.querySelector('.p-head');
		// One kind of session: the project is picked on the new-session screen.
		expect(head?.querySelector('[role="radiogroup"]')).toBeNull();
		expect(screen.queryAllByRole('radio')).toHaveLength(0);
		expect(head?.textContent).toContain('Chats');
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
	it('says one thing when there is nothing at all, with no empty Settled lane', () => {
		mocks.sessions.set([]);
		appMode.set('voice');
		render(Sidebar, { props: sidebarProps });
		const empties = Array.from(document.querySelectorAll('.empty')).map((n) => n.textContent?.trim());
		expect(empties).toEqual(['No chats yet']);
		expect(Array.from(document.querySelectorAll('.sect')).some((n) => n.textContent?.includes('Settled'))).toBe(false);
	});
	it('keeps an explicit project scope across lanes', async () => {
		mocks.sessions.set(fixture());
		render(Sidebar, { props: sidebarProps });
		await openFilter();
		await fireEvent.click(filterOption('Visible Project'));
		appMode.set('chat');
		await tick();
		expect(cardIds()).toEqual(['legacy', 'coded']);
		await fireEvent.click(screen.getByRole('button', { name: 'Clear project filter Visible Project' }));
		expect(cardIds()).toEqual(['legacy', 'coded', 'chatty', 'spoken']);
	});
});

describe('Sidebar page-panel host', () => {
	it('keeps the single notification bell and offered update reachable on Notes', async () => {
		mocks.activeView.set({ type: 'notes' });
		mocks.apiGet.mockResolvedValue({ allow_updates: true, update_all_workers: false, workers: [{
			device_id: 'local', device_name: 'Local', is_self: true, phase: 'available',
			supported: true, online: true, allow_updates: true, version: '2.0.0', running_version: '1.0.0'
		}] });
		render(Sidebar, { props: { ...sidebarProps, panelHost: true } });
		await tick();
		const rail = screen.getByRole('navigation', { name: 'Workspace' });
		for (const name of [/Notifications/, /Download update/]) {
			const button = await screen.findByRole('button', { name });
			expect(rail.contains(button)).toBe(true);
			expect(button.closest('[inert], [aria-hidden="true"]')).toBeNull();
		}
		expect(document.querySelectorAll('.notif-center')).toHaveLength(1);
		expect(document.querySelectorAll('.update-slot')).toHaveLength(1);
		await fireEvent.click(screen.getByRole('button', { name: /Notifications/ }));
		expect(screen.getByRole('dialog', { name: 'Notifications' }).closest('[inert], [aria-hidden="true"]')).toBeNull();
	});
	it('keeps phone drawer controls in the Chats header, not the bottom tabs', async () => {
		platform.set('mobile');
		mocks.activeView.set({ type: 'notes' });
		render(Sidebar, { props: { ...sidebarProps, panelHost: true } });
		await tick();
		const bell = screen.getByRole('button', { name: /Notifications/ });
		expect(bell.closest('.p-head')).not.toBeNull();
		expect(bell.closest('[inert], [aria-hidden="true"]')).toBeNull();
		expect(screen.getByRole('navigation', { name: 'Workspace' }).contains(bell)).toBe(false);
		expect(document.querySelectorAll('.notif-center')).toHaveLength(1);
	});
	it.each(['prs', 'settings', 'build', 'stats', 'docs', 'terminal'])('keeps the rail menu and Customize outside hidden Chats on %s', async (type) => {
		mocks.activeView.set({ type });
		terminalTargets.mockResolvedValue({
			worker: { id: 'w', name: 'ServerWorker', platform: 'linux' }, supported: true, unsupported_reason: null, targets: [],
			shells: [{ name: 'bash', default: true }], default_shell: 'bash',
			default_folder: { path: '/workspace', source: 'workspace' }, limit: 8, open: 0,
		});
		const onOpenFeature = vi.fn();
		const view = render(Sidebar, { props: { ...sidebarProps, panelHost: true, isAdmin: true, onOpenFeature } });
		await terminals.ensureCapability();
		await tick();
		const chats = view.container.querySelector('.col.rail-hidden');
		expect(chats?.getAttribute('aria-hidden')).toBe('true');
		// Chat-specific controls still belong to Chats, not the global rail.
		expect(chats?.querySelector('.selbar')).toBeTruthy();
		expect(chats?.querySelector('[aria-label="Filter chats"]')).toBeTruthy();
		await openMore();
		const menu = screen.getByRole('menu', { name: 'More destinations' });
		expect(menu.closest('[inert], [aria-hidden="true"]')).toBeNull();
		for (const [label, destination] of [['Docs', 'docs'], ['Stats', 'stats'], ['Terminal', 'terminal']]) {
			await fireEvent.click(menuItem(label));
			expect(onOpenFeature).toHaveBeenCalledWith(destination);
			await openMore();
		}
		await fireEvent.click(menuItem('Customize the rail'));
		const dialog = screen.getByRole('dialog', { name: 'Customize the rail' });
		expect(dialog.closest('[inert], [aria-hidden="true"]')).toBeNull();
	});
	it('keeps Chats mounted and its scroll while a lazy page shows only its title', async () => {
		const view = render(Sidebar, { props: { ...sidebarProps, panelHost: true } });
		await tick();
		const ledger = view.container.querySelector('.list') as HTMLDivElement;
		ledger.scrollTop = 120;
		const card = view.container.querySelector('[data-session-id="active"]');
		mocks.activeView.set({ type: 'notes' });
		await tick();
		expect(viewPanel.hostMounted).toBe(true);
		expect(screen.getByRole('heading', { name: 'Notes' })).toBeTruthy();
		expect(view.container.querySelector('.page-panel [aria-busy="true"]')).toBeTruthy();
		expect(ledger.closest('.rail-hidden')).toBeTruthy();
		expect(ledger.scrollTop).toBe(120);
		mocks.activeView.set({ type: 'session' });
		await tick();
		expect(view.container.querySelector('.list')).toBe(ledger);
		expect(view.container.querySelector('[data-session-id="active"]')).toBe(card);
		expect(ledger.closest('.rail-hidden')).toBeNull();
		expect(ledger.scrollTop).toBe(120);
		view.unmount();
		expect(viewPanel.hostMounted).toBe(false);
	});
	it.each([false, true])('does not register or consume claims in the phone drawer (host=%s)', async (panelHost) => {
		platform.set('mobile');
		mocks.activeView.set({ type: 'settings' });
		const view = render(Sidebar, { props: { ...sidebarProps, panelHost } });
		await tick();
		expect(viewPanel.hostMounted).toBe(false);
		expect(view.container.querySelector('.page-panel')).toBeNull();
		expect(view.container.querySelector('.list')?.closest('.rail-hidden')).toBeNull();
	});
});

describe('Sidebar without the retired assistant (#371 step 5)', () => {
	it('shows no pinned assistant row, and the hidden old home stays out of every lane', async () => {
		mocks.sessions.set([
			session('home', 'Assistant home chat', null, true, true),
			session('active', 'Active chat', 'visible-project', true),
		]);
		const onOpenFeature = vi.fn();
		render(Sidebar, { props: { ...sidebarProps, isAdmin: true, onOpenFeature } });
		await tick();
		expect(screen.queryByRole('button', { name: 'Open your assistant' })).toBeNull();
		expect(document.body.textContent).not.toMatch(/Your ongoing conversation|Set up your assistant/);
		expect(cardIds()).toEqual(['active']);
		expect(onOpenFeature).not.toHaveBeenCalledWith('assistant');
	});
});


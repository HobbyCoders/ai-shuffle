import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { platform } from '$lib/stores/platform.svelte';

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
		activeSession: store({ sessionId: null as string | null }),
		setSessionActive: vi.fn(),
		deleteSession: vi.fn(),
		renameSession: vi.fn(),
		loadSessions: vi.fn(),
		forkSession: vi.fn(),
		newChat: vi.fn(),
		setTitle: vi.fn(),
		toastPush: vi.fn(),
		gitCurrent: {
			sessionId: null as string | null,
			projectId: null as string | null,
			worktreeId: null as string | null,
			status: null as Record<string, unknown> | null,
			diffStats: null as Record<string, unknown> | null,
		},
		watch: vi.fn(() => () => {}),
		getSessionGit: vi.fn(() => null as Record<string, unknown> | null),
	};
});

vi.mock('$lib/stores/app-data.svelte', () => ({
	appData: {
		setSessionActive: mocks.setSessionActive,
		deleteSession: mocks.deleteSession,
		renameSession: mocks.renameSession,
		loadSessions: mocks.loadSessions,
	},
}));

vi.mock('$lib/stores/active-session', () => ({
	activeSession: Object.assign(mocks.activeSession, {
		newChat: mocks.newChat,
		setTitle: mocks.setTitle,
	}),
}));

vi.mock('$lib/api/client', () => ({
	forkSession: mocks.forkSession,
}));

vi.mock('$lib/stores/notifications', () => ({
	notify: mocks.toastPush,
}));

// The card reads the real git store only for the session open in the main
// panel, and leases a poll from the sidebar's own store for every other active
// card. Both are stubbed so a row renders without touching the network.
vi.mock('$lib/stores/git.svelte', () => ({
	git: {
		get current() {
			return mocks.gitCurrent;
		},
	},
}));

vi.mock('$lib/stores/session-git-state.svelte', () => ({
	sessionGitState: {
		watch: mocks.watch,
		get: mocks.getSessionGit,
	},
}));

import SessionCard from './SessionCard.svelte';

/** A canonical `GitStatus`, trimmed to what the card actually reads. */
function gitStatus(over: Record<string, unknown> = {}) {
	return {
		is_git_repo: true,
		has_changes: false,
		ahead: 0,
		unpushed: null as number | null,
		default_branch: 'main',
		...over,
	};
}

function session(overrides: Record<string, unknown> = {}) {
	return {
		id: 'session-a',
		profile_id: 'default',
		project_id: 'project-a',
		title: 'Sidebar work',
		status: 'active',
		is_active: true,
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
		updated_at: '2026-08-20T12:05:00Z',
		is_remote: false,
		...overrides,
	};
}

function renderCard(overrides: Record<string, unknown> = {}, propOverrides: Record<string, unknown> = {}) {
	const onOpen = vi.fn();
	const onSettled = vi.fn();
	const row = session(overrides);
	const result = render(SessionCard, {
		props: {
			session: row as never,
			status: row.is_active ? 'done' : 'idle',
			projectLabel: 'Project A',
			active: false,
			now: Date.parse('2026-08-20T12:06:00Z'),
			onOpen,
			onSettled,
			...propOverrides,
		},
	});
	return { onOpen, onSettled, ...result };
}

beforeEach(() => {
	vi.clearAllMocks();
	Object.defineProperty(window, 'matchMedia', {
		writable: true,
		value: vi.fn().mockImplementation(() => ({
			matches: false,
			addEventListener: vi.fn(),
			removeEventListener: vi.fn(),
		})),
	});
	mocks.activeSession.set({ sessionId: null });
	mocks.gitCurrent.sessionId = null;
	mocks.gitCurrent.projectId = null;
	mocks.gitCurrent.worktreeId = null;
	mocks.gitCurrent.status = null;
	mocks.gitCurrent.diffStats = null;
	mocks.watch.mockReturnValue(() => {});
	mocks.getSessionGit.mockReturnValue(null);
	mocks.setSessionActive.mockResolvedValue(undefined);
	mocks.deleteSession.mockResolvedValue(undefined);
	mocks.renameSession.mockResolvedValue(undefined);
	mocks.loadSessions.mockResolvedValue(undefined);
	mocks.forkSession.mockResolvedValue({ session_id: 'forked-session' });
});

afterEach(() => {
	cleanup();
	platform.set('desktop');
});

describe('SessionCard actions', () => {
	it('keeps settle and omits rename, fork, and delete controls', () => {
		renderCard();

		expect(screen.getByTitle('Settle session')).toBeTruthy();
		expect(screen.queryByTitle('Rename session')).toBeNull();
		expect(screen.queryByTitle('Fork session')).toBeNull();
		expect(screen.queryByTitle('Delete session')).toBeNull();
	});

	it('recedes a working row that is not the open session', () => {
		const { container } = renderCard({ is_active: true }, { status: 'working' });

		expect(container.querySelector('.scard.receded')).toBeTruthy();
	});

	it('keeps an awaiting row bright (not receded)', () => {
		const { container } = renderCard({ is_active: true }, { status: 'await' });

		expect(container.querySelector('.scard.receded')).toBeNull();
	});

	it('renders a settled row as two compact lines, keeping its action', () => {
		const { container } = renderCard(
			{ is_active: false, worktree_branch: 'fix/thing' },
			{ compact: true }
		);

		expect(container.querySelector('.scard.compact')).toBeTruthy();
		expect(container.querySelector('.title')?.textContent).toContain('Sidebar work');
		// A settled row drops the status line; its meta reads device icon · git · branch
		// (the project is the group it already sits under).
		expect(container.querySelector('.l2')).toBeNull();
		const meta = container.querySelector('.l3');
		expect(meta).toBeTruthy();
		// Device shows as leading icon only — label stays on title/aria for hover + a11y.
		const deviceSeg = meta?.querySelector('.device-seg');
		expect(meta?.firstElementChild).toBe(deviceSeg);
		expect(deviceSeg?.getAttribute('aria-label')).toBe('This device');
		expect(meta?.textContent).not.toContain('This device');
		expect(meta?.textContent).toContain('fix/thing');
		expect(meta?.textContent).not.toContain('Project A');
		expect(screen.getByTitle('Move to Active')).toBeTruthy();
	});

	it('settles an active session without opening it', async () => {
		const { onOpen } = renderCard();

		await fireEvent.click(screen.getByTitle('Settle session'));

		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', false);
		expect(onOpen).not.toHaveBeenCalled();
	});

	it('fires onSettled when the OPEN session is settled', async () => {
		const { onSettled } = renderCard({ is_active: true }, { active: true });

		await fireEvent.click(screen.getByTitle('Settle session'));

		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', false);
		expect(onSettled).toHaveBeenCalledOnce();
	});

	it("doesn't fire onSettled when a background session is settled", async () => {
		const { onSettled } = renderCard({ is_active: true }, { active: false });

		await fireEvent.click(screen.getByTitle('Settle session'));

		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', false);
		expect(onSettled).not.toHaveBeenCalled();
	});

	it("doesn't fire onSettled when moving back to Active", async () => {
		const { onSettled } = renderCard({ is_active: false }, { active: true });

		await fireEvent.click(screen.getByTitle('Move to Active'));

		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', true);
		expect(onSettled).not.toHaveBeenCalled();
	});

	it('moves a settled session back to Active', async () => {
		renderCard({ is_active: false });

		await fireEvent.click(screen.getByTitle('Move to Active'));

		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', true);
	});

	// ── R15: no per-card global listeners, no per-tick date parsing ──────
	it('attaches no window click/keydown listeners of its own', () => {
		const addSpy = vi.spyOn(window, 'addEventListener');

		renderCard();

		const globals = addSpy.mock.calls
			.map(([type]) => type)
			.filter((type) => type === 'click' || type === 'keydown');
		expect(globals).toEqual([]);
		addSpy.mockRestore();
	});

	it('uses the timestamp handed down by the list instead of re-parsing the row', () => {
		// The sidebar resolves each row's last-activity time once when it builds
		// the list; the card must not parse `updated_at` again on every tick.
		const { container } = renderCard(
			{ updated_at: 'not-a-date' },
			{ timestamp: Date.parse('2026-08-20T12:05:00Z'), status: 'working' }
		);

		expect(container.querySelector('.timer')?.textContent).toBe('1m 00s');
	});

	it('follows the parent when the open-card id is controlled', async () => {
		const { container, rerender } = renderCard({}, { openActionsId: null });
		const frame = container.querySelector('.scard-frame') as HTMLElement;
		expect(frame.classList.contains('actions-open')).toBe(false);
		expect(frame.getAttribute('data-session-card')).toBe('session-a');

		await rerender({ openActionsId: 'session-a' } as never);
		expect(frame.classList.contains('actions-open')).toBe(true);
	});

	it('reports its reveal to the parent instead of closing itself', async () => {
		const onActionsOpenChange = vi.fn();
		platform.set('mobile');
		const { container } = renderCard({}, { openActionsId: null, onActionsOpenChange });
		const frame = container.querySelector('.scard-frame') as HTMLElement;
		const card = container.querySelector('.scard') as HTMLElement;

		await fireEvent.pointerDown(card, { pointerId: 1, isPrimary: true, button: 0, clientX: 260, clientY: 20 });
		await fireEvent.pointerMove(frame, { pointerId: 1, isPrimary: true, clientX: 180, clientY: 22 });
		await fireEvent.pointerUp(frame, { pointerId: 1, isPrimary: true, button: 0, clientX: 180, clientY: 22 });

		expect(onActionsOpenChange).toHaveBeenCalledWith('session-a');
	});

	it('keeps mobile actions hidden until a left swipe reveals them', async () => {
		platform.set('mobile');
		const { container, onOpen } = renderCard();
		const frame = container.querySelector('.scard-frame') as HTMLElement;
		const card = container.querySelector('.scard') as HTMLElement;
		const actions = container.querySelector('.card-actions') as HTMLElement;

		expect(actions.getAttribute('aria-hidden')).toBe('true');
		expect(frame.classList.contains('actions-open')).toBe(false);

		await fireEvent.pointerDown(card, { pointerId: 1, isPrimary: true, button: 0, clientX: 260, clientY: 20 });
		await fireEvent.pointerMove(frame, { pointerId: 1, isPrimary: true, clientX: 180, clientY: 22 });
		await fireEvent.pointerUp(frame, { pointerId: 1, isPrimary: true, button: 0, clientX: 180, clientY: 22 });

		expect(frame.classList.contains('actions-open')).toBe(true);
		expect(actions.getAttribute('aria-hidden')).toBeNull();
		expect(onOpen).not.toHaveBeenCalled();
	});

	it('closes revealed mobile actions when the card is tapped', async () => {
		platform.set('mobile');
		const { container, onOpen } = renderCard();
		const frame = container.querySelector('.scard-frame') as HTMLElement;
		const card = container.querySelector('.scard') as HTMLElement;

		await fireEvent.pointerDown(card, { pointerId: 1, isPrimary: true, button: 0, clientX: 260, clientY: 20 });
		await fireEvent.pointerMove(frame, { pointerId: 1, isPrimary: true, clientX: 180, clientY: 20 });
		await fireEvent.pointerUp(frame, { pointerId: 1, isPrimary: true, button: 0, clientX: 180, clientY: 20 });
		await new Promise((resolve) => setTimeout(resolve, 0));

		// A real tap is pointerdown → pointerup → click; the pointerdown is what
		// clears the one-shot guard that swallowed the swipe's trailing click.
		await fireEvent.pointerDown(card, { pointerId: 2, isPrimary: true, button: 0, clientX: 200, clientY: 20 });
		await fireEvent.pointerUp(frame, { pointerId: 2, isPrimary: true, button: 0, clientX: 200, clientY: 20 });
		await fireEvent.click(card);

		expect(frame.classList.contains('actions-open')).toBe(false);
		expect(onOpen).not.toHaveBeenCalled();
	});
});

describe('SessionCard git state', () => {
	it('leads a merged session with the purple merge glyph', () => {
		const { container } = renderCard({ pr_state: 'merged', pr_number: 1071 });

		const lead = container.querySelector('.lead');
		expect(lead?.getAttribute('aria-label')).toBe('Pull request merged');
		expect(lead?.classList.contains('purple')).toBe(true);
		expect(container.querySelector('.l2')?.textContent).toContain('PR #1071 merged');
	});

	it('leads an open PR with the green pull-request glyph', () => {
		const { container } = renderCard({ pr_state: 'open', pr_number: 42 }, { status: 'pr' });

		const lead = container.querySelector('.lead');
		expect(lead?.getAttribute('aria-label')).toBe('Pull request open');
		expect(lead?.classList.contains('green')).toBe(true);
		expect(container.querySelector('.l2')?.textContent).toContain('PR #42 open');
	});

	it('leads a chat session with the chat glyph and no branch talk', () => {
		const { container } = renderCard({ project_id: null });

		expect(container.querySelector('.lead')?.getAttribute('aria-label')).toBe('Chat session');
		expect(container.querySelector('.l2')?.textContent).not.toContain('PR');
	});

	it('shows the amber diff glyph and the diffstat for an uncommitted tree', () => {
		mocks.getSessionGit.mockReturnValue({
			status: gitStatus({ has_changes: true }),
			diffStats: { additions: 124, deletions: 38, files_changed: 4 },
			loaded: true,
		});

		const { container } = renderCard({ worktree_branch: 'feat/x' });

		const lead = container.querySelector('.lead');
		expect(lead?.getAttribute('aria-label')).toBe('Uncommitted changes');
		expect(lead?.classList.contains('amber')).toBe(true);
		expect(container.querySelector('.diff')?.textContent).toBe('+124 −38');
	});

	it('restarts the glyph after a merged PR once the tree is dirty again', () => {
		mocks.getSessionGit.mockReturnValue({
			status: gitStatus({ has_changes: true }),
			diffStats: { additions: 12, deletions: 3, files_changed: 2 },
			loaded: true,
		});

		const { container } = renderCard({
			worktree_branch: 'feat/x',
			pr_state: 'merged',
			pr_number: 1071,
		});

		const lead = container.querySelector('.lead');
		expect(lead?.getAttribute('aria-label')).toBe('Uncommitted changes');
		expect(lead?.classList.contains('amber')).toBe(true);
		expect(container.querySelector('.l2')?.textContent).not.toContain('PR #1071 merged');
		expect(container.querySelector('.diff')?.textContent).toBe('+12 −3');
	});

	it('leads a pushed session branch with the upload glyph', () => {
		mocks.getSessionGit.mockReturnValue({
			// ahead>0 (commits vs main) + unpushed=0 (remote has them all)
			status: gitStatus({ ahead: 2, unpushed: 0 }),
			diffStats: null,
			loaded: true,
		});

		const { container } = renderCard({ worktree_branch: 'feat/x' });

		expect(container.querySelector('.lead')?.getAttribute('aria-label')).toBe('Pushed, no PR yet');
		// No uncommitted work, so no diffstat to answer for.
		expect(container.querySelector('.diff')).toBeNull();
	});

	it('does not call a brand-new worktree pushed', () => {
		// Fresh branch off main: clean tree, ahead=0, never on the remote.
		mocks.getSessionGit.mockReturnValue({
			status: gitStatus({ ahead: 0, unpushed: null }),
			diffStats: null,
			loaded: true,
		});

		const { container } = renderCard({ worktree_branch: 'feat/x' });

		expect(container.querySelector('.lead')?.getAttribute('aria-label')).toBe('No changes');
		expect(container.querySelector('.lead')?.getAttribute('aria-label')).not.toBe(
			'Pushed, no PR yet'
		);
	});

	it('lays an active card out as title, status line with project, then device meta', () => {
		const { container } = renderCard(
			{ is_active: true, worktree_branch: 'feat/x' },
			{ status: 'working', projectLabel: 'Project A' }
		);

		expect(container.querySelector('.title')?.textContent).toContain('Sidebar work');
		const statusLine = container.querySelector('.l2');
		// Project leads the middle row; runtime status follows.
		expect(statusLine?.querySelector('.proj')?.textContent).toBe('Project A');
		expect(statusLine?.firstElementChild?.querySelector('.proj')).toBeTruthy();
		expect(statusLine?.querySelector('.st')?.textContent).toBe('Working');
		expect(statusLine?.querySelector('.st')?.classList.contains('accent')).toBe(true);
		const meta = container.querySelector('.l3');
		const deviceSeg = meta?.querySelector('.device-seg');
		// Device icon leads the bottom meta line (same position as settled rows).
		expect(meta?.firstElementChild).toBe(deviceSeg);
		expect(deviceSeg?.getAttribute('aria-label')).toBe('This device');
		expect(meta?.textContent).not.toContain('This device');
		expect(meta?.textContent).not.toContain('Project A');
		expect(meta?.textContent).toContain('feat/x');
	});

	it('marks a working row with the live pip and a parked row with the warning', () => {
		const working = renderCard({ is_active: true }, { status: 'working' });
		expect(working.container.querySelector('.title .pip')).toBeTruthy();
		cleanup();

		const parked = renderCard({ is_active: true }, { status: 'needs_permission' });
		expect(parked.container.querySelector('.title .pip')).toBeNull();
		expect(parked.container.querySelector('.title .warn')).toBeTruthy();
		expect(parked.container.querySelector('.l2 .st')?.textContent).toBe('Needs permission');
	});

	it('leases one poll per active card', () => {
		renderCard({ project_id: 'project-a', worktree_id: 'wt-1' });

		expect(mocks.watch).toHaveBeenCalledWith('session-a', 'project-a', 'wt-1');
	});

	it('never spends a request on a settled row', () => {
		renderCard({ is_active: false }, { compact: true });

		expect(mocks.watch).not.toHaveBeenCalled();
	});

	it('reuses the main panel git store for the session that is open', () => {
		mocks.gitCurrent.sessionId = 'session-a';
		mocks.gitCurrent.status = gitStatus({ has_changes: true });
		mocks.gitCurrent.diffStats = { additions: 7, deletions: 1, files_changed: 1 };

		const { container } = renderCard({ worktree_branch: 'feat/x' }, { active: true });

		expect(mocks.watch).not.toHaveBeenCalled();
		expect(container.querySelector('.diff')?.textContent).toBe('+7 −1');
		expect(container.querySelector('.lead')?.getAttribute('aria-label')).toBe('Uncommitted changes');
	});
});

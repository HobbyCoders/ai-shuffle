/**
 * Behavior tests for the SessionsBoard action rail — specifically the inline
 * Resume / Fork icon buttons that replaced the old right-click context menu.
 *
 * These mount the real board with its store/api dependencies mocked, then drive
 * the rail buttons that appear on every session row. Covered:
 *  - Fork/Favorite/Clear render on every row; Resume renders ONLY on remote
 *    (cross-install) rows — a local row's own click already opens it, so a
 *    same-device "Open" button would be redundant and is hidden entirely
 *  - the Resume tooltip on a remote row reads "Resume here …" tagged with the
 *    owning device
 *  - clicking Fork forks that row's session and opens the NEW one, and does NOT
 *    open the clicked row (stopPropagation) — the row click would open its own id
 *  - clicking Resume on a remote row adopts-then-opens
 *  - the pre-existing Favorite rail action still fires
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';

// jsdom lacks ResizeObserver; some lucide/layout paths probe for it.
if (!('ResizeObserver' in globalThis)) {
	(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
		observe() {}
		unobserve() {}
		disconnect() {}
	};
}

// A tiny Svelte-store-compatible container so `$store` auto-subscription works
// inside the component under test without pulling in the real store graph.
const mocks = vi.hoisted(() => {
	function store<T>(initial: T) {
		let val = initial;
		const subs = new Set<(v: T) => void>();
		return {
			subscribe(fn: (v: T) => void) {
				fn(val);
				subs.add(fn);
				return () => subs.delete(fn);
			},
			set(v: T) {
				val = v;
				subs.forEach((f) => f(val));
			}
		};
	}
	return {
		sessions: store<unknown[]>([]),
		projects: store<unknown[]>([]),
		activeSession: store<{ sessionId: string | null; isStreaming: boolean }>({
			sessionId: null,
			isStreaming: false
		}),
		streamingSessions: store<Set<string>>(new Set()),
		backgroundPermissions: store<Set<string>>(new Set()),
		isAdmin: store<boolean>(false),
		forkSession: vi.fn(),
		adoptSession: vi.fn(),
		toggleSessionFavorite: vi.fn(),
		loadSessions: vi.fn(),
		loadProjects: vi.fn(),
		deleteSession: vi.fn(),
		toastPush: vi.fn(),
		apiPost: vi.fn(),
		apiDelete: vi.fn()
	};
});

vi.mock('$lib/stores/app-data.svelte', () => ({
	appData: {
		loadSessions: mocks.loadSessions,
		loadProjects: mocks.loadProjects,
		deleteSession: mocks.deleteSession
	},
	sessions: mocks.sessions,
	projects: mocks.projects
}));
vi.mock('$lib/stores/active-session', () => ({
	activeSession: mocks.activeSession,
	streamingSessions: mocks.streamingSessions,
	backgroundPermissions: mocks.backgroundPermissions
}));
vi.mock('$lib/api/client', () => ({
	toggleSessionFavorite: mocks.toggleSessionFavorite,
	forkSession: mocks.forkSession,
	adoptSession: mocks.adoptSession,
	api: { post: mocks.apiPost, delete: mocks.apiDelete }
}));
vi.mock('$lib/stores/auth.svelte', () => ({ isAdmin: mocks.isAdmin }));
vi.mock('$lib/stores/toast', () => ({ toast: { push: mocks.toastPush } }));

// Imported AFTER the mocks are registered.
import SessionsBoard from './SessionsBoard.svelte';

type AnySession = Record<string, unknown>;
function makeSession(over: AnySession = {}): AnySession {
	return {
		id: 'a',
		title: 'A session',
		is_favorite: false,
		is_remote: false,
		instance_id: null,
		device_name: null,
		device_online: true,
		controllable: true,
		project_id: null,
		project_name: null,
		link_group_id: null,
		automation_id: null,
		worktree_branch: null,
		total_cost_usd: 0,
		turn_count: 0,
		created_at: '2026-07-01T00:00:00Z',
		updated_at: '2026-07-08T12:00:00Z',
		...over
	};
}

// Row A: local (tooltip "Open"). Row B: remote on "Mac Studio" (tooltip "Resume here …").
const localSession = makeSession({ id: 'a', title: 'Local one', updated_at: '2026-07-08T12:00:00Z' });
const remoteSession = makeSession({
	id: 'b',
	title: 'Remote one',
	is_remote: true,
	instance_id: 'inst-2',
	device_name: 'Mac Studio',
	device_online: true,
	updated_at: '2026-07-08T11:00:00Z'
});

function renderBoard() {
	const onOpenSession = vi.fn();
	const onNewSession = vi.fn();
	const result = render(SessionsBoard, { props: { onOpenSession, onNewSession } });
	return { onOpenSession, onNewSession, unmount: result.unmount };
}

/** The `.row` whose title matches (scoped to the row list — the same title
 * also appears in a project card's "latest" line, so a global text query is
 * ambiguous). */
function rowFor(title: string): HTMLElement {
	const rows = Array.from(document.querySelectorAll('.row')) as HTMLElement[];
	const el = rows.find((r) => r.querySelector('.row-title')?.textContent?.trim() === title);
	if (!el) throw new Error(`no row for ${title}`);
	return el;
}

beforeEach(() => {
	vi.clearAllMocks();
	mocks.forkSession.mockResolvedValue({ session_id: 'forked-a' });
	mocks.adoptSession.mockResolvedValue({ session_id: 'b', adopted: true });
	mocks.toggleSessionFavorite.mockResolvedValue({});
	mocks.loadSessions.mockResolvedValue(undefined);
	mocks.sessions.set([localSession, remoteSession]);
});

afterEach(() => cleanup());

describe('SessionsBoard action rail', () => {
	it('renders Fork/Favorite/Clear on every row, next to each other', () => {
		renderBoard();
		// One Fork + one Clear + one Favorite per row (two rows).
		expect(screen.getAllByTitle('Fork session')).toHaveLength(2);
		expect(screen.getAllByTitle('Clear session')).toHaveLength(2);
		expect(screen.getAllByTitle('Favorite')).toHaveLength(2);
	});

	it('hides the Resume/Open action for a same-device (local) session row', () => {
		renderBoard();
		const localRow = rowFor('Local one');
		expect(localRow.querySelector('[title="Open"]')).toBeNull();
		expect(localRow.querySelector('[title^="Resume here"]')).toBeNull();
		// The row still carries its other actions — only Resume is gone.
		expect(localRow.querySelector('[title="Fork session"]')).toBeTruthy();
	});

	it('shows the Resume action, tagged with the owning device, for a remote session row', () => {
		renderBoard();
		expect(rowFor('Remote one').querySelector('[title="Resume here (Mac Studio)"]')).toBeTruthy();
	});

	it('forks the row and opens the NEW session, without opening the clicked row', async () => {
		const { onOpenSession } = renderBoard();
		const fork = rowFor('Local one').querySelector('[title="Fork session"]') as HTMLElement;

		await fireEvent.click(fork);

		expect(mocks.forkSession).toHaveBeenCalledWith('a');
		await vi.waitFor(() => expect(onOpenSession).toHaveBeenCalledWith('forked-a'));
		// stopPropagation: the row's own click (which would open 'a') never fired.
		expect(onOpenSession).not.toHaveBeenCalledWith('a');
	});

	it('resumes a remote session by adopting it, then opening it', async () => {
		const { onOpenSession } = renderBoard();
		const resume = rowFor('Remote one').querySelector('[title="Resume here (Mac Studio)"]') as HTMLElement;

		await fireEvent.click(resume);

		expect(mocks.adoptSession).toHaveBeenCalledWith('b');
		await vi.waitFor(() => expect(onOpenSession).toHaveBeenCalledWith('b'));
	});

	it('still fires the pre-existing Favorite rail action', async () => {
		renderBoard();
		const fav = rowFor('Local one').querySelector('[title="Favorite"]') as HTMLElement;

		await fireEvent.click(fav);

		expect(mocks.toggleSessionFavorite).toHaveBeenCalledWith('a');
	});
});

/**
 * Regression: the clear-session confirmation used to blank itself out after a
 * mere 3s via a fire-and-forget timer, so users perceived "the confirm button
 * vanished as if I never clicked". The confirm window is now 8s and its timer is
 * cancelled whenever the confirm is resolved or replaced, so cancelling can't
 * leave a stale timer that later clears a fresh confirm.
 */
describe('SessionsBoard clear-session confirm timer', () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	// Arm the confirm on the "Local one" row by clicking its Clear button.
	async function armConfirm() {
		const clear = rowFor('Local one').querySelector('[title="Clear session"]') as HTMLElement;
		await fireEvent.click(clear);
	}
	const confirmVisible = () =>
		rowFor('Local one').querySelector('[title="Confirm clear"]') !== null;

	it('keeps the confirm past 3s and up to <8s, then auto-resets after 8s', async () => {
		renderBoard();
		await armConfirm();
		expect(confirmVisible()).toBe(true);

		// Old behavior blanked it here — it must still be showing.
		await vi.advanceTimersByTimeAsync(3000);
		expect(confirmVisible()).toBe(true);

		// Still armed right up to the new 8s deadline.
		await vi.advanceTimersByTimeAsync(4900);
		expect(confirmVisible()).toBe(true);

		// Past 8s the safety auto-reset fires and the plain Clear button returns.
		await vi.advanceTimersByTimeAsync(200);
		expect(confirmVisible()).toBe(false);
		expect(rowFor('Local one').querySelector('[title="Clear session"]')).toBeTruthy();
		expect(mocks.deleteSession).not.toHaveBeenCalled();
	});

	it('cancelling clears the timer so a re-armed confirm is not blanked at the old deadline', async () => {
		renderBoard();

		// Arm, then cancel partway through the window — this must cancel timer #1.
		await armConfirm();
		await vi.advanceTimersByTimeAsync(4000);
		const cancel = rowFor('Local one').querySelector('[title="Cancel"]') as HTMLElement;
		await fireEvent.click(cancel);
		expect(confirmVisible()).toBe(false);

		// Re-arm a moment later → a FRESH 8s timer (#2) with its own deadline.
		await vi.advanceTimersByTimeAsync(1000);
		await armConfirm();
		expect(confirmVisible()).toBe(true);

		// Cross the first timer's original deadline. If it were still alive it would
		// clear this fresh confirm out from under the user — it must stay armed.
		await vi.advanceTimersByTimeAsync(3200);
		expect(confirmVisible()).toBe(true);

		// The fresh timer still auto-resets on schedule, and nothing was deleted.
		await vi.advanceTimersByTimeAsync(4900);
		expect(confirmVisible()).toBe(false);
		expect(mocks.deleteSession).not.toHaveBeenCalled();
	});

	// Regression: the auto-reset timer used to be cancelled on every interactive
	// path (cancel / re-arm / confirm) but NOT on component teardown, so an armed
	// confirm surviving an unmount (e.g. navigating away) left a pending timer
	// that would fire post-unmount and write to state no longer on screen.
	it('cancels the pending auto-reset timer on unmount', async () => {
		const { unmount } = renderBoard();
		await armConfirm();
		expect(confirmVisible()).toBe(true);
		expect(vi.getTimerCount()).toBeGreaterThan(0);

		unmount();

		// The timer must be cancelled, not merely orphaned — advancing well past
		// its 8s deadline must not throw or warn from a post-unmount state write.
		expect(vi.getTimerCount()).toBe(0);
		expect(() => vi.advanceTimersByTime(10000)).not.toThrow();
	});
});

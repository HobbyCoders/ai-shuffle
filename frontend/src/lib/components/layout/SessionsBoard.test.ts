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
		clearSession: vi.fn(),
		clearMessages: vi.fn(),
		toastPush: vi.fn(),
		apiPost: vi.fn(),
		apiDelete: vi.fn()
	};
});

vi.mock('$lib/stores/app-data.svelte', () => ({
	appData: {
		loadSessions: mocks.loadSessions,
		loadProjects: mocks.loadProjects,
		deleteSession: mocks.deleteSession,
		clearSession: mocks.clearSession
	},
	sessions: mocks.sessions,
	projects: mocks.projects
}));
vi.mock('$lib/stores/active-session', () => ({
	// The board reads `$activeSession` (subscribe) AND calls
	// `activeSession.clearMessages()` on an in-place clear of the OPEN session, so
	// the mocked store must carry that method too.
	activeSession: Object.assign(mocks.activeSession, { clearMessages: mocks.clearMessages }),
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
	mocks.clearSession.mockResolvedValue(undefined);
	mocks.activeSession.set({ sessionId: null, isStreaming: false });
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
 * Core behavior for the "Clear session opens a new screen" fix: confirming Clear
 * on a session must clear its conversation IN PLACE — persist the clear to the
 * backend and (for the OPEN session) empty the live message store — while STAYING
 * on the session. It must NOT delete the session and must NOT navigate to New
 * Chat. Previously the active-session path called onNewSession()+deleteSession(),
 * which yanked the user to the blank New Chat hero and destroyed the row.
 */
describe('SessionsBoard clear-session clears in place (no New Chat, no delete)', () => {
	// Confirm requires two clicks (arm, then confirm) on the row's Clear button.
	async function confirmClear(title: string) {
		await fireEvent.click(rowFor(title).querySelector('[title="Clear session"]') as HTMLElement);
		await fireEvent.click(rowFor(title).querySelector('[title="Confirm clear"]') as HTMLElement);
	}

	it('clears the OPEN (active) session in place: persists, empties the live store, no navigate, no delete', async () => {
		// The row under test IS the session currently open on screen.
		mocks.activeSession.set({ sessionId: 'a', isStreaming: false });
		const { onNewSession, onOpenSession } = renderBoard();

		await confirmClear('Local one');

		// Persisted the clear to the backend for id 'a'…
		expect(mocks.clearSession).toHaveBeenCalledWith('a');
		// …emptied the open chat's live message store for instant feedback…
		await vi.waitFor(() => expect(mocks.clearMessages).toHaveBeenCalledTimes(1));
		// …and stayed put: never navigated to New Chat, never re-opened, never deleted.
		expect(onNewSession).not.toHaveBeenCalled();
		expect(onOpenSession).not.toHaveBeenCalled();
		expect(mocks.deleteSession).not.toHaveBeenCalled();
	});

	it('clears a NON-active session in place without touching the open chat', async () => {
		// A DIFFERENT session is open; clearing row 'a' must persist but must NOT
		// clear the open session's live store (that belongs to another session).
		mocks.activeSession.set({ sessionId: 'z', isStreaming: false });
		const { onNewSession } = renderBoard();

		await confirmClear('Local one');

		expect(mocks.clearSession).toHaveBeenCalledWith('a');
		expect(mocks.clearMessages).not.toHaveBeenCalled();
		expect(onNewSession).not.toHaveBeenCalled();
		expect(mocks.deleteSession).not.toHaveBeenCalled();
	});

	it('surfaces a stop-first toast when the backend refuses a mid-stream clear (409)', async () => {
		// The backend returns 409 while the session is streaming (wiping it under
		// an in-flight turn would corrupt the transcript). The handler must show
		// the stop-first guidance — NOT the generic failure — and must not clear
		// the open chat's live store.
		mocks.activeSession.set({ sessionId: 'a', isStreaming: false });
		mocks.clearSession.mockRejectedValueOnce({ status: 409, detail: 'streaming' });
		renderBoard();

		await confirmClear('Local one');

		expect(mocks.clearSession).toHaveBeenCalledWith('a');
		await vi.waitFor(() =>
			expect(mocks.toastPush).toHaveBeenCalledWith(
				'Stop the current response before clearing this session',
				{ tone: 'error' }
			)
		);
		expect(mocks.clearMessages).not.toHaveBeenCalled();
	});

	it('surfaces the generic failure toast for a non-409 clear error', async () => {
		mocks.activeSession.set({ sessionId: 'a', isStreaming: false });
		mocks.clearSession.mockRejectedValueOnce({ status: 500, detail: 'boom' });
		renderBoard();

		await confirmClear('Local one');

		await vi.waitFor(() =>
			expect(mocks.toastPush).toHaveBeenCalledWith('Failed to clear session', {
				tone: 'error'
			})
		);
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
		expect(mocks.clearSession).not.toHaveBeenCalled();
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
		expect(mocks.clearSession).not.toHaveBeenCalled();
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

/**
 * Regression: the mobile ⋮ tray (`actionsOpenId`) stayed open after acting on
 * any of its buttons. Every in-tray handler calls `e.stopPropagation()` so the
 * window-click closer (`handleFilterWindowClick`) never sees the click, and
 * none of the handlers cleared `actionsOpenId` themselves. Fixed by clearing
 * it directly in each handler.
 *
 * The tray's open state is asserted via `aria-expanded` on the row's ⋮ toggle
 * (RowActionMenu wires `open={actionsOpenId === session.id}` straight to it),
 * not by CSS visibility — jsdom doesn't evaluate the (hover: none)/max-width
 * media query that actually reveals the tray on real touch/narrow layouts.
 */
describe('SessionsBoard action tray open/close state', () => {
	function toggleFor(title: string): HTMLElement {
		return rowFor(title).querySelector('[title="Session actions"]') as HTMLElement;
	}
	function isTrayOpen(title: string): boolean {
		return toggleFor(title).getAttribute('aria-expanded') === 'true';
	}
	async function openTray(title: string) {
		await fireEvent.click(toggleFor(title));
		expect(isTrayOpen(title)).toBe(true);
	}

	it('closes the tray after clicking Favorite', async () => {
		renderBoard();
		await openTray('Local one');
		const fav = rowFor('Local one').querySelector('[title="Favorite"]') as HTMLElement;
		await fireEvent.click(fav);
		expect(isTrayOpen('Local one')).toBe(false);
	});

	it('closes the tray after clicking Fork', async () => {
		renderBoard();
		await openTray('Local one');
		const fork = rowFor('Local one').querySelector('[title="Fork session"]') as HTMLElement;
		await fireEvent.click(fork);
		expect(isTrayOpen('Local one')).toBe(false);
	});

	it('closes the tray after Resume (on a remote row)', async () => {
		renderBoard();
		await openTray('Remote one');
		const resume = rowFor('Remote one').querySelector('[title^="Resume here"]') as HTMLElement;
		await fireEvent.click(resume);
		expect(isTrayOpen('Remote one')).toBe(false);
	});

	it('keeps the tray open on the first Clear-session click, closes on Confirm', async () => {
		renderBoard();
		await openTray('Local one');

		// First click only arms the confirm — the tray must stay open so the
		// user can see the Confirm/Cancel pair it reveals.
		const clear = rowFor('Local one').querySelector('[title="Clear session"]') as HTMLElement;
		await fireEvent.click(clear);
		expect(isTrayOpen('Local one')).toBe(true);
		expect(mocks.clearSession).not.toHaveBeenCalled();

		// Confirming actually clears the conversation (never deletes the row) and
		// must close the tray.
		const confirm = rowFor('Local one').querySelector('[title="Confirm clear"]') as HTMLElement;
		await fireEvent.click(confirm);
		expect(mocks.clearSession).toHaveBeenCalledWith('a');
		expect(mocks.deleteSession).not.toHaveBeenCalled();
		expect(isTrayOpen('Local one')).toBe(false);
	});

	it('closes the tray after Cancel of an armed delete confirm', async () => {
		renderBoard();
		await openTray('Local one');
		const clear = rowFor('Local one').querySelector('[title="Clear session"]') as HTMLElement;
		await fireEvent.click(clear);
		expect(isTrayOpen('Local one')).toBe(true);

		const cancel = rowFor('Local one').querySelector('[title="Cancel"]') as HTMLElement;
		await fireEvent.click(cancel);
		expect(isTrayOpen('Local one')).toBe(false);
		expect(mocks.clearSession).not.toHaveBeenCalled();
		expect(mocks.deleteSession).not.toHaveBeenCalled();
	});
});

/**
 * Regression: "sometimes clicking Confirm on Clear session opens a new chat."
 *
 * A stray click landing on the row while its delete is in flight (the browser
 * retargets a click on the now-`disabled` Confirm button to its parent row) must
 * NOT re-open the just-deleted session — which the top bar would surface as a
 * fresh, empty chat. openSession() therefore guards on a DEDICATED
 * `deleteInFlightId`, set before the request and cleared only in its `finally`.
 * The guard used to piggyback on `deletingSessionId` (the armed-confirm UI
 * state), which Escape, Cancel, outside-click and arming another row all null at
 * will — each of those torn the guard down mid-request. Every case below holds
 * the delete promise open, fires one of those guard-killers, then clicks the
 * deleted row and asserts nothing opens.
 */
describe('SessionsBoard clear-session confirm never opens a new chat', () => {
	// Arm + confirm the clear on "Local one" (id 'a') with the delete held open;
	// returns the resolver so each test controls when the request "completes".
	async function confirmWithDeleteHeldOpen() {
		let resolveDelete!: () => void;
		mocks.clearSession.mockImplementationOnce(
			() => new Promise<void>((res) => { resolveDelete = () => res(); })
		);
		const rendered = renderBoard();
		await fireEvent.click(rowFor('Local one').querySelector('[title="Clear session"]') as HTMLElement);
		await fireEvent.click(rowFor('Local one').querySelector('[title="Confirm clear"]') as HTMLElement);
		expect(mocks.clearSession).toHaveBeenCalledWith('a');
		return { ...rendered, resolveDelete };
	}
	async function finish(resolveDelete: () => void) {
		resolveDelete();
		await vi.waitFor(() => expect(mocks.clearSession).toHaveBeenCalledTimes(1));
	}

	it('keeps the row-open guard live while the delete is in flight', async () => {
		const { onOpenSession, onNewSession, resolveDelete } = await confirmWithDeleteHeldOpen();

		// Delete still in flight — a click that reaches the row must be swallowed by
		// the guard, not routed on to open the just-deleted session.
		await fireEvent.click(rowFor('Local one'));
		expect(onOpenSession).not.toHaveBeenCalled();
		expect(onNewSession).not.toHaveBeenCalled();

		await finish(resolveDelete);
	});

	it('Escape during the in-flight window does not tear the guard down', async () => {
		const { onOpenSession, resolveDelete } = await confirmWithDeleteHeldOpen();

		// Escape unconditionally disarms the confirm UI (handleFilterWindowKeydown)
		// — that must not defeat the in-flight guard.
		await fireEvent.keyDown(window, { key: 'Escape' });
		await fireEvent.click(rowFor('Local one'));
		expect(onOpenSession).not.toHaveBeenCalled();

		await finish(resolveDelete);
	});

	it('Cancel clicked during the in-flight window does not tear the guard down', async () => {
		const { onOpenSession, resolveDelete } = await confirmWithDeleteHeldOpen();

		// On desktop the rail is hover-revealed (not gated on the ⋮ tray state), so
		// the Cancel button is still clickable while the request runs; it must only
		// drop the confirm UI, never the in-flight guard.
		const cancel = rowFor('Local one').querySelector('[title="Cancel"]') as HTMLElement;
		expect(cancel).toBeTruthy();
		await fireEvent.click(cancel);
		await fireEvent.click(rowFor('Local one'));
		expect(onOpenSession).not.toHaveBeenCalled();

		await finish(resolveDelete);
	});

	it("arming another row's clear during the in-flight window does not tear the guard down", async () => {
		const { onOpenSession, resolveDelete } = await confirmWithDeleteHeldOpen();

		// Arming row B calls armDeleteConfirm → disarmDeleteConfirm, which used to
		// null the shared variable guarding row A's in-flight delete.
		await fireEvent.click(rowFor('Remote one').querySelector('[title="Clear session"]') as HTMLElement);
		expect(rowFor('Remote one').querySelector('[title="Confirm clear"]')).toBeTruthy();

		await fireEvent.click(rowFor('Local one'));
		expect(onOpenSession).not.toHaveBeenCalledWith('a');
		expect(onOpenSession).not.toHaveBeenCalled();

		await finish(resolveDelete);
	});
});

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { platform } from '$lib/stores/platform.svelte';
import { assistantState } from '$lib/stores/assistant';
import type { AssistantState } from '$lib/api/assistant';

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
		setSessionArchived: vi.fn(),
		banner: vi.fn((..._args: unknown[]) => 'banner-1'),
		dismissLive: vi.fn(),
		deleteSession: vi.fn(),
		renameSession: vi.fn(),
		loadSessions: vi.fn(),
		forkSession: vi.fn(),
		newChat: vi.fn(),
		setTitle: vi.fn(),
		toastPush: vi.fn(),
		remember: vi.fn(),
		gitCurrent: {
			hydrated: true,
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
		setSessionArchived: mocks.setSessionArchived,
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
	notify: Object.assign(mocks.toastPush, { banner: mocks.banner, dismissLive: mocks.dismissLive }),
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
		remember: mocks.remember,
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
	assistantState.set(null);
	Object.defineProperty(window, 'matchMedia', {
		writable: true,
		value: vi.fn().mockImplementation(() => ({
			matches: false,
			addEventListener: vi.fn(),
			removeEventListener: vi.fn(),
		})),
	});
	mocks.activeSession.set({ sessionId: null });
	mocks.gitCurrent.hydrated = true;
	mocks.gitCurrent.sessionId = null;
	mocks.gitCurrent.projectId = null;
	mocks.gitCurrent.worktreeId = null;
	mocks.gitCurrent.status = null;
	mocks.gitCurrent.diffStats = null;
	mocks.watch.mockReturnValue(() => {});
	mocks.getSessionGit.mockReturnValue(null);
	mocks.setSessionActive.mockResolvedValue(undefined);
	mocks.setSessionArchived.mockResolvedValue(undefined);
	mocks.deleteSession.mockResolvedValue(undefined);
	mocks.renameSession.mockResolvedValue(undefined);
	mocks.loadSessions.mockResolvedValue(undefined);
	mocks.forkSession.mockResolvedValue({ session_id: 'forked-session' });
});

afterEach(() => {
	cleanup();
	platform.set('desktop');
});

describe('SessionCard archive and undo', () => {
	type BannerOpts = { action: { label: string; onClick: () => void }; duration_ms: number; persist: boolean };
	const lastBanner = () => mocks.banner.mock.calls.at(-1) as unknown as [string, BannerOpts];

	it('offers Undo after settling, which moves the session back to Active', async () => {
		renderCard({ is_active: true });
		await fireEvent.click(screen.getByTitle('Settle session'));
		const [message, opts] = lastBanner();
		expect(message).toBe('Settled “Sidebar work”');
		expect(opts).toMatchObject({ persist: false, duration_ms: 6000, action: { label: 'Undo' } });
		opts.action.onClick();
		expect(mocks.dismissLive).toHaveBeenCalledWith('banner-1');
		expect(mocks.setSessionActive).toHaveBeenLastCalledWith('session-a', true);
	});

	it('archives an active desktop session directly and Undo restores Active', async () => {
		renderCard({ is_active: true });
		await fireEvent.click(screen.getByTitle('Archive session'));
		expect(mocks.setSessionArchived).toHaveBeenCalledWith('session-a', true);
		lastBanner()[1].action.onClick();
		expect(mocks.setSessionArchived).toHaveBeenLastCalledWith('session-a', false, true);
	});

	it('archives a settled session and Undo restores it where it was', async () => {
		renderCard({ is_active: false });
		await fireEvent.click(screen.getByTitle('Archive session'));
		expect(mocks.setSessionArchived).toHaveBeenCalledWith('session-a', true);
		const [message, opts] = lastBanner();
		expect(message).toBe('Archived “Sidebar work”');
		opts.action.onClick();
		expect(mocks.setSessionArchived).toHaveBeenLastCalledWith('session-a', false, false);
	});

	it('offers only Restore on an archived row, with no Undo banner', async () => {
		renderCard({ is_active: false, status: 'archived' });
		expect(screen.queryByTitle('Archive session')).toBeNull();
		expect(screen.queryByTitle('Move to Active')).toBeNull();
		await fireEvent.click(screen.getByTitle('Restore archived session'));
		expect(mocks.setSessionArchived).toHaveBeenCalledWith('session-a', false);
		expect(mocks.banner).not.toHaveBeenCalled();
	});

});

describe('SessionCard actions', () => {
	it('does not offer settlement for the persistent assistant home', () => {
		assistantState.set({ home_session_id: 'session-a' } as AssistantState);
		renderCard();
		expect(screen.queryByTitle('Settle session')).toBeNull();
		expect(screen.getByText('Persistent')).toBeTruthy();
		expect(mocks.setSessionActive).not.toHaveBeenCalled();
	});
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
		// The working time closes the meta line, not the title line.
		expect(meta?.lastElementChild?.classList.contains('timer')).toBe(true);
		expect(container.querySelector('.l1 .timer')).toBeNull();
		expect(screen.getByTitle('Move to Active')).toBeTruthy();
	});

	it('shows the PR chip on a settled row that has no project id', () => {
		const { container } = renderCard(
			{ is_active: false, project_id: null, pr_number: 1839, pr_state: 'merged', pr_url: 'https://github.com/o/r/pull/1839' },
			{ compact: true }
		);
		expect(container.querySelector('.l3')?.textContent).toContain('PR #1839 merged');
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

	it.each([true, false])('keeps the mobile session and drawer open when settling (open=%s)', async (active) => {
		platform.set('mobile');
		const { onSettled, onOpen } = renderCard({ is_active: true }, { active });

		await fireEvent.click(screen.getByTitle('Settle session'));

		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', false);
		// The mobile shell's onSettled callback creates a new chat AND closes
		// the drawer. Neither navigation callback should run for this action.
		expect(onSettled).not.toHaveBeenCalled();
		expect(onOpen).not.toHaveBeenCalled();
	});

	it('does not navigate after a failed settle', async () => {
		mocks.setSessionActive.mockRejectedValueOnce(new Error('offline'));
		const { onSettled, onOpen } = renderCard({ is_active: true }, { active: true });

		await fireEvent.click(screen.getByTitle('Settle session'));

		expect(onSettled).not.toHaveBeenCalled();
		expect(onOpen).not.toHaveBeenCalled();
		expect(mocks.toastPush).toHaveBeenCalledWith('Failed to update session', { severity: 'error' });
	});

	it('names the server reason when a settle is refused (#1621)', async () => {
		mocks.setSessionActive.mockRejectedValueOnce({
			status: 503,
			detail: "DesktopWorker, the worker that runs this session, can't be reached right now. Try again once it is back online.",
		});
		renderCard({ is_active: true }, { active: true });

		await fireEvent.click(screen.getByTitle('Settle session'));

		expect(mocks.toastPush).toHaveBeenCalledWith(
			"Failed to update session: DesktopWorker, the worker that runs this session, can't be reached right now. Try again once it is back online.",
			{ severity: 'error' },
		);
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
			{ updated_at: 'not-a-date', is_active: false },
			{ timestamp: Date.parse('2026-08-20T12:03:00Z'), compact: true, status: 'idle' }
		);

		expect(container.querySelector('.timer')?.getAttribute('title')).toBe(
			'Total working time · last activity 3m ago'
		);
	});

	// Owner decision 2026-10-01: settled rows show the cumulative total too,
	// with the idle age kept in the hover title.
	it('shows the working total on an idle settled row, with its age in the title', () => {
		const { container } = renderCard(
			{ is_active: false, work_ms: 754_000, work_started_at: null },
			{ timestamp: Date.parse('2026-08-20T09:06:00Z'), compact: true, status: 'idle' }
		);

		const timer = container.querySelector('.timer');
		expect(timer?.textContent).toBe('12m 34s');
		expect(timer?.getAttribute('title')).toBe('Total working time · last activity 3h ago');
		expect(timer?.classList.contains('live')).toBe(false);
	});

	it('gives a parked card the plain timer style, not the live accent', () => {
		const parked = renderCard({ work_ms: 90_000, work_started_at: null }, { status: 'parked' });
		expect(parked.container.querySelector('.timer')?.classList.contains('live')).toBe(false);

		const working = renderCard(
			{ work_ms: 0, work_started_at: '2026-08-20T12:05:00Z' },
			{ status: 'working' }
		);
		expect(working.container.querySelector('.timer')?.classList.contains('live')).toBe(true);
	});

	// ── #1619: total working time, not time since the last activity ─────
	it('shows the banked working total on a done card instead of its age', () => {
		const { container } = renderCard({ work_ms: 754_000 }, { status: 'done' });

		const timer = container.querySelector('.timer');
		expect(timer?.textContent).toBe('12m 34s');
		expect(timer?.getAttribute('title')).toBe('Total working time');
	});

	it('adds the running turn to the banked total while it works', () => {
		// Banked 1h earlier, the current turn started 2m before `now`.
		const { container } = renderCard(
			{ work_ms: 3_600_000, work_started_at: '2026-08-20T12:04:00Z' },
			{ status: 'working' }
		);

		expect(container.querySelector('.timer')?.textContent).toBe('1h 02m');
	});

	it('keeps the total when the session parks and grows it on the next turn', async () => {
		const { container, rerender } = renderCard(
			{ work_ms: 90_000, work_started_at: null },
			{ status: 'parked' }
		);
		expect(container.querySelector('.timer')?.textContent).toBe('1m 30s');

		// An hour of idle wall-clock later the parked total has not moved.
		await rerender({ now: Date.parse('2026-08-20T13:06:00Z') } as never);
		expect(container.querySelector('.timer')?.textContent).toBe('1m 30s');

		// A wake turn starts: the total resumes from the banked value.
		await rerender({
			session: session({ work_ms: 90_000, work_started_at: '2026-08-20T13:05:30Z' }) as never,
			status: 'working',
		} as never);
		expect(container.querySelector('.timer')?.textContent).toBe('2m 00s');
	});

	it('closes a stale live interval at the last activity once the turn is done', () => {
		// The done frame arrived before the row update that banks the turn:
		// the interval ends at the last activity and does not keep growing.
		const { container } = renderCard(
			{ work_ms: 60_000, work_started_at: '2026-08-20T12:04:00Z' },
			{ status: 'done', timestamp: Date.parse('2026-08-20T12:05:00Z'), now: Date.parse('2026-08-20T14:00:00Z') }
		);

		expect(container.querySelector('.timer')?.textContent).toBe('2m 00s');
	});

	it('shows the working total on a settled row while its turn runs', () => {
		const { container } = renderCard(
			{ is_active: false, work_ms: 30_000, work_started_at: '2026-08-20T12:05:50Z' },
			{ compact: true, status: 'working' }
		);

		expect(container.querySelector('.timer')?.textContent).toBe('40s');
	});

});

describe('SessionCard swipe gestures', () => {
	// jsdom has no layout, so every row measures as the 320px fallback: the
	// lane arms past 128px (40%) and a commit waits out the 420ms slide + fold.
	type Pt = { x: number; y?: number; type?: string; id?: number };
	const down = (el: Element, p: Pt) =>
		fireEvent.pointerDown(el, { pointerId: p.id ?? 1, isPrimary: true, button: 0, pointerType: p.type ?? 'touch', clientX: p.x, clientY: p.y ?? 20 });
	const move = (el: Element, p: Pt) =>
		fireEvent.pointerMove(el, { pointerId: p.id ?? 1, isPrimary: true, pointerType: p.type ?? 'touch', clientX: p.x, clientY: p.y ?? 20 });
	const up = (el: Element, p: Pt) =>
		fireEvent.pointerUp(el, { pointerId: p.id ?? 1, isPrimary: true, button: 0, pointerType: p.type ?? 'touch', clientX: p.x, clientY: p.y ?? 20 });

	function parts(container: HTMLElement) {
		return {
			frame: container.querySelector('.scard-frame') as HTMLElement,
			card: container.querySelector('.scard') as HTMLElement,
		};
	}

	/** A slow drag (400ms between samples), so no flick reading decides it. */
	function slowClock() {
		let clock = 1000;
		return vi.spyOn(performance, 'now').mockImplementation(() => (clock += 400));
	}

	beforeEach(() => {
		platform.set('mobile');
		vi.useFakeTimers();
	});

	afterEach(() => {
		vi.useRealTimers();
		vi.restoreAllMocks();
	});

	it('settles an active row on a long right swipe, then offers Undo', async () => {
		const clock = slowClock();
		const { container, onOpen } = renderCard({ is_active: true });
		const { frame, card } = parts(container);
		await down(card, { x: 20 });
		await move(frame, { x: 100 });
		expect(frame.classList.contains('dragging')).toBe(true);
		expect(frame.classList.contains('armed')).toBe(false);
		await move(frame, { x: 200 });
		expect(frame.classList.contains('armed')).toBe(true);
		expect(container.querySelector('.lane.l.on')?.textContent).toContain('Settle');
		await up(frame, { x: 200 });
		await fireEvent.click(card);
		clock.mockRestore();

		expect(frame.classList.contains('sliding')).toBe(true);
		expect(mocks.setSessionActive).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(450);
		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', false);
		expect(mocks.banner.mock.calls.at(-1)?.[0]).toBe('Settled “Sidebar work”');
		expect(onOpen).not.toHaveBeenCalled();
	});

	it.each([true, false])('archives on a long left swipe (active=%s)', async (is_active) => {
		const clock = slowClock();
		const { container } = renderCard({ is_active });
		const { frame, card } = parts(container);
		await down(card, { x: 300 });
		await move(frame, { x: 100 });
		expect(container.querySelector('.lane.r.on')?.textContent).toContain('Archive');
		await up(frame, { x: 100 });
		clock.mockRestore();
		await vi.advanceTimersByTimeAsync(450);
		expect(mocks.setSessionArchived).toHaveBeenCalledWith('session-a', true);
		expect(mocks.setSessionActive).not.toHaveBeenCalled();
	});

	it('moves a settled row back to Active on a right swipe', async () => {
		const clock = slowClock();
		const { container } = renderCard({ is_active: false }, { compact: true });
		const { frame, card } = parts(container);
		await down(card, { x: 20 });
		await move(frame, { x: 220 });
		expect(container.querySelector('.lane.l.on')?.textContent).toContain('Move to Active');
		await up(frame, { x: 220 });
		clock.mockRestore();
		await vi.advanceTimersByTimeAsync(450);
		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', true);
	});

	it('springs a short slow drag back and keeps the next tap working', async () => {
		const clock = slowClock();
		const { container, onOpen } = renderCard();
		const { frame, card } = parts(container);
		await down(card, { x: 100 });
		await move(frame, { x: 160 });
		await up(frame, { x: 160 });
		// The click the drag leaves behind does not open the session…
		await fireEvent.click(card);
		clock.mockRestore();
		expect(frame.classList.contains('dragging')).toBe(false);
		expect(frame.classList.contains('sliding')).toBe(false);
		await vi.advanceTimersByTimeAsync(450);
		expect(mocks.setSessionActive).not.toHaveBeenCalled();
		expect(onOpen).not.toHaveBeenCalled();

		// …but a real tap afterwards does.
		await down(card, { x: 100, id: 2 });
		await up(frame, { x: 100, id: 2 });
		await fireEvent.click(card);
		expect(onOpen).toHaveBeenCalledWith('session-a');
	});

	it('commits a fast flick short of the arm point', async () => {
		// 50px in 20ms: 2.5px/ms, well over the flick speed.
		const ticks = [1000, 1020, 1040];
		vi.spyOn(performance, 'now').mockImplementation(() => ticks.shift() ?? 1040);
		const { container } = renderCard({ is_active: true });
		const { frame, card } = parts(container);
		await down(card, { x: 20 });
		await move(frame, { x: 60 });
		await move(frame, { x: 110 });
		expect(frame.classList.contains('armed')).toBe(false);
		await up(frame, { x: 110 });
		await vi.advanceTimersByTimeAsync(450);
		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', false);
	});

	it('leaves a vertical move to the list scroll', async () => {
		const { container } = renderCard();
		const { frame, card } = parts(container);
		await down(card, { x: 100, y: 20 });
		await move(frame, { x: 104, y: 80 });
		await move(frame, { x: 260, y: 90 });
		expect(frame.classList.contains('dragging')).toBe(false);
		await up(frame, { x: 260, y: 90 });
		await vi.advanceTimersByTimeAsync(450);
		expect(mocks.setSessionActive).not.toHaveBeenCalled();
	});

	it('never swipes with a mouse; the hover actions cover it', async () => {
		platform.set('desktop');
		const { container } = renderCard();
		const { frame, card } = parts(container);
		await down(card, { x: 20, type: 'mouse' });
		await move(frame, { x: 260, type: 'mouse' });
		expect(frame.classList.contains('dragging')).toBe(false);
		await up(frame, { x: 260, type: 'mouse' });
		await vi.advanceTimersByTimeAsync(450);
		expect(mocks.setSessionActive).not.toHaveBeenCalled();
		expect(screen.getByTitle('Settle session')).toBeTruthy();
	});

	it('does not swipe an archived row left, since it is archived already', async () => {
		const { container } = renderCard({ is_active: false, status: 'archived' });
		const { frame, card } = parts(container);
		await down(card, { x: 300 });
		await move(frame, { x: 100 });
		expect(frame.classList.contains('dragging')).toBe(false);
	});

	it('starts selection on a long press without opening the row', async () => {
		const onLongPress = vi.fn();
		const { container, onOpen } = renderCard({}, { onLongPress });
		const { frame, card } = parts(container);
		await down(card, { x: 100 });
		expect(frame.classList.contains('pressing')).toBe(true);
		await vi.advanceTimersByTimeAsync(500);
		expect(onLongPress).toHaveBeenCalledWith('session-a');
		await up(frame, { x: 100 });
		await fireEvent.click(card);
		expect(onOpen).not.toHaveBeenCalled();
	});

	it('cancels the long press once the finger moves', async () => {
		const onLongPress = vi.fn();
		const { container } = renderCard({}, { onLongPress });
		const { frame, card } = parts(container);
		await down(card, { x: 100 });
		await move(frame, { x: 100, y: 60 });
		await vi.advanceTimersByTimeAsync(500);
		expect(onLongPress).not.toHaveBeenCalled();
	});

	it('toggles the checkmark on tap while selecting, and does not swipe', async () => {
		const onSelectToggle = vi.fn();
		const { container, onOpen } = renderCard({}, { selecting: true, selected: true, onSelectToggle });
		const { frame, card } = parts(container);
		expect(card.getAttribute('aria-pressed')).toBe('true');
		expect(container.querySelector('.check')).toBeTruthy();
		expect(container.querySelector('.hover-acts')).toBeNull();
		await fireEvent.click(card);
		expect(onSelectToggle).toHaveBeenCalledWith('session-a');
		expect(onOpen).not.toHaveBeenCalled();
		await down(card, { x: 20 });
		await move(frame, { x: 240 });
		expect(frame.classList.contains('dragging')).toBe(false);
	});

	it('opens the row from the keyboard', async () => {
		const { container, onOpen } = renderCard();
		await fireEvent.keyDown(parts(container).card, { key: 'Enter' });
		expect(onOpen).toHaveBeenCalledWith('session-a');
	});
});

describe('SessionCard git state', () => {
	it('ignores a previous checkout placeholder assigned to a new worktree session', () => {
		mocks.gitCurrent.sessionId = 'session-a';
		mocks.gitCurrent.hydrated = false;
		mocks.gitCurrent.status = gitStatus({ has_changes: true });
		mocks.gitCurrent.diffStats = { additions: 70, deletions: 4 };
		const { container } = renderCard({ worktree_branch: 'feat/new' }, { active: true });
		expect(container.querySelector('.diff')).toBeNull();
		expect(container.querySelector('.lead')?.getAttribute('aria-label')).toBe('No changes');
		expect(mocks.watch).toHaveBeenCalled();
		expect(mocks.remember).not.toHaveBeenCalled();
	});

	it('keeps the branch PR visible across runtime transitions', async () => {
		const { container, rerender } = renderCard({}, { openPr: true, status: 'done' });
		for (const status of ['working', 'parked', 'needs_permission', 'done'] as const) {
			await rerender({ status });
			expect(container.querySelector('.l2')?.textContent).toContain('PR open');
		}
	});

	it('does not borrow another session on the same project and legacy worktree', () => {
		mocks.gitCurrent.sessionId = 'other';
		mocks.gitCurrent.projectId = 'project-a';
		mocks.gitCurrent.status = gitStatus({ has_changes: true });
		const { container } = renderCard({}, { active: true });
		expect(container.querySelector('.lead')?.getAttribute('aria-label')).toBe('No changes');
	});

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
		expect(container.querySelector('.l2')?.textContent).toContain('PR #1071 merged');
		expect(container.querySelector('.diff')?.textContent).toBe('+12 −3');
		expect(container.querySelector('.l2 .st.purple')?.textContent).toBe('PR #1071 merged');
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
		// Runtime status leads the middle row; the project moved to the meta line.
		expect(statusLine?.firstElementChild?.textContent).toBe('Working');
		expect(statusLine?.querySelector('.proj')).toBeNull();
		expect(statusLine?.querySelector('.st')?.textContent).toBe('Working');
		expect(statusLine?.querySelector('.st')?.classList.contains('accent')).toBe(true);
		// The working time closes the status line; the title keeps the top row.
		expect(statusLine?.lastElementChild?.classList.contains('timer')).toBe(true);
		expect(container.querySelector('.l1 .timer')).toBeNull();
		const meta = container.querySelector('.l3');
		expect(meta?.querySelector('.timer')).toBeNull();
		const deviceSeg = meta?.querySelector('.device-seg');
		// Device icon leads the bottom meta line (same position as settled rows).
		expect(meta?.firstElementChild).toBe(deviceSeg);
		expect(deviceSeg?.getAttribute('aria-label')).toBe('This device');
		expect(meta?.textContent).not.toContain('This device');
		// Device, then project, then branch.
		const metaText = meta?.textContent ?? '';
		expect(meta?.querySelector('.proj')?.textContent).toBe('Project A');
		expect(metaText.indexOf('Project A')).toBeLessThan(metaText.indexOf('feat/x'));
	});

	it.each([true, false])('adds three decorative dots beside Working (selected: %s)', (active) => {
		const { container } = renderCard({ is_active: true }, { status: 'working', active });
		const status = container.querySelector('.l2 .st');
		const dots = status?.querySelector('.working-dots');

		expect(status?.textContent).toBe('Working');
		expect(dots?.getAttribute('aria-hidden')).toBe('true');
		expect(dots?.querySelectorAll('span')).toHaveLength(3);
	});

	it.each(['needs_permission', 'has_question', 'await', 'pr', 'pr_merged', 'done', 'idle'])(
		'does not show working dots for %s sessions',
		(status) => {
			const { container } = renderCard({ is_active: true }, { status });
			expect(container.querySelector('.working-dots')).toBeNull();
		}
	);

	it('removes the working dots when the session finishes', async () => {
		const { container, rerender } = renderCard({ is_active: true }, { status: 'working' });
		expect(container.querySelector('.working-dots')).toBeTruthy();

		await rerender({ status: 'done' });

		expect(container.querySelector('.working-dots')).toBeNull();
		expect(container.querySelector('.l2 .st')?.textContent).toBe('Done');
	});

	it('names the pip/warning state for screen readers on compact rows, where no status word shows', () => {
		const working = renderCard({ is_active: true }, { status: 'working', compact: true });
		expect(working.container.querySelector('.title .sr-only')?.textContent).toBe('Working: ');
		cleanup();

		const parked = renderCard({ is_active: true }, { status: 'needs_permission', compact: true });
		expect(parked.container.querySelector('.title .sr-only')?.textContent).toBe('Needs permission: ');
		cleanup();

		// Full rows already print the status word on line two.
		const full = renderCard({ is_active: true }, { status: 'working' });
		expect(full.container.querySelector('.title .sr-only')).toBeNull();
	});

	it('keeps compact cards free of the working status and dots', () => {
		const { container } = renderCard({}, { status: 'working', compact: true });
		expect(container.querySelector('.l2')).toBeNull();
		expect(container.querySelector('.working-dots')).toBeNull();
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
		mocks.gitCurrent.projectId = 'project-a';
		mocks.gitCurrent.status = gitStatus({ has_changes: true });
		mocks.gitCurrent.diffStats = { additions: 7, deletions: 1, files_changed: 1 };

		const { container } = renderCard({ worktree_branch: 'feat/x' }, { active: true });

		expect(mocks.watch).not.toHaveBeenCalled();
		expect(container.querySelector('.diff')?.textContent).toBe('+7 −1');
		expect(container.querySelector('.lead')?.getAttribute('aria-label')).toBe('Uncommitted changes');
	});
});

describe('SessionCard several pull requests', () => {
	const pr = (number: number, state: 'open' | 'merged' | 'closed', extra: Record<string, unknown> = {}) => ({
		number,
		url: `https://github.com/o/r/pull/${number}`,
		state,
		title: `Change ${number}`,
		...extra,
	});

	it('keeps one PR exactly as before, with no list control', () => {
		const { container } = renderCard({ pr_state: 'open', pr_number: 42, pull_requests: [pr(42, 'open')] }, { status: 'pr' });
		expect(container.querySelector('.l2 .st.pr')?.textContent).toBe('PR #42 open');
		expect(container.querySelector('.pr-chip.multi')).toBeNull();
		expect(screen.queryByRole('button', { name: /Pull requests/ })).toBeNull();
	});

	it('summarises open and merged PRs in one toned chip', () => {
		const { container } = renderCard(
			{
				pr_state: 'merged',
				pr_number: 3,
				pull_requests: [pr(1, 'open'), pr(2, 'open', { source: 'subagent' }), pr(3, 'merged')],
			},
			{ status: 'pr' },
		);
		const chip = container.querySelector('.l2 .pr-chip.multi');
		expect(chip?.textContent).toBe('2 PRs open · 1 merged');
		expect(chip?.querySelector('.green')?.textContent).toBe('2 PRs open');
		expect(chip?.querySelector('.purple')?.textContent).toBe('1 merged');
	});

	it('lists every PR from the chip and closes on Escape', async () => {
		const { container, onOpen } = renderCard({
			pr_state: 'merged',
			pr_number: 3,
			pull_requests: [pr(1, 'open'), pr(2, 'open', { source: 'subagent', title: 'Worker fix' }), pr(3, 'merged')],
		});
		const button = screen.getByRole('button', { name: 'Pull requests: 2 PRs open, 1 merged' });
		// The chip itself is the control; no separate row action duplicates it.
		expect(button.classList.contains('pr-chip')).toBe(true);
		expect(screen.getAllByRole('button', { name: /Pull requests/ })).toHaveLength(1);
		expect(container.querySelector('.hover-acts button[aria-haspopup]')).toBeNull();
		expect(button.getAttribute('aria-haspopup')).toBe('dialog');
		expect(button.getAttribute('aria-expanded')).toBe('false');
		await fireEvent.click(button);
		expect(onOpen).not.toHaveBeenCalled();
		expect(button.getAttribute('aria-expanded')).toBe('true');

		const dialog = screen.getByRole('dialog', { name: 'Pull requests' });
		const links = [...dialog.querySelectorAll('a')];
		// Newest first, each a link to the PR.
		expect(links.map((a) => a.getAttribute('href'))).toEqual([
			'https://github.com/o/r/pull/3',
			'https://github.com/o/r/pull/2',
			'https://github.com/o/r/pull/1',
		]);
		// A sub-agent's PR reads like any other: no tag taking room from the title.
		expect(links[1].getAttribute('aria-label')).toBe('#2 Worker fix, open');
		expect(dialog.querySelector('.pr-tag')).toBeNull();
		expect(dialog.textContent).not.toContain('sub-agent');
		expect(links[0].getAttribute('target')).toBe('_blank');

		await fireEvent.keyDown(dialog, { key: 'Escape' });
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(document.activeElement).toBe(button);
	});

	it('opens the list with Enter on the focused chip and returns focus on Escape', async () => {
		const { onOpen } = renderCard({
			pull_requests: [pr(1, 'open'), pr(2, 'merged')],
			pr_state: 'merged',
			pr_number: 2,
		});
		const chip = screen.getByRole('button', { name: 'Pull requests: 1 PR open, 1 merged' });
		expect(chip.tagName).toBe('BUTTON');
		expect(chip.classList.contains('pr-chip')).toBe(true);
		expect(chip.getAttribute('type')).toBe('button');
		chip.focus();
		expect(document.activeElement).toBe(chip);
		// A native button turns Enter into a click; jsdom does not, so mirror it.
		const enter = await fireEvent.keyDown(chip, { key: 'Enter' });
		expect(enter).toBe(true);
		await fireEvent.click(chip);
		expect(onOpen).not.toHaveBeenCalled();
		expect(chip.getAttribute('aria-expanded')).toBe('true');
		const dialog = screen.getByRole('dialog', { name: 'Pull requests' });
		await fireEvent.keyDown(dialog, { key: 'Escape' });
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(chip.getAttribute('aria-expanded')).toBe('false');
		expect(document.activeElement).toBe(chip);
	});

	it('opens the list from the chip without opening the session', async () => {
		const { container, onOpen } = renderCard({
			pull_requests: [pr(1, 'merged'), pr(2, 'merged')],
			pr_state: 'merged',
			pr_number: 2,
		});
		const chip = container.querySelector('.pr-chip.multi') as HTMLElement;
		expect(chip.textContent).toBe('2 PRs merged');
		await fireEvent.click(chip);
		expect(onOpen).not.toHaveBeenCalled();
		expect(screen.getByRole('dialog', { name: 'Pull requests' })).toBeTruthy();
		await fireEvent.click(chip);
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('gives the chip a 44 px touch target without growing the row', async () => {
		const { container, onOpen } = renderCard({
			pull_requests: [pr(1, 'merged'), pr(2, 'merged')],
			pr_state: 'merged',
			pr_number: 2,
		});
		const chip = container.querySelector('.pr-chip.multi') as HTMLElement;
		const card = container.querySelector('.scard') as HTMLElement;
		const frame = container.querySelector('.scard-frame') as HTMLElement;
		// The chip is a 16 px line: 100..180 x 40..56.
		chip.getBoundingClientRect = () =>
			({ left: 100, top: 40, width: 80, height: 16, right: 180, bottom: 56, x: 100, y: 40, toJSON() {} }) as DOMRect;
		const tap = async (type: string, x: number, y: number) => {
			await fireEvent.pointerDown(frame, { pointerId: 1, isPrimary: true, button: 0, pointerType: type, clientX: x, clientY: y });
			await fireEvent.pointerUp(frame, { pointerId: 1, isPrimary: true, button: 0, pointerType: type, clientX: x, clientY: y });
			await fireEvent.click(card, { clientX: x, clientY: y });
		};
		// A finger 12 px below the text still means the chip.
		await tap('touch', 140, 68);
		expect(onOpen).not.toHaveBeenCalled();
		expect(screen.getByRole('dialog', { name: 'Pull requests' })).toBeTruthy();
		await fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
		// Outside the 44 px band, the tap opens the chat as before.
		await tap('touch', 140, 80);
		expect(onOpen).toHaveBeenCalledTimes(1);
		// A mouse is precise: the same near-miss opens the chat.
		await tap('mouse', 140, 68);
		expect(onOpen).toHaveBeenCalledTimes(2);
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('shows the same toned summary on a settled row', () => {
		const { container } = renderCard(
			{ is_active: false, pull_requests: [pr(1, 'merged'), pr(2, 'open')], pr_state: 'open', pr_number: 2 },
			{ compact: true },
		);
		expect(container.querySelector('.l3 .pr-chip.multi')?.textContent).toBe('1 PR open · 1 merged');
	});
});


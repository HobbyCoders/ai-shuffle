import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { platform } from '$lib/stores/platform.svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

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

	it('settles without an Undo banner', async () => {
		renderCard({ is_active: true });
		await fireEvent.click(screen.getByTitle('Settle session'));
		await vi.waitFor(() => expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', false));
		expect(mocks.banner).not.toHaveBeenCalled();
	});

	it.each([true, false])('has no archive or restore control (active=%s)', (is_active) => {
		renderCard({ is_active });
		expect(screen.queryByTitle('Archive session')).toBeNull();
		expect(screen.queryByTitle('Restore archived session')).toBeNull();
	});

	it('reactivates legacy metadata without Restore or Undo', async () => {
		renderCard({ is_active: false, status: 'archived' });
		await fireEvent.click(screen.getByTitle('Move to Active'));
		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', true);
		expect(mocks.setSessionArchived).not.toHaveBeenCalled();
		expect(mocks.banner).not.toHaveBeenCalled();
	});

});

describe('SessionCard actions', () => {
	it('keeps settle and omits rename, fork, and delete controls', () => {
		renderCard();

		expect(screen.getByTitle('Settle session')).toBeTruthy();
		expect(screen.queryByTitle('Rename session')).toBeNull();
		expect(screen.queryByTitle('Fork session')).toBeNull();
		expect(screen.queryByTitle('Delete session')).toBeNull();
	});

	it('gives only open, running or blocked rows the full foreground title', () => {
		const idle = renderCard({ is_active: true }, { status: 'done' });
		expect(idle.container.querySelector('.scard.emphasized')).toBeNull();
		cleanup();
		for (const status of ['working', 'parked', 'await', 'failed'] as const) {
			const row = renderCard({ is_active: true }, { status });
			expect(row.container.querySelector('.scard.emphasized')).toBeTruthy();
			cleanup();
		}
		const open = renderCard({ is_active: true }, { status: 'done', active: true });
		expect(open.container.querySelector('.scard.emphasized')).toBeTruthy();
	});

	it('renders a settled row on two quiet lines, keeping its details and action', () => {
		const { container } = renderCard(
			{ is_active: false, worktree_branch: 'fix/thing', work_ms: 90_000 },
			{ compact: true }
		);

		expect(container.querySelector('.scard.compact')).toBeTruthy();
		expect(container.querySelector('.title')?.textContent).toContain('Sidebar work');
		expect(container.querySelector('.l2 .where')?.textContent).toBe('fix/thing');
		expect(container.querySelector('.l1 .timer')?.textContent).toBe('1m');
		// The rest of the row's facts stay on hover and for screen readers.
		const tip = container.querySelector('.body')?.getAttribute('title');
		expect(tip).toContain('Sidebar work');
		for (const part of ['This device', 'Project A', 'fix/thing']) {
			expect(tip).toContain(part);
			expect(container.querySelector('.details')?.textContent).toContain(part);
		}
		expect(screen.getByTitle('Move to Active')).toBeTruthy();
	});

	it('leaves the time off a row with no recorded work', () => {
		const { container } = renderCard({ is_active: false }, { compact: true, status: 'idle' });
		expect(container.querySelector('.timer')).toBeNull();
	});

	it('names the PR on a settled row that has no project id', () => {
		const { container } = renderCard(
			{ is_active: false, project_id: null, pr_number: 1839, pr_state: 'merged', pr_url: 'https://github.com/o/r/pull/1839' },
			{ compact: true }
		);
		expect(container.querySelector('.details')?.textContent).toContain('PR #1839 merged');
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
			{ updated_at: 'not-a-date', is_active: false, work_ms: 90_000 },
			{ timestamp: Date.parse('2026-08-20T12:03:00Z'), compact: true, status: 'idle' }
		);

		expect(container.querySelector('.timer')?.getAttribute('title')).toBe(
			'Total working time 1m 30s · last activity 3m ago'
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
		expect(timer?.textContent).toBe('12m');
		expect(timer?.getAttribute('title')).toBe('Total working time 12m 34s · last activity 3h ago');
	});

	// Owner decision 2026-11-05: state belongs to the state column, not the clock.
	it('draws the time in one neutral tone, whatever the row is doing', () => {
		for (const status of ['working', 'parked', 'needs_permission', 'failed', 'offline', 'done', 'idle']) {
			for (const compact of [false, true]) {
				const { container } = renderCard({ work_ms: 90_000, is_active: !compact }, { status, compact });
				expect(container.querySelector('.timer')?.className.replace(/\s*svelte-\S+/g, '')).toBe('timer');
				cleanup();
			}
		}
		const source = readFileSync(resolve(import.meta.dirname, 'SessionCard.svelte'), 'utf8');
		expect(source.match(/\.timer[^{]*\{[^}]*\}/g)?.join()).not.toMatch(/--primary|--warning|--destructive|--success/);
		expect(source).not.toMatch(/\.timer\.[a-z]/);
	});

	it('ticks seconds only while the row is working, with the exact total on hover everywhere', () => {
		const shown = (status: string) => {
			const { container } = renderCard({ work_ms: 754_000, work_started_at: null }, { status });
			const timer = container.querySelector('.timer');
			const out = [timer?.textContent, timer?.getAttribute('title')];
			cleanup();
			return out;
		};
		expect(shown('working')).toEqual(['12m 34s', 'Total working time 12m 34s']);
		for (const status of ['parked', 'needs_permission', 'has_question', 'await', 'failed', 'offline', 'done', 'idle']) {
			expect(shown(status)).toEqual(['12m', 'Total working time 12m 34s']);
		}
	});

	it('keeps an hour-long total short, and its seconds on hover', () => {
		const { container } = renderCard({ work_ms: 3_969_000, work_started_at: null }, { status: 'done' });
		const timer = container.querySelector('.timer');
		expect(timer?.textContent).toBe('1h 06m');
		expect(timer?.getAttribute('title')).toBe('Total working time 1h 06m 09s');
	});

	// ── #1619: total working time, not time since the last activity ─────
	it('shows the banked working total on a done card instead of its age', () => {
		const { container } = renderCard({ work_ms: 754_000 }, { status: 'done' });

		const timer = container.querySelector('.timer');
		expect(timer?.textContent).toBe('12m');
		expect(timer?.getAttribute('title')).toBe('Total working time 12m 34s');
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
		expect(container.querySelector('.timer')?.textContent).toBe('1m');

		// An hour of idle wall-clock later the parked total has not moved.
		await rerender({ now: Date.parse('2026-08-20T13:06:00Z') } as never);
		expect(container.querySelector('.timer')?.textContent).toBe('1m');

		// A wake turn starts: the total resumes from the banked value.
		await rerender({
			session: session({ work_ms: 90_000, work_started_at: '2026-08-20T13:05:30Z' }) as never,
			status: 'working',
		} as never);
		expect(container.querySelector('.timer')?.textContent).toBe('2m 00s');
	});

	it('keeps counting while parked on background work', async () => {
		// Parked at 12:05 with 90s banked; the server keeps the park's segment open.
		const { container, rerender } = renderCard(
			{ work_ms: 90_000, work_started_at: '2026-08-20T12:05:00Z' },
			{ status: 'parked' }
		);
		expect(container.querySelector('.timer')?.textContent).toBe('2m');

		// Ten minutes later the background work is still running.
		await rerender({ now: Date.parse('2026-08-20T12:16:00Z') } as never);
		expect(container.querySelector('.timer')?.textContent).toBe('12m');
		expect(container.querySelector('.timer')?.getAttribute('title')).toBe('Total working time 12m 30s');
	});

	it('closes a stale live interval at the last activity once the turn is done', () => {
		// The done frame arrived before the row update that banks the turn:
		// the interval ends at the last activity and does not keep growing.
		const { container } = renderCard(
			{ work_ms: 60_000, work_started_at: '2026-08-20T12:04:00Z' },
			{ status: 'done', timestamp: Date.parse('2026-08-20T12:05:00Z'), now: Date.parse('2026-08-20T14:00:00Z') }
		);

		expect(container.querySelector('.timer')?.textContent).toBe('2m');
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

	it('settles an active row on a long left swipe, with no Undo banner', async () => {
		const clock = slowClock();
		const { container, onOpen } = renderCard({ is_active: true });
		const { frame, card } = parts(container);
		await down(card, { x: 300 });
		await move(frame, { x: 220 });
		expect(frame.classList.contains('dragging')).toBe(true);
		expect(frame.classList.contains('armed')).toBe(false);
		await move(frame, { x: 120 });
		expect(frame.classList.contains('armed')).toBe(true);
		// The lane uncovered on the right is the settle lane, in its colour.
		const lane = container.querySelector('.lane.r.on');
		expect(lane?.textContent).toContain('Settle');
		expect(lane?.classList.contains('toggle')).toBe(true);
		await up(frame, { x: 120 });
		await fireEvent.click(card);
		clock.mockRestore();

		expect(frame.classList.contains('sliding')).toBe(true);
		// The row leaves the way the finger went.
		expect(card.style.transform).toBe('translateX(-320px)');
		expect(mocks.setSessionActive).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(450);
		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', false);
		expect(mocks.banner).not.toHaveBeenCalled();
		expect(onOpen).not.toHaveBeenCalled();
	});

	it.each([true, false])('right swipe has no lifecycle side effects (active=%s)', async (is_active) => {
		const clock = slowClock();
		const { container } = renderCard({ is_active });
		const { frame, card } = parts(container);
		await down(card, { x: 20 });
		await move(frame, { x: 220 });
		await up(frame, { x: 220 });
		clock.mockRestore();
		await vi.advanceTimersByTimeAsync(450);
		expect(container.querySelector('.lane.l')).toBeNull();
		expect(mocks.setSessionArchived).not.toHaveBeenCalled();
		expect(mocks.setSessionActive).not.toHaveBeenCalled();
		expect(mocks.banner).not.toHaveBeenCalled();
	});

	it('moves a settled row back to Active on a left swipe', async () => {
		const clock = slowClock();
		const { container } = renderCard({ is_active: false }, { compact: true });
		const { frame, card } = parts(container);
		await down(card, { x: 300 });
		await move(frame, { x: 100 });
		expect(container.querySelector('.lane.r.on')?.textContent).toContain('Move to Active');
		await up(frame, { x: 100 });
		clock.mockRestore();
		await vi.advanceTimersByTimeAsync(450);
		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', true);
		expect(mocks.setSessionArchived).not.toHaveBeenCalled();
	});

	it('restores an archived row on a left swipe', async () => {
		const clock = slowClock();
		const { container } = renderCard({ is_active: false, status: 'archived' }, { compact: true });
		const { frame, card } = parts(container);
		await down(card, { x: 300 });
		await move(frame, { x: 100 });
		expect(container.querySelector('.lane.r.on')?.textContent).toContain('Move to Active');
		await up(frame, { x: 100 });
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
		await down(card, { x: 300 });
		await move(frame, { x: 260 });
		await move(frame, { x: 210 });
		expect(frame.classList.contains('armed')).toBe(false);
		await up(frame, { x: 210 });
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

	it('does not swipe an archived row right, since it is archived already', async () => {
		const { container } = renderCard({ is_active: false, status: 'archived' });
		const { frame, card } = parts(container);
		await down(card, { x: 20 });
		await move(frame, { x: 220 });
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
		expect(container.querySelector('.lead')?.hasAttribute('title')).toBe(false);
		expect(mocks.watch).toHaveBeenCalled();
		expect(mocks.remember).not.toHaveBeenCalled();
	});

	it('keeps the branch PR visible across runtime transitions', async () => {
		const { container, rerender } = renderCard({}, { openPr: true, status: 'done' });
		for (const status of ['working', 'parked', 'needs_permission', 'done'] as const) {
			await rerender({ status });
			expect(container.querySelector('.details')?.textContent).toContain('PR open');
		}
	});

	it('does not borrow another session on the same project and legacy worktree', () => {
		mocks.gitCurrent.sessionId = 'other';
		mocks.gitCurrent.projectId = 'project-a';
		mocks.gitCurrent.status = gitStatus({ has_changes: true });
		const { container } = renderCard({}, { active: true });
		expect(container.querySelector('.lead')?.hasAttribute('title')).toBe(false);
	});

	it('claims nothing about a settled row whose checkout was never read', () => {
		const { container } = renderCard({ is_active: false, worktree_branch: 'feat/left-dirty' });
		expect(container.querySelector('.lead')?.hasAttribute('title')).toBe(false);
		expect(container.querySelector('.details')?.textContent).toContain('feat/left-dirty');
	});

	it('leads a merged session with the purple merge glyph', () => {
		const { container } = renderCard({ pr_state: 'merged', pr_number: 1071 });

		const lead = container.querySelector('.lead');
		expect(lead?.getAttribute('title')).toBe('Pull request merged');
		expect(lead?.classList.contains('purple')).toBe(true);
		expect(container.querySelector('.details')?.textContent).toContain('PR #1071 merged');
	});

	it('leads an open PR with the green pull-request glyph', () => {
		const { container } = renderCard({ pr_state: 'open', pr_number: 42 }, { status: 'pr' });

		const lead = container.querySelector('.lead');
		expect(lead?.getAttribute('title')).toBe('Pull request open');
		expect(lead?.classList.contains('green')).toBe(true);
		expect(container.querySelector('.details')?.textContent).toContain('PR #42 open');
	});

	it('leads a chat session with the chat glyph and no branch talk', () => {
		const { container } = renderCard({ project_id: null });

		expect(container.querySelector('.lead')?.getAttribute('title')).toBe('Chat session');
		expect(container.querySelector('.details')?.textContent).not.toContain('PR');
	});

	it('shows the amber diff glyph and the diffstat for an uncommitted tree', () => {
		mocks.getSessionGit.mockReturnValue({
			status: gitStatus({ has_changes: true }),
			diffStats: { additions: 124, deletions: 38, files_changed: 4 },
			loaded: true,
		});

		const { container } = renderCard({ worktree_branch: 'feat/x' });

		const lead = container.querySelector('.lead');
		expect(lead?.getAttribute('title')).toBe('Uncommitted changes');
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
		expect(lead?.getAttribute('title')).toBe('Uncommitted changes');
		expect(lead?.classList.contains('amber')).toBe(true);
		expect(container.querySelector('.details')?.textContent).toContain('PR #1071 merged');
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

		expect(container.querySelector('.lead')?.getAttribute('title')).toBe('Pushed, no PR yet');
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

		expect(container.querySelector('.lead')?.getAttribute('title')).toBe('No changes');
		expect(container.querySelector('.lead')?.getAttribute('title')).not.toBe(
			'Pushed, no PR yet'
		);
	});

	it('gives a running row a status line and keeps the time on the title line', () => {
		const { container } = renderCard(
			{ is_active: true, worktree_branch: 'feat/x' },
			{ status: 'working', projectLabel: 'Project A' }
		);

		expect(container.querySelector('.title')?.textContent).toContain('Sidebar work');
		const statusLine = container.querySelector('.l2');
		expect(statusLine?.textContent?.trim()).toBe('feat/x');
		expect(statusLine?.querySelector('.timer')).toBeNull();
		expect(container.querySelector('.l1 .timer')).toBeTruthy();
	});

	// Owner decision 2026-11-05: the state column says the state, without words.
	it.each([
		['working', 'Working'],
		['parked', 'Parked'],
		['needs_permission', 'Needs permission'],
		['has_question', 'Has question'],
		['await', 'Awaiting input'],
		['failed', 'Failed'],
		['offline', 'Offline'],
		['pr', 'Done'],
		['pr_merged', 'Done'],
		['done', 'Done'],
		['idle', 'Idle'],
	])('prints no status word on a %s row, in either lane', (status, word) => {
		for (const compact of [false, true]) {
			const { container } = renderCard(
				{ is_active: !compact, worktree_branch: 'feat/x' },
				{ status, compact },
			);
			expect(container.querySelector('.l2 .where')?.textContent).toBe('feat/x');
			expect(container.querySelector('.l2 .st:not(.pr)')).toBeNull();
			expect(container.querySelector('.l2')?.textContent).not.toContain(word);
			cleanup();
		}
	});

	it('says the state once for screen readers: the mark is hidden, the details line speaks', () => {
		for (const [status, word, compact] of [
			['working', 'Working', true],
			['needs_permission', 'Needs permission', true],
			['working', 'Working', false],
			['failed', 'Failed', false],
		] as const) {
			const { container } = renderCard({ is_active: !compact, title: 'Plain title' }, { status, compact });
			const details = container.querySelector('.details')?.textContent ?? '';
			expect(details.split(' · ')[0]).toBe(word);
			expect(details.split(word)).toHaveLength(2);
			const lead = container.querySelector('.lead');
			expect(lead?.getAttribute('aria-hidden')).toBe('true');
			expect(lead?.getAttribute('title')).toBe(word);
			for (const printed of container.querySelectorAll('.l2 .st, .l2 .where, .l2 .diff')) {
				expect(printed.getAttribute('aria-hidden')).toBe('true');
			}
			// A title on the button would be read out again as its description.
			expect(container.querySelector('.scard')?.hasAttribute('title')).toBe(false);
			expect(container.querySelector('.body')?.getAttribute('title')).toContain(word);
			cleanup();
		}
	});

	it('gives each runtime state its own mark and its word as the hover title', () => {
		const mark = (status: string, compacting = false) => {
			const { container } = renderCard({ is_active: true }, { status, compacting });
			const lead = container.querySelector('.lead')!;
			const out = {
				word: lead.getAttribute('title'),
				glyph: lead.querySelector('svg') !== null,
				classes: [...(lead.querySelector('.pip')?.classList ?? [])].filter((c) => !c.startsWith('svelte-')).join(' '),
			};
			cleanup();
			return out;
		};
		expect(mark('working')).toEqual({ word: 'Working', glyph: false, classes: 'pip' });
		expect(mark('working', true)).toEqual({ word: 'Compacting', glyph: false, classes: 'pip compacting' });
		expect(mark('parked')).toEqual({ word: 'Parked', glyph: false, classes: 'pip held' });
		expect(mark('needs_permission')).toEqual({ word: 'Needs permission', glyph: false, classes: 'pip warn' });
		expect(mark('has_question')).toEqual({ word: 'Has question', glyph: false, classes: 'pip warn' });
		expect(mark('failed')).toEqual({ word: 'Failed', glyph: false, classes: 'pip warn failed' });
		expect(mark('offline')).toEqual({ word: 'Offline', glyph: true, classes: '' });
	});

	it('only says Compacting while the row is actually working', () => {
		const { container } = renderCard({ is_active: true }, { status: 'idle', compacting: true });
		expect(container.querySelector('.pip')).toBeNull();
		expect(container.querySelector('.details')?.textContent).not.toContain('Compacting');
	});

	it('keeps the marks apart by shape, not hue alone, with motion off too', () => {
		const source = readFileSync(resolve(import.meta.dirname, 'SessionCard.svelte'), 'utf8');
		const rule = (selector: string) => source.match(new RegExp(`${selector.replace(/\./g, '\\.')} \\{[^}]*\\}`))?.[0] ?? '';
		expect(rule('.pip')).toMatch(/border-radius: 50%/);
		expect(rule('.pip.compacting')).toMatch(/width: 12px[^}]*height: 5px/);
		expect(rule('.pip.held')).toMatch(/background: transparent/);
		expect(rule('.pip.warn')).toMatch(/rotate\(45deg\)/);
		expect(rule('.pip.warn.failed')).toMatch(/clip-path: polygon/);
		// Compacting moves at half working's pace.
		expect(rule('.pip')).toMatch(/card-ping 1\.8s/);
		expect(rule('.pip.compacting')).toMatch(/card-squeeze 3\.6s/);
		// Motion off stops every mark. `.pip` alone would not beat `.pip.compacting`.
		expect(source).toMatch(/prefers-reduced-motion: reduce\) \{\s*\.pip,\s*\.pip\.compacting,\s*\.pip\.warn \{ animation: none; \}/);
	});

	it('never lets a pulse fade a mark below what it needs to stay readable', () => {
		const source = readFileSync(resolve(import.meta.dirname, 'SessionCard.svelte'), 'utf8');
		const frames = (name: string) => source.match(new RegExp(`@keyframes ${name} \\{[\\s\\S]*?\\n\\t\\}`))?.[0] ?? '';
		// 0.8 is the floor where the working colour still holds 3:1 on the light panel.
		for (const name of ['card-ping', 'card-squeeze']) {
			const opacities = [...frames(name).matchAll(/opacity: ([\d.]+)/g)].map((m) => Number(m[1]));
			expect(opacities.length).toBeGreaterThan(0);
			expect(Math.min(...opacities)).toBeGreaterThanOrEqual(0.8);
		}
		// Amber has no headroom on the light panel, so the diamond never fades at all,
		// and it stays a diamond while it pulses.
		expect(frames('card-pulse')).not.toMatch(/opacity/);
		expect(frames('card-pulse')).toMatch(/rotate\(45deg\) scale\(0\.82\)/);
	});

	it('keeps the row on two lines in every status, so starting or stopping work cannot move it', () => {
		const statuses = ['working', 'parked', 'needs_permission', 'needs_input', 'failed', 'offline', 'idle', 'done', 'pr', 'pr_merged'];
		for (const compact of [false, true]) {
			for (const status of statuses) {
				const { container } = renderCard({ is_active: !compact }, { status, compact });
				expect(container.querySelectorAll('.body > .l1')).toHaveLength(1);
				expect(container.querySelectorAll('.body > .l2')).toHaveLength(1);
				cleanup();
			}
		}
		// Both lines take their height from the row, not from what they hold.
		const source = readFileSync(resolve(import.meta.dirname, 'SessionCard.svelte'), 'utf8');
		expect(source).toMatch(/\.l1 \{[^}]*height: var\(--line-1\)/);
		expect(source).toMatch(/\.l2 \{[^}]*height: var\(--line-2\)/);
	});

	it('keeps the PR state and diffstat in view while the row works', () => {
		mocks.getSessionGit.mockReturnValue({
			status: gitStatus({ has_changes: true }),
			diffStats: { additions: 12, deletions: 3, files_changed: 4 },
			loaded: true,
		});
		const { container } = renderCard(
			{ is_active: true, pr_state: 'open', pr_number: 142, worktree_branch: 'feat/rows' },
			{ status: 'working' },
		);
		expect(container.querySelector('.lead .pip')).toBeTruthy();
		const pr = container.querySelector('.l2 .st.pr');
		expect(pr?.textContent).toBe('PR #142 open');
		expect(pr?.classList.contains('green')).toBe(true);
		expect(container.querySelector('.l2 .diff')?.textContent).toBe('+12 −3');
		expect(container.querySelector('.l2 .where')?.textContent).toBe('feat/rows');
	});

	it('prints no status word on a settled row, working or not, but keeps its PR', () => {
		for (const status of ['working', 'needs_permission', 'failed', 'done', 'idle']) {
			const { container } = renderCard(
				{ is_active: false, pr_state: 'merged', pr_number: 7 },
				{ status, compact: true },
			);
			expect(container.querySelector('.l2')?.textContent?.trim()).toBe('PR #7 merged');
			expect(container.querySelector('.l2 .st.pr.purple')?.textContent).toBe('PR #7 merged');
			cleanup();
		}
	});

	it('never leaves line two blank: a row with no facts shows its age', () => {
		const { container } = renderCard(
			{ is_active: false, worktree_branch: null, branch: null, pr_state: null, pr_number: null, pull_requests: [] },
			{ compact: true, status: 'idle' },
		);
		expect(container.querySelector('.l2 .where')?.textContent).toMatch(/ago$|^Just now$/);
	});

	it('falls back to the same age on a working row with no facts, never a status word', () => {
		const bare = { worktree_branch: null, branch: null, pr_state: null, pr_number: null, pull_requests: [] };
		for (const status of ['working', 'parked', 'needs_permission', 'failed']) {
			const { container } = renderCard({ ...bare, is_active: true }, { status });
			expect(container.querySelector('.l2 .where')?.textContent).toMatch(/ago$|^Just now$/);
			cleanup();
		}
		const remote = renderCard(
			{ ...bare, is_active: true, is_remote: true, device_name: 'GamingPC' },
			{ status: 'working' },
		);
		expect(remote.container.querySelector('.l2 .where')?.textContent).toMatch(/^GamingPC · (.* ago|Just now)$/);
	});

	it('names the project only when the row is not under a project header', () => {
		const under = renderCard({ is_active: true, worktree_branch: 'feat/a' }, { status: 'idle' });
		expect(under.container.querySelector('.l2 .where')?.textContent).toBe('feat/a');
		cleanup();
		const flat = renderCard({ is_active: true, worktree_branch: 'feat/a' }, { status: 'idle', showProject: true });
		expect(flat.container.querySelector('.l2 .where')?.textContent).toBe('Project A · feat/a');
	});

	it('marks another worker with an icon, its name on hover and in the details', () => {
		const { container } = renderCard(
			{ is_active: true, is_remote: true, device_name: 'GamingPC', worktree_branch: 'feat/a' },
			{ status: 'idle' },
		);
		expect(container.querySelector('.l2 .worker')?.getAttribute('title')).toBe('On GamingPC');
		expect(container.querySelector('.details')?.textContent).toContain('GamingPC');
		expect(container.querySelector('.l2 .where')?.textContent).toBe('feat/a');
	});

	it('marks a working row with the live pip and a parked row with the warning', () => {
		const working = renderCard({ is_active: true }, { status: 'working' });
		expect(working.container.querySelector('.lead .pip')).toBeTruthy();
		cleanup();

		const parked = renderCard({ is_active: true }, { status: 'needs_permission' });
		expect(parked.container.querySelector('.lead .pip.warn')).toBeTruthy();
		expect(parked.container.querySelector('.lead .pip.warn.failed')).toBeNull();
		expect(parked.container.querySelector('.lead')?.getAttribute('title')).toBe('Needs permission');
	});

	it('draws nothing on an idle row but keeps its gutter', () => {
		const idle = renderCard({ is_active: true }, { status: 'idle' });
		const lead = idle.container.querySelector('.lead')!;
		expect(lead).toBeTruthy();
		expect(lead.children).toHaveLength(0);
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
		expect(container.querySelector('.lead')?.getAttribute('title')).toBe('Uncommitted changes');
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
		expect(container.querySelector('.details')?.textContent).toContain('PR #42 open');
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
		expect(container.querySelector('.l2 .pr-chip.multi')?.textContent).toBe('1 PR open · 1 merged');
	});
});


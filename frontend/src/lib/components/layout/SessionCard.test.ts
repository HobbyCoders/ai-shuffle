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
		activeSession: store({ sessionId: null as string | null }),
		setSessionActive: vi.fn(),
		deleteSession: vi.fn(),
		renameSession: vi.fn(),
		loadSessions: vi.fn(),
		forkSession: vi.fn(),
		newChat: vi.fn(),
		setTitle: vi.fn(),
		toastPush: vi.fn(),
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

vi.mock('$lib/stores/toast', () => ({
	toast: { push: mocks.toastPush },
}));

import SessionCard from './SessionCard.svelte';

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
	mocks.setSessionActive.mockResolvedValue(undefined);
	mocks.deleteSession.mockResolvedValue(undefined);
	mocks.renameSession.mockResolvedValue(undefined);
	mocks.loadSessions.mockResolvedValue(undefined);
	mocks.forkSession.mockResolvedValue({ session_id: 'forked-session' });
});

afterEach(() => cleanup());

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
		const { container } = renderCard({ is_active: false }, { compact: true });

		expect(container.querySelector('.scard.compact')).toBeTruthy();
		expect(container.querySelector('.ctitle')?.textContent).toContain('Sidebar work');
		// The meta line keeps project · device · branch on its own row.
		const meta = container.querySelector('.l3');
		expect(meta).toBeTruthy();
		expect(meta?.textContent).toContain('Project A');
		expect(meta?.textContent).toContain('This device');
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
		vi.mocked(window.matchMedia).mockImplementation(() => ({
			matches: true,
			addEventListener: vi.fn(),
			removeEventListener: vi.fn(),
		}) as unknown as MediaQueryList);
		const { container } = renderCard({}, { openActionsId: null, onActionsOpenChange });
		const frame = container.querySelector('.scard-frame') as HTMLElement;
		const card = container.querySelector('.scard') as HTMLElement;

		await fireEvent.pointerDown(card, { pointerId: 1, isPrimary: true, button: 0, clientX: 260, clientY: 20 });
		await fireEvent.pointerMove(frame, { pointerId: 1, isPrimary: true, clientX: 180, clientY: 22 });
		await fireEvent.pointerUp(frame, { pointerId: 1, isPrimary: true, button: 0, clientX: 180, clientY: 22 });

		expect(onActionsOpenChange).toHaveBeenCalledWith('session-a');
	});

	it('keeps mobile actions hidden until a left swipe reveals them', async () => {
		vi.mocked(window.matchMedia).mockImplementation(() => ({
			matches: true,
			addEventListener: vi.fn(),
			removeEventListener: vi.fn(),
		}) as unknown as MediaQueryList);
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
		vi.mocked(window.matchMedia).mockImplementation(() => ({
			matches: true,
			addEventListener: vi.fn(),
			removeEventListener: vi.fn(),
		}) as unknown as MediaQueryList);
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

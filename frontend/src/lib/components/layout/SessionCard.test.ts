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

function renderCard(overrides: Record<string, unknown> = {}) {
	const onOpen = vi.fn();
	const row = session(overrides);
	const result = render(SessionCard, {
		props: {
			session: row as never,
			status: row.is_active ? 'done' : 'idle',
			projectLabel: 'Project A',
			active: false,
			now: Date.parse('2026-08-20T12:06:00Z'),
			onOpen,
		},
	});
	return { onOpen, ...result };
}

beforeEach(() => {
	vi.clearAllMocks();
	mocks.activeSession.set({ sessionId: null });
	mocks.setSessionActive.mockResolvedValue(undefined);
	mocks.deleteSession.mockResolvedValue(undefined);
	mocks.renameSession.mockResolvedValue(undefined);
	mocks.loadSessions.mockResolvedValue(undefined);
	mocks.forkSession.mockResolvedValue({ session_id: 'forked-session' });
});

afterEach(() => cleanup());

describe('SessionCard actions', () => {
	it('restores settle, rename, fork, and delete controls', () => {
		renderCard();

		expect(screen.getByTitle('Settle session')).toBeTruthy();
		expect(screen.getByTitle('Rename session')).toBeTruthy();
		expect(screen.getByTitle('Fork session')).toBeTruthy();
		expect(screen.getByTitle('Delete session')).toBeTruthy();
	});

	it('settles an active session without opening it', async () => {
		const { onOpen } = renderCard();

		await fireEvent.click(screen.getByTitle('Settle session'));

		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', false);
		expect(onOpen).not.toHaveBeenCalled();
	});

	it('moves a settled session back to Active', async () => {
		renderCard({ is_active: false });

		await fireEvent.click(screen.getByTitle('Move to Active'));

		expect(mocks.setSessionActive).toHaveBeenCalledWith('session-a', true);
	});

	it('requires confirmation before deleting and resets the open chat in place', async () => {
		mocks.activeSession.set({ sessionId: 'session-a' });
		const { onOpen } = renderCard();

		await fireEvent.click(screen.getByTitle('Delete session'));
		expect(mocks.deleteSession).not.toHaveBeenCalled();

		await fireEvent.click(screen.getByTitle('Confirm delete'));

		expect(mocks.deleteSession).toHaveBeenCalledWith('session-a');
		await vi.waitFor(() => expect(mocks.newChat).toHaveBeenCalledOnce());
		expect(onOpen).not.toHaveBeenCalled();
	});

	it('forks the selected session and opens the fork', async () => {
		const { onOpen } = renderCard();

		await fireEvent.click(screen.getByTitle('Fork session'));

		expect(mocks.forkSession).toHaveBeenCalledWith('session-a');
		await vi.waitFor(() => expect(onOpen).toHaveBeenCalledWith('forked-session'));
		expect(onOpen).not.toHaveBeenCalledWith('session-a');
	});
});

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
		sessions: store<unknown[]>([]),
		projects: store<unknown[]>([]),
		activeSession: store({ sessionId: null as string | null, isStreaming: false }),
		streamingSessions: store(new Set<string>()),
		backgroundPermissions: store(new Set<string>()),
		activeView: store<unknown>(null),
	};
});

vi.mock('$lib/stores/app-data.svelte', () => ({
	sessions: mocks.sessions,
	projects: mocks.projects,
	appData: {
		setSessionActive: vi.fn(),
		deleteSession: vi.fn(),
		renameSession: vi.fn(),
		loadSessions: vi.fn(),
	},
}));

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

vi.mock('$lib/stores/toast', () => ({
	toast: { push: vi.fn() },
}));

import Sidebar from './Sidebar.svelte';

function session(id: string, title: string, projectId: string, isActive: boolean, hidden = false) {
	return {
		id,
		profile_id: 'default',
		project_id: projectId,
		project_name: projectId === 'visible-project' ? 'Visible Project' : 'Hidden Project',
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
	};
}

beforeEach(() => {
	localStorage.clear();
	mocks.activeSession.set({ sessionId: null, isStreaming: false });
	mocks.streamingSessions.set(new Set());
	mocks.backgroundPermissions.set(new Set());
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

afterEach(() => cleanup());

describe('Sidebar session groups', () => {
	it('groups sessions into Active and Settled by is_active', () => {
		render(Sidebar, {
			props: {
				onNewSession: vi.fn(),
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
				onOpenSession: vi.fn(),
				onOpenFeature: vi.fn(),
			},
		});

		expect(screen.queryByText('Hidden worker')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: /All projects/ }));
		expect(screen.getByRole('menuitem', { name: /Visible Project/ })).toBeTruthy();
		expect(screen.queryByRole('menuitem', { name: /Hidden Project/ })).toBeNull();
	});
});

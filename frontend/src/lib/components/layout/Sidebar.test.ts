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
		clusterProjects: store<unknown[]>([]),
		activeSession: store({ sessionId: null as string | null, isStreaming: false }),
		streamingSessions: store(new Set<string>()),
		backgroundPermissions: store(new Set<string>()),
		activeView: store<unknown>(null),
	};
});

vi.mock('$lib/stores/app-data.svelte', () => ({
	sessions: mocks.sessions,
	projects: mocks.projects,
	clusterProjects: mocks.clusterProjects,
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

const sidebarProps = {
	onSettledOpenSession: vi.fn(),
	onNewSession: vi.fn(),
	onOpenSession: vi.fn(),
	onOpenFeature: vi.fn(),
};

beforeEach(() => {
	localStorage.clear();
	mocks.activeSession.set({ sessionId: null, isStreaming: false });
	mocks.streamingSessions.set(new Set());
	mocks.backgroundPermissions.set(new Set());
	mocks.clusterProjects.set([]);
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
				onSettledOpenSession: vi.fn(),
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
				onSettledOpenSession: vi.fn(),
				onOpenSession: vi.fn(),
				onOpenFeature: vi.fn(),
			},
		});

		expect(screen.queryByText('Hidden worker')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: /All projects/ }));
		expect(screen.getByRole('menuitem', { name: /Visible Project/ })).toBeTruthy();
		expect(screen.queryByRole('menuitem', { name: /Hidden Project/ })).toBeNull();
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
		await fireEvent.click(screen.getByRole('button', { name: /All projects/ }));
		expect(screen.getByRole('menuitem', { name: /Visible Project/ })).toBeTruthy();
		expect(screen.queryByRole('menuitem', { name: /Deleted Project/ })).toBeNull();
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
		await fireEvent.click(screen.getByRole('button', { name: /All projects/ }));
		expect(screen.getByRole('menuitem', { name: /Other/ })).toBeTruthy();
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
		await fireEvent.click(screen.getByRole('button', { name: /All devices/ }));
		expect(screen.getByRole('menuitem', { name: /This device/ })).toBeTruthy();
		expect(screen.getByRole('menuitem', { name: /Laptop/ })).toBeTruthy();
		expect(screen.queryByRole('menuitem', { name: /Unknown device/ })).toBeNull();
	});

	it('marks the project ghost row active once a project filter is applied', async () => {
		render(Sidebar, { props: sidebarProps });

		expect(document.querySelector('.ghostrow.active')).toBeNull();

		await fireEvent.click(screen.getByRole('button', { name: /All projects/ }));
		await fireEvent.click(screen.getByRole('menuitem', { name: /Visible Project/ }));

		const active = document.querySelector('.ghostrow.active');
		expect(active).toBeTruthy();
		expect(active?.textContent).toContain('Visible Project');
	});
});

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
		activeSession: store({ sessionId: null as string | null, title: '' }),
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

import SessionTitleMenu from './SessionTitleMenu.svelte';
import { openHeaderDropdown } from '$lib/stores/header-dropdowns';

function renderMenu(props: { title?: string; sessionId?: string | null; variant?: 'crumb' | 'mobile' } = {}) {
	return render(SessionTitleMenu, {
		props: {
			title: props.title ?? 'Design review',
			sessionId: props.sessionId === undefined ? 'session-a' : props.sessionId,
			variant: props.variant,
		},
	});
}

async function openMenu(name = 'Design review') {
	await fireEvent.click(screen.getByRole('button', { name }));
}

beforeEach(() => {
	vi.clearAllMocks();
	openHeaderDropdown.set(null);
	mocks.activeSession.set({ sessionId: null, title: '' });
	mocks.deleteSession.mockResolvedValue(undefined);
	mocks.renameSession.mockResolvedValue(undefined);
	mocks.loadSessions.mockResolvedValue(undefined);
	mocks.forkSession.mockResolvedValue({ session_id: 'forked-session' });
});

afterEach(() => {
	openHeaderDropdown.set(null);
	cleanup();
});

describe('SessionTitleMenu', () => {
	it('opens rename, fork, and delete actions for a bound session', async () => {
		renderMenu();

		const trigger = screen.getByRole('button', { name: 'Design review' });
		expect(trigger.getAttribute('aria-haspopup')).toBe('menu');

		await openMenu();

		expect(screen.getByRole('menuitem', { name: 'Rename session' })).toBeTruthy();
		expect(screen.getByRole('menuitem', { name: 'Fork session' })).toBeTruthy();
		expect(screen.getByRole('menuitem', { name: 'Delete' })).toBeTruthy();
	});

	it('renders a non-interactive title when unbound', () => {
		renderMenu({ sessionId: null, title: 'New Chat' });

		expect(screen.queryByRole('button', { name: 'New Chat' })).toBeNull();
		expect(screen.queryByRole('menuitem')).toBeNull();
		expect(screen.getByText('New Chat')).toBeTruthy();
	});

	it('renames in place from the menu', async () => {
		mocks.activeSession.set({ sessionId: 'session-a', title: 'Design review' });
		renderMenu();

		await openMenu();
		await fireEvent.click(screen.getByRole('menuitem', { name: 'Rename session' }));

		const input = screen.getByLabelText('Rename session') as HTMLInputElement;
		expect(input.value).toBe('Design review');
		await fireEvent.input(input, { target: { value: 'Renamed chat' } });
		await fireEvent.keyDown(input, { key: 'Enter' });

		expect(mocks.renameSession).toHaveBeenCalledWith('session-a', 'Renamed chat');
		expect(mocks.setTitle).toHaveBeenCalledWith('session-a', 'Renamed chat');
	});

	it('forks the session and dispatches open-session', async () => {
		const opened: string[] = [];
		const onOpen = (event: Event) => {
			opened.push((event as CustomEvent<{ sessionId: string }>).detail.sessionId);
		};
		window.addEventListener('open-session', onOpen);
		renderMenu();

		await openMenu();
		await fireEvent.click(screen.getByRole('menuitem', { name: 'Fork session' }));

		expect(mocks.forkSession).toHaveBeenCalledWith('session-a');
		await vi.waitFor(() => expect(opened).toEqual(['forked-session']));
		expect(mocks.loadSessions).toHaveBeenCalledOnce();
		expect(mocks.toastPush).toHaveBeenCalledWith('Session forked', { tone: 'success' });
		window.removeEventListener('open-session', onOpen);
	});

	it('deletes after ConfirmDialog confirm and resets the open chat', async () => {
		mocks.activeSession.set({ sessionId: 'session-a', title: 'Design review' });
		renderMenu();

		await openMenu();
		await fireEvent.click(screen.getByRole('menuitem', { name: 'Delete' }));

		expect(screen.getByRole('dialog')).toBeTruthy();
		expect(screen.getByRole('heading', { name: 'Delete session' })).toBeTruthy();
		expect(mocks.deleteSession).not.toHaveBeenCalled();

		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

		expect(mocks.deleteSession).toHaveBeenCalledWith('session-a');
		await vi.waitFor(() => expect(mocks.newChat).toHaveBeenCalledOnce());
	});

	it('does not delete when ConfirmDialog is cancelled', async () => {
		renderMenu();

		await openMenu();
		await fireEvent.click(screen.getByRole('menuitem', { name: 'Delete' }));
		expect(screen.getByRole('dialog')).toBeTruthy();

		await fireEvent.click(screen.getAllByRole('button', { name: 'Cancel' })[0]);

		expect(mocks.deleteSession).not.toHaveBeenCalled();
		expect(screen.queryByRole('dialog')).toBeNull();
	});
});

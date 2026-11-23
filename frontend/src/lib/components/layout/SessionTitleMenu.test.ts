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
		auth: store({ status: { is_admin: true } as { is_admin: boolean } | null }),
		sessions: store([] as Array<Record<string, unknown>>),
		workerInstalls: store(null as Array<Record<string, unknown>> | null),
		refreshWorkerPresence: vi.fn(),
	};
});

vi.mock('$lib/stores/auth.svelte', () => ({ auth: mocks.auth }));

vi.mock('$lib/stores/app-data.svelte', () => ({
	appData: {
		deleteSession: mocks.deleteSession,
		renameSession: mocks.renameSession,
		loadSessions: mocks.loadSessions,
	},
	sessions: mocks.sessions,
}));

vi.mock('$lib/stores/worker-presence', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/worker-presence')>()),
	workerInstalls: mocks.workerInstalls,
	refreshWorkerPresence: mocks.refreshWorkerPresence,
}));

vi.mock('$lib/stores/active-session', () => ({
	activeSession: Object.assign(mocks.activeSession, {
		newChat: mocks.newChat,
		setTitle: mocks.setTitle,
	}),
}));

vi.mock('$lib/api/client', () => ({
	forkSession: mocks.forkSession,
	getErrorMessage: (e: unknown, fallback: string) =>
		e && typeof e === 'object' && 'detail' in e ? String((e as { detail: unknown }).detail) : fallback,
}));

vi.mock('$lib/stores/notifications', () => ({
	notify: mocks.toastPush,
}));

import SessionTitleMenu from './SessionTitleMenu.svelte';
import { openHeaderDropdown } from '$lib/stores/header-dropdowns';

const flush = () => new Promise((r) => setTimeout(r, 0));

function renderMenu(props: { title?: string; sessionId?: string | null; variant?: 'crumb' | 'sheet'; projectLabel?: string } = {}) {
	return render(SessionTitleMenu, {
		props: {
			title: props.title ?? 'Design review',
			sessionId: props.sessionId === undefined ? 'session-a' : props.sessionId,
			variant: props.variant,
			projectLabel: props.projectLabel,
		},
	});
}

async function openMenu(name = 'Design review') {
	await fireEvent.click(screen.getByRole('button', { name }));
}

beforeEach(() => {
	vi.clearAllMocks();
	openHeaderDropdown.set(null);
	mocks.auth.set({ status: { is_admin: true } });
	mocks.activeSession.set({ sessionId: null, title: '' });
	mocks.deleteSession.mockResolvedValue(undefined);
	mocks.renameSession.mockResolvedValue(undefined);
	mocks.loadSessions.mockResolvedValue(undefined);
	mocks.forkSession.mockResolvedValue({ session_id: 'forked-session' });
	mocks.sessions.set([]);
	mocks.workerInstalls.set(null);
	mocks.refreshWorkerPresence.mockResolvedValue(undefined);
});

const INSTALLS = [
	{ id: 'dev-self', device_name: 'Home server', is_self: true, online: true, approved: true },
	{ id: 'dev-workstation', device_name: 'Workstation', is_self: false, online: true, approved: true },
	{ id: 'dev-laptop', device_name: 'Laptop', is_self: false, online: false, approved: true },
];

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
		expect(mocks.toastPush).toHaveBeenCalledWith('Chat forked', { severity: 'success' });
		window.removeEventListener('open-session', onOpen);
	});

	it("shows the server's reason when a fork fails", async () => {
		mocks.forkSession.mockRejectedValue({
			status: 503,
			detail: 'Home server is offline. Forking runs on the worker that runs this chat; try again once it is reachable.',
		});
		renderMenu();

		await openMenu();
		await fireEvent.click(screen.getByRole('menuitem', { name: 'Fork session' }));

		await vi.waitFor(() =>
			expect(mocks.toastPush).toHaveBeenCalledWith(
				'Home server is offline. Forking runs on the worker that runs this chat; try again once it is reachable.',
				{ severity: 'error' },
			),
		);
		expect(mocks.loadSessions).not.toHaveBeenCalled();
	});

	describe('fork to another worker', () => {
		it('is not offered when no other worker is known', async () => {
			mocks.workerInstalls.set([INSTALLS[0]]);
			renderMenu();
			await openMenu();
			expect(screen.queryByRole('menuitem', { name: 'Fork to another worker…' })).toBeNull();
		});

		it('lists the other workers, greys an offline one, and forks to the one picked', async () => {
			mocks.workerInstalls.set(INSTALLS);
			mocks.sessions.set([{ id: 'session-a', owner_device_id: 'dev-self', is_remote: false }]);
			mocks.forkSession.mockResolvedValue({ session_id: 'forked-session', branch: 'chore/fork-abc' });
			const opened: string[] = [];
			const onOpen = (event: Event) => opened.push((event as CustomEvent<{ sessionId: string }>).detail.sessionId);
			window.addEventListener('open-session', onOpen);
			renderMenu();

			await openMenu();
			await fireEvent.click(screen.getByRole('menuitem', { name: 'Fork to another worker…' }));

			// The click swaps the menu to the worker list instead of closing it.
			expect(screen.queryByRole('menuitem', { name: 'Fork session' })).toBeNull();
			expect(screen.queryByRole('menuitem', { name: /Home server/ })).toBeNull();
			const laptop = screen.getByRole('menuitem', { name: /Laptop/ }) as HTMLButtonElement;
			expect(laptop.disabled).toBe(true);
			expect(laptop.textContent).toContain('offline');

			await fireEvent.click(screen.getByRole('menuitem', { name: /Workstation/ }));
			expect(mocks.forkSession).toHaveBeenCalledWith('session-a', undefined, 'dev-workstation');
			await vi.waitFor(() => expect(opened).toEqual(['forked-session']));
			expect(mocks.toastPush).toHaveBeenCalledWith('Chat forked to Workstation', { severity: 'success' });
			window.removeEventListener('open-session', onOpen);
		});

		it('reloads worker presence each time the menu opens', async () => {
			mocks.workerInstalls.set(INSTALLS);
			renderMenu();
			await openMenu();
			expect(mocks.refreshWorkerPresence).toHaveBeenCalledTimes(1);
			await fireEvent.keyDown(window, { key: 'Escape' });
			await openMenu();
			expect(mocks.refreshWorkerPresence).toHaveBeenCalledTimes(2);
		});

		it('offers this worker for a chat another worker runs', async () => {
			mocks.workerInstalls.set(INSTALLS);
			mocks.sessions.set([{ id: 'session-a', owner_device_id: 'dev-workstation', is_remote: true }]);
			renderMenu();

			await openMenu();
			await fireEvent.click(screen.getByRole('menuitem', { name: 'Fork to another worker…' }));
			expect(screen.getByRole('menuitem', { name: /Home server/ })).toBeTruthy();
			expect(screen.queryByRole('menuitem', { name: /Workstation/ })).toBeNull();
		});

		it('says so when the fork was made but could not move', async () => {
			mocks.workerInstalls.set(INSTALLS);
			mocks.forkSession.mockResolvedValue({
				session_id: 'forked-session',
				move_error: "This session's project isn't checked out on Workstation yet.",
			});
			renderMenu();

			await openMenu();
			await fireEvent.click(screen.getByRole('menuitem', { name: 'Fork to another worker…' }));
			await fireEvent.click(screen.getByRole('menuitem', { name: /Workstation/ }));
			await vi.waitFor(() =>
				expect(mocks.toastPush).toHaveBeenCalledWith(
					"Chat forked, but it could not move to Workstation: This session's project isn't checked out on Workstation yet.",
					{ severity: 'warning' },
				),
			);
		});

		it('works from the phone sheet too', async () => {
			mocks.workerInstalls.set(INSTALLS);
			renderMenu({ variant: 'sheet' });
			await fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }));
			await fireEvent.click(screen.getByRole('button', { name: 'Fork to another worker…' }));
			await fireEvent.click(screen.getByRole('button', { name: /Workstation/ }));
			expect(mocks.forkSession).toHaveBeenCalledWith('session-a', undefined, 'dev-workstation');
		});
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

	describe('sheet variant (phone one-bar)', () => {
		const longTitle = 'Investigate why the Android worker bar flickers when the drawer opens on DeX';

		it('renders only a chevron, nothing for an unbound chat', () => {
			const view = renderMenu({ variant: 'sheet', title: longTitle });
			const chevron = screen.getByRole('button', { name: 'Chat actions' });
			expect(chevron.getAttribute('aria-haspopup')).toBe('dialog');
			expect(chevron.getAttribute('aria-expanded')).toBe('false');
			expect(screen.queryByText(longTitle)).toBeNull();
			view.unmount();
			renderMenu({ variant: 'sheet', sessionId: null, title: 'New Chat' });
			expect(screen.queryByRole('button')).toBeNull();
			expect(screen.queryByText('New Chat')).toBeNull();
		});

		it('opens a sheet with the project, the whole title and the actions', async () => {
			renderMenu({ variant: 'sheet', title: longTitle, projectLabel: 'AI Shuffle' });
			await fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }));
			const sheet = screen.getByRole('dialog', { name: 'Chat actions' });
			expect(sheet.parentElement?.parentElement).toBe(document.body);
			expect(sheet.textContent).toContain('AI Shuffle');
			expect(sheet.textContent).toContain(longTitle);
			expect(screen.getByRole('button', { name: 'Delete' }).querySelector('svg')).toBeNull();
			for (const name of ['Rename session', 'Fork session', 'Delete']) {
				expect(screen.getByRole('button', { name })).toBeTruthy();
			}
			expect(screen.getByRole('button', { name: 'Chat actions' }).getAttribute('aria-expanded')).toBe('true');
			await fireEvent.click(screen.getByRole('button', { name: 'Close chat actions' }));
			expect(screen.queryByRole('dialog')).toBeNull();
		});

		it('closes on Escape and from the scrim', async () => {
			renderMenu({ variant: 'sheet' });
			await fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }));
			await fireEvent.keyDown(window, { key: 'Escape' });
			expect(screen.queryByRole('dialog')).toBeNull();
			await fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }));
			await fireEvent.click(document.querySelector('.stm-scrim')!);
			expect(screen.queryByRole('dialog')).toBeNull();
		});

		it('renames inside the sheet and closes it on save', async () => {
			mocks.activeSession.set({ sessionId: 'session-a', title: 'Design review' });
			renderMenu({ variant: 'sheet' });
			await fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }));
			await fireEvent.click(screen.getByRole('button', { name: 'Rename session' }));
			expect(screen.getByRole('dialog')).toBeTruthy();
			const input = screen.getByLabelText('Rename session') as HTMLInputElement;
			expect(input.value).toBe('Design review');
			await fireEvent.input(input, { target: { value: 'Renamed chat' } });
			await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
			expect(mocks.renameSession).toHaveBeenCalledWith('session-a', 'Renamed chat');
			expect(screen.queryByRole('dialog')).toBeNull();
		});

		it('forks and deletes from the sheet', async () => {
			mocks.activeSession.set({ sessionId: 'session-a', title: 'Design review' });
			renderMenu({ variant: 'sheet' });
			await fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }));
			await fireEvent.click(screen.getByRole('button', { name: 'Fork session' }));
			expect(mocks.forkSession).toHaveBeenCalledWith('session-a');
			await fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }));
			await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
			expect(screen.getByRole('heading', { name: 'Delete session' })).toBeTruthy();
			await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
			expect(mocks.deleteSession).toHaveBeenCalledWith('session-a');
		});

		it('traps keyboard focus inside the sheet and returns it to the chevron on Escape', async () => {
			renderMenu({ variant: 'sheet', title: longTitle });
			const chevron = screen.getByRole('button', { name: 'Chat actions' });
			chevron.focus();
			await fireEvent.click(chevron);
			// The trap focuses the first action on a microtask.
			await Promise.resolve();
			await Promise.resolve();
			expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Rename session' }));

			const close = screen.getByRole('button', { name: 'Close chat actions' });
			const del = screen.getByRole('button', { name: 'Delete' });

			// Tab past the last action wraps to the first, not to the page behind.
			del.focus();
			await fireEvent.keyDown(del, { key: 'Tab' });
			expect(document.activeElement).toBe(close);

			// Shift+Tab before the first action wraps to the last.
			await fireEvent.keyDown(close, { key: 'Tab', shiftKey: true });
			expect(document.activeElement).toBe(del);

			await fireEvent.keyDown(window, { key: 'Escape' });
			expect(screen.queryByRole('dialog')).toBeNull();
			expect(document.activeElement).toBe(chevron);
		});
	});
	describe('every bound chat', () => {
		for (const variant of ['crumb', 'sheet'] as const) {
			const actionable = () =>
				variant === 'crumb'
					? screen.queryByRole('button', { name: 'Design review' })
					: screen.queryByRole('button', { name: 'Chat actions' });

			it(`${variant}: offers its actions at once, whoever is signed in`, async () => {
				mocks.auth.set({ status: null });
				renderMenu({ variant });
				await flush();
				expect(actionable()).toBeTruthy();
			});
		}
	});
});

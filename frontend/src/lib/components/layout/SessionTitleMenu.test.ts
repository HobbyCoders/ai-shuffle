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
		activeSession: store<{ sessionId: string | null; title: string; messages?: unknown[] }>({ sessionId: null, title: '' }),
		exportSession: vi.fn(),
		deleteSession: vi.fn(),
		renameSession: vi.fn(),
		loadSessions: vi.fn(),
		forkSession: vi.fn(),
		newChat: vi.fn(),
		setTitle: vi.fn(),
		toastPush: vi.fn(),
		auth: store({ status: { is_admin: true } as { is_admin: boolean } | null }),
	};
});

vi.mock('$lib/stores/auth.svelte', () => ({ auth: mocks.auth }));

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
	getErrorMessage: (e: unknown, fallback: string) =>
		e && typeof e === 'object' && 'detail' in e ? String((e as { detail: unknown }).detail) : fallback,
}));

vi.mock('$lib/api/sessions', () => ({ exportSession: mocks.exportSession }));

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
		for (const name of ['Rename session', 'Fork session', 'Delete']) {
			expect(screen.getByRole('menuitem', { name }).querySelector('svg')).toBeTruthy();
		}
		// No transcript loaded for this chat: nothing to export yet.
		expect(screen.queryByRole('menuitem', { name: 'Export for sharing (.md)' })).toBeNull();
	});

	it('carries both exports as menu items once the chat has messages, and closes on start', async () => {
		mocks.activeSession.set({ sessionId: 'session-a', title: 'Design review', messages: [{ role: 'user', content: 'hi' }] });
		renderMenu();

		await openMenu();
		const menu = screen.getByRole('menu', { name: 'Session actions' });
		expect(menu.contains(screen.getByRole('menuitem', { name: 'Download private backup (.jsonl)' }))).toBe(true);
		await fireEvent.click(screen.getByRole('menuitem', { name: 'Export for sharing (.md)' }));

		expect(mocks.exportSession).toHaveBeenCalledExactlyOnceWith('session-a', 'markdown', { shareSafe: true });
		expect(screen.queryByRole('menu')).toBeNull();
	});

	it('hands focus back to the title when Escape closes the menu from a row', async () => {
		mocks.activeSession.set({ sessionId: 'session-a', title: 'Design review', messages: [{ role: 'user', content: 'hi' }] });
		renderMenu();

		await openMenu();
		screen.getByRole('menuitem', { name: 'Export for sharing (.md)' }).focus();
		await fireEvent.keyDown(window, { key: 'Escape' });

		expect(screen.queryByRole('menu')).toBeNull();
		expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Design review' }));
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
			for (const name of ['Rename session', 'Fork session', 'Delete']) {
				expect(screen.getByRole('button', { name }).querySelector('svg')).toBeTruthy();
			}
			// No transcript loaded for this chat: nothing to export yet.
			expect(screen.queryByRole('button', { name: 'Export for sharing (.md)' })).toBeNull();
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

		it('carries both exports between Fork and Delete once the chat has messages', async () => {
			mocks.activeSession.set({ sessionId: 'session-a', title: 'Design review', messages: [{ role: 'user', content: 'hi' }] });
			renderMenu({ variant: 'sheet' });
			await fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }));
			const rows = Array.from(screen.getByRole('group', { name: 'Session actions' }).querySelectorAll('button'));
			expect(rows.map((b) => b.getAttribute('aria-label') ?? b.textContent?.trim())).toEqual([
				'Rename session', 'Fork session', 'Download private backup (.jsonl)', 'Export for sharing (.md)', 'Delete',
			]);
			await fireEvent.click(screen.getByRole('button', { name: 'Export for sharing (.md)' }));
			expect(mocks.exportSession).toHaveBeenCalledExactlyOnceWith('session-a', 'markdown', { shareSafe: true });
			expect(screen.queryByRole('dialog')).toBeNull();
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

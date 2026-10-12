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
		getAssistant: vi.fn(),
	};
});

vi.mock('$lib/stores/auth.svelte', () => ({ auth: mocks.auth }));

vi.mock('$lib/api/assistant', () => ({
	getAssistant: mocks.getAssistant,
	getAgentIdentities: () => Promise.resolve([]),
}));

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

vi.mock('$lib/stores/notifications', () => ({
	notify: mocks.toastPush,
}));

import SessionTitleMenu from './SessionTitleMenu.svelte';
import { openHeaderDropdown } from '$lib/stores/header-dropdowns';
import { assistantState, acceptAssistant } from '$lib/stores/assistant';
import type { AssistantState } from '$lib/api/assistant';

/** Only the home id matters to the title menu; a loaded state with no home
 *  makes every chat ordinary. */
function setAssistantHome(id: string | null) {
	acceptAssistant({ home_session_id: id } as AssistantState);
}

function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (reason: unknown) => void;
	const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
	return { promise, resolve, reject };
}

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
	setAssistantHome(null);
	mocks.activeSession.set({ sessionId: null, title: '' });
	mocks.deleteSession.mockResolvedValue(undefined);
	mocks.renameSession.mockResolvedValue(undefined);
	mocks.loadSessions.mockResolvedValue(undefined);
	mocks.forkSession.mockResolvedValue({ session_id: 'forked-session' });
});

afterEach(() => {
	openHeaderDropdown.set(null);
	assistantState.set(null);
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
		expect(mocks.toastPush).toHaveBeenCalledWith('Session forked', { severity: 'success' });
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
	describe('assistant home chat (built in)', () => {
		it('crumb: renders a plain title with no menu', async () => {
			setAssistantHome('session-a');
			renderMenu({ title: 'Assistant' });

			expect(screen.queryByRole('button', { name: 'Assistant' })).toBeNull();
			expect(screen.getByText('Assistant').tagName).toBe('SPAN');
			// Even a stale open request shows no actions.
			openHeaderDropdown.set('session-title');
			await Promise.resolve();
			expect(screen.queryByRole('menu')).toBeNull();
			expect(screen.queryByRole('menuitem')).toBeNull();
		});

		it('sheet: renders no chevron and no sheet', () => {
			setAssistantHome('session-a');
			renderMenu({ variant: 'sheet', title: 'Assistant' });

			expect(screen.queryByRole('button', { name: 'Chat actions' })).toBeNull();
			expect(screen.queryByRole('dialog')).toBeNull();
		});

		it('closes an open menu if the chat becomes the assistant home', async () => {
			renderMenu();
			await openMenu();
			expect(screen.getByRole('menuitem', { name: 'Delete' })).toBeTruthy();

			setAssistantHome('session-a');
			await Promise.resolve();

			expect(screen.queryByRole('menuitem')).toBeNull();
			expect(screen.queryByRole('button', { name: 'Design review' })).toBeNull();
		});

		for (const variant of ['crumb', 'sheet'] as const) {
			const actionable = () =>
				variant === 'crumb'
					? screen.queryByRole('button', { name: 'Design review' })
					: screen.queryByRole('button', { name: 'Chat actions' });

			it(`${variant}: offers no actions while the assistant state is loading, then returns them for an ordinary chat`, async () => {
				assistantState.set(null);
				const load = deferred<AssistantState>();
				mocks.getAssistant.mockReturnValue(load.promise);
				renderMenu({ variant });
				await flush();

				expect(mocks.getAssistant).toHaveBeenCalled();
				expect(actionable()).toBeNull();
				openHeaderDropdown.set('session-title');
				await flush();
				expect(screen.queryByRole('menuitem')).toBeNull();
				expect(screen.queryByRole('dialog')).toBeNull();
				expect(document.body.textContent).not.toMatch(/Rename session|Fork session/);
				expect(mocks.renameSession).not.toHaveBeenCalled();
				expect(mocks.forkSession).not.toHaveBeenCalled();
				expect(mocks.deleteSession).not.toHaveBeenCalled();

				load.resolve({ home_session_id: 'assistant-home' } as AssistantState);
				await flush();
				expect(actionable()).toBeTruthy();
			});

			it(`${variant}: stays closed when the assistant state load is for the home`, async () => {
				assistantState.set(null);
				const load = deferred<AssistantState>();
				mocks.getAssistant.mockReturnValue(load.promise);
				renderMenu({ variant });
				await flush();
				expect(actionable()).toBeNull();

				load.resolve({ home_session_id: 'session-a' } as AssistantState);
				await flush();
				expect(actionable()).toBeNull();
				expect(screen.queryByRole('menuitem')).toBeNull();
			});

			it(`${variant}: stays closed when the assistant state load fails`, async () => {
				assistantState.set(null);
				const load = deferred<AssistantState>();
				mocks.getAssistant.mockReturnValue(load.promise);
				renderMenu({ variant });
				await flush();

				load.reject(new Error('offline'));
				await flush();
				expect(actionable()).toBeNull();
				openHeaderDropdown.set('session-title');
				await flush();
				expect(screen.queryByRole('menuitem')).toBeNull();
				expect(screen.queryByRole('dialog')).toBeNull();
				expect(mocks.deleteSession).not.toHaveBeenCalled();
				expect(mocks.renameSession).not.toHaveBeenCalled();
				expect(mocks.forkSession).not.toHaveBeenCalled();
			});

			it(`${variant}: stays closed while the auth status is unknown`, async () => {
				assistantState.set(null);
				mocks.auth.set({ status: null });
				renderMenu({ variant });
				await flush();
				expect(actionable()).toBeNull();
				expect(mocks.getAssistant).not.toHaveBeenCalled();
			});

			it(`${variant}: a non-admin's chat keeps its actions without loading the assistant`, async () => {
				assistantState.set(null);
				mocks.auth.set({ status: { is_admin: false } });
				renderMenu({ variant });
				await flush();
				expect(actionable()).toBeTruthy();
				expect(mocks.getAssistant).not.toHaveBeenCalled();
			});
		}

		it('keeps the menu on an ordinary chat while an assistant exists', async () => {
			setAssistantHome('assistant-home');
			renderMenu();
			await openMenu();

			expect(screen.getByRole('menuitem', { name: 'Rename session' })).toBeTruthy();
			expect(screen.getByRole('menuitem', { name: 'Fork session' })).toBeTruthy();
			expect(screen.getByRole('menuitem', { name: 'Delete' })).toBeTruthy();
		});

		it('sheet: keeps the chevron on an ordinary chat while an assistant exists', () => {
			setAssistantHome('assistant-home');
			renderMenu({ variant: 'sheet' });

			expect(screen.getByRole('button', { name: 'Chat actions' })).toBeTruthy();
		});
	});
});

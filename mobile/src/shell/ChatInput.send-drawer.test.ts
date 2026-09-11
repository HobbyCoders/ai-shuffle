import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import ChatInput from '$lib/components/chat/ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { platform } from '$lib/stores/platform.svelte';
import '$lib/test-stubs/composer-dom';
import { installMobileShell } from './register';
import { api } from '$lib/api/client';
import { createMessageSender } from '$lib/stores/session/message-sender';
import type { InternalMutableState } from '$lib/stores/session/ws-message-handlers';

vi.hoisted(() => {
	// No component request may reach a real backend in these tests.
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), {
			status: 200, headers: { 'Content-Type': 'application/json' }
		});
	}));
});

// ProseMirror scrolls its selection after real DOM input; jsdom has no range layout.
Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

const prompt = 'Check that the composer drawer closes after sending.';

beforeEach(async () => {
	installMobileShell();
	platform.set('mobile');
	// A hardware keyboard still uses Enter-to-send in the mobile shell.
	vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query === '(any-pointer: fine)' })));
	sessionStorage.clear();
	activeSession.newChat();
	Object.assign(activeSession.current, {
		sessionId: 'composer-send-test', mode: 'chat', profile: 'test-profile', draft: ''
	});
	await auth.checkAuth();
});

afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
});

async function openComposer(compact = true) {
	const { container } = render(ChatInput, { props: { compact } });
	const trigger = screen.getByRole('button', { name: /^Open chat controls/ });
	await fireEvent.click(trigger);
	expect(trigger.getAttribute('aria-expanded')).toBe('true');
	expect(container.querySelector('.mobile-controls-open')).not.toBeNull();
	const editor = container.querySelector<HTMLElement>('.tiptap')!;
	editor.focus();
	editor.innerHTML = `<p>${prompt}</p>`;
	await fireEvent.input(editor, { inputType: 'insertText', data: prompt });
	await waitFor(() => expect(screen.getByRole('button', { name: 'Send' }).hasAttribute('disabled')).toBe(false));
	await tick();
	return { container, trigger, editor };
}

describe('mobile composer drawer after send', () => {
	it('closes after the real first-send worktree path adopts its new session id', async () => {
		Object.assign(activeSession.current, {
			sessionId: null, mode: 'code', project: 'drawer-project', messages: [],
			worktreeIntent: 'worktree', worktreeMode: 'new', worktreeBranch: 'drawer-branch'
		});
		const post = vi.spyOn(api, 'post').mockResolvedValue({
			session_id: 'created-by-send', branch_name: 'drawer-branch', status: 'ready'
		});
		const sendQuery = vi.fn(() => true);
		// Use the production sender to populate sessionId from the worktree POST,
		// not a mock that merely seeds the field consumed by the composer.
		const sender = createMessageSender({
			getState: () => activeSession.current,
			update: fn => {
				Object.assign(activeSession.current, fn(activeSession.current));
				activeSession.setDraft(activeSession.current.draft); // notify shared subscribers
			},
			internal: {
				pendingTools: new Set(), awaitingNewSession: false, pendingRequestId: null,
				stopRequestedBeforeSession: false, worktreeCreationGeneration: 0, worktreeCreateAbort: null
			} as InternalMutableState,
			wsConn: { isConnected: () => true, connect: vi.fn(), send: sendQuery, resetReconnectAttempts: vi.fn() },
			sessionEphemeralCache: { remove: vi.fn() },
			appData: { loadSessions: vi.fn(), loadApiUserSessions: vi.fn() },
			persistSession: vi.fn(), stopTimeout: { get: () => null, set: vi.fn() },
			getActiveSession: () => sender
		});
		vi.spyOn(activeSession, 'sendMessage').mockImplementation(sender.sendMessage);
		const { container, trigger } = await openComposer(false);
		await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
		await waitFor(() => expect(post).toHaveBeenCalledWith('/sessions/with-worktree', expect.any(Object)));
		await waitFor(() => expect(sendQuery).toHaveBeenCalledWith(expect.objectContaining({
			type: 'query', session_id: 'created-by-send', prompt
		})));
		expect(activeSession.current.sessionId).toBe('created-by-send');
		await waitFor(() => expect(trigger.getAttribute('aria-expanded')).toBe('false'));
		expect(container.querySelector('.mobile-controls-open')).toBeNull();
	});

	it('does not re-expand after closing a drawer whose control still has focus', async () => {
		let resolveSend!: (sent: boolean) => void;
		vi.spyOn(activeSession, 'sendMessage').mockReturnValue(new Promise(resolve => { resolveSend = resolve; }));
		const { container, trigger } = await openComposer();
		await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
		const control = container.querySelector<HTMLButtonElement>('.mobile-composer-controls button')!;
		control.focus();
		expect(document.activeElement).toBe(control);
		resolveSend(true);
		await waitFor(() => expect(trigger.getAttribute('aria-expanded')).toBe('false'));
		await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
		expect(container.querySelector('.mobile-collapsed')).not.toBeNull();
		expect(document.activeElement).not.toBe(trigger);
	});

	it('does not close the next new-chat drawer when an old send completes', async () => {
		let resolveSend!: (sent: boolean) => void;
		vi.spyOn(activeSession, 'sendMessage').mockReturnValue(new Promise(resolve => { resolveSend = resolve; }));
		const { trigger } = await openComposer();
		await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
		activeSession.newChat();
		await tick();
		resolveSend(true);
		await tick();
		expect(trigger.getAttribute('aria-expanded')).toBe('true');
	});


	it.each([
		['send button', true, false],
		['Enter', true, false],
		['new-chat layout', false, false],
		['send while streaming', true, true]
	] as const)('closes after a successful %s', async (action, compact, streaming) => {
		activeSession.current.isStreaming = streaming;
		const send = vi.spyOn(activeSession, 'sendMessage').mockResolvedValue(true);
		const { container, trigger, editor } = await openComposer(compact);
		if (action === 'Enter') await fireEvent.keyDown(editor, { key: 'Enter' });
		else await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
		await waitFor(() => expect(send).toHaveBeenCalledWith(prompt, undefined));
		await waitFor(() => expect(trigger.getAttribute('aria-expanded')).toBe('false'));
		expect(container.querySelector('.mobile-controls-open')).toBeNull();
		if (compact) await waitFor(() => expect(container.querySelector('.mobile-collapsed')).not.toBeNull());
		await waitFor(() => expect(document.activeElement).not.toBe(editor));
	});

	it.each(['false', 'throw'] as const)('keeps the drawer and restores the draft when send returns %s', async failure => {
		const send = vi.spyOn(activeSession, 'sendMessage');
		if (failure === 'false') send.mockResolvedValue(false);
		else {
			send.mockRejectedValue(new Error('Connection lost'));
			vi.spyOn(console, 'error').mockImplementation(() => {});
		}
		const { container, trigger, editor } = await openComposer();
		await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
		await waitFor(() => expect(send).toHaveBeenCalledOnce());
		await waitFor(() => expect(editor.textContent).toBe(prompt));
		expect(trigger.getAttribute('aria-expanded')).toBe('true');
		expect(container.querySelector('.mobile-controls-open')).not.toBeNull();
	});

	it('leaves the drawer open when a submit guard blocks sending', async () => {
		activeSession.current.profile = '';
		const send = vi.spyOn(activeSession, 'sendMessage');
		const { trigger, editor } = await openComposer();
		await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
		expect(send).not.toHaveBeenCalled();
		expect(trigger.getAttribute('aria-expanded')).toBe('true');
		expect(editor.textContent).toBe(prompt);
	});

	it('waits for send success before closing the drawer', async () => {
		let resolveSend!: (sent: boolean) => void;
		vi.spyOn(activeSession, 'sendMessage').mockReturnValue(new Promise(resolve => { resolveSend = resolve; }));
		const { trigger } = await openComposer();
		await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
		expect(trigger.getAttribute('aria-expanded')).toBe('true');
		resolveSend(true);
		await waitFor(() => expect(trigger.getAttribute('aria-expanded')).toBe('false'));
	});
});

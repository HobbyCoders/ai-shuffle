import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { readDraft } from '$lib/stores/session/draft-persistence';
import '$lib/test-stubs/composer-dom';

// #1077: Send waits on the cold-cache check (a request) before it sends. New
// chat pressed in that window used to send the message into the new chat.
const gate = vi.hoisted(() => ({ release: null as null | (() => void) }));
vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url.includes('/cache-state')) {
			await new Promise<void>((resolve) => { gate.release = resolve; });
			return new Response(JSON.stringify({ warn: false, cold: false, large: false }), { status: 200, headers: { 'Content-Type': 'application/json' } });
		}
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

const TEXT = 'meant for chat A';
let sends: MockInstance;

beforeEach(async () => {
	gate.release = null;
	localStorage.clear();
	sessionStorage.clear();
	activeSession.newChat();
	Object.assign(activeSession.current, {
		sessionId: 'chat-a', mode: 'chat', profile: 'coding', isStreaming: false, liveTurnId: null,
		error: null, worktreeError: null
	});
	activeSession.setDraft(TEXT);
	sends = vi.spyOn(activeSession, 'sendMessage').mockResolvedValue(true);
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

describe('New chat right after Send (#1077)', () => {
	it('never sends the message into the new chat; it stays as chat A\'s draft', async () => {
		const view = render(ChatInput);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(gate.release).toBeTruthy());

		// New chat while the send is still waiting on its check.
		activeSession.newChat();
		await waitFor(() => expect(editor.textContent).toBe(''));
		gate.release!();
		await new Promise((resolve) => setTimeout(resolve, 20));

		expect(sends).not.toHaveBeenCalled();
		expect(activeSession.current.sessionId).toBeNull();
		expect(editor.textContent).toBe('');
		expect(readDraft('chat-a')).toContain(TEXT);
	});

	it('still sends to chat A when nothing changed during the check', async () => {
		const view = render(ChatInput);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(gate.release).toBeTruthy());
		gate.release!();
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
		expect(sends.mock.calls[0][0]).toBe(TEXT);
	});
});

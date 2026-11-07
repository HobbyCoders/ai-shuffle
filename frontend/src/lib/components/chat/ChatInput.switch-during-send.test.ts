import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { readDraft } from '$lib/stores/session/draft-persistence';
import { notify } from '$lib/stores/notifications';
import type { Editor } from '@tiptap/core';
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

	// Review S1: the store waits for a reload's restore before it reads which
	// chat is open. New chat in that wait moved the message there.
	it('never sends into the new chat when New chat lands during the store\'s restore wait', async () => {
		const notices = vi.spyOn(notify, 'banner');
		let restored!: () => void;
		const restore = new Promise<void>((resolve) => { restored = resolve; });
		const sentTo: (string | null)[] = [];
		// What the store does: wait for the restore, then send to the open chat
		// unless the caller's destination check says no.
		sends.mockImplementation(async (_prompt: string, options?: { stillAtDestination?: () => boolean }) => {
			await restore;
			if (options?.stillAtDestination && !options.stillAtDestination()) return false;
			sentTo.push(activeSession.current.sessionId);
			return true;
		});
		const view = render(ChatInput);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(gate.release).toBeTruthy());
		gate.release!();
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());

		activeSession.newChat();
		await waitFor(() => expect(editor.textContent).toBe(''));
		restored();
		await new Promise((resolve) => setTimeout(resolve, 20));

		expect(sentTo).toEqual([]);
		expect(activeSession.current.sessionId).toBeNull();
		expect(editor.textContent).toBe('');
		expect(readDraft('chat-a')).toContain(TEXT);
		expect(notices.mock.calls.map(([message]) => message).join(' ')).toMatch(/chat changed before it went out/);
	});

	// Review S2: an edit made while the check waited is the newest draft.
	it('keeps the edits made during the wait, not the text as it was at Send', async () => {
		const view = render(ChatInput);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')! as HTMLElement & { editor: Editor };
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(gate.release).toBeTruthy());

		editor.editor.commands.focus('end');
		editor.editor.commands.insertContent(' and the correction');
		await waitFor(() => expect(editor.textContent).toBe(`${TEXT} and the correction`));
		activeSession.newChat();
		await waitFor(() => expect(editor.textContent).toBe(''));
		gate.release!();
		await new Promise((resolve) => setTimeout(resolve, 20));

		expect(sends).not.toHaveBeenCalled();
		expect(readDraft('chat-a')).toContain('and the correction');
	});

	// Review S3: an editor first mounted in New chat, later moved to chat A.
	it('leaves a New chat draft typed during the wait in place', async () => {
		activeSession.newChat();
		const view = render(ChatInput);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')! as HTMLElement & { editor: Editor };
		Object.assign(activeSession.current, { sessionId: 'chat-a', mode: 'chat', profile: 'coding', isStreaming: false });
		activeSession.setDraft(TEXT);
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(gate.release).toBeTruthy());

		activeSession.newChat();
		await waitFor(() => expect(editor.textContent).toBe(''));
		editor.editor.commands.insertContent('a different new chat');
		await waitFor(() => expect(readDraft(null)).toContain('a different new chat'));
		gate.release!();
		await new Promise((resolve) => setTimeout(resolve, 20));

		expect(sends).not.toHaveBeenCalled();
		expect(readDraft('chat-a')).toContain(TEXT);
		expect(readDraft(null)).toContain('a different new chat');
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

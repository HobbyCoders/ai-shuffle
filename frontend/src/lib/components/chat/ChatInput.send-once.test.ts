import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import '$lib/test-stubs/composer-dom';

// #858: a double click/tap on Send sends once. #864: the caret and selection
// come back with a chat's draft after switching away and back.
const heldChecks: Array<() => void> = [];
let answerCacheState: (() => void) | null = null;

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url.includes('/cache-state')) {
			// Hold the pre-send cache check open: the window a second tap hit.
			await new Promise<void>((resolve) => {
				heldChecks.push(resolve);
				answerCacheState = () => heldChecks.splice(0).forEach((release) => release());
			});
		}
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/cache-state') ? { warn: false }
				: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

let sends: MockInstance;

function openChat(sessionId: string) {
	Object.assign(activeSession.current, { sessionId });
	activeSession.setDraft('');
}

beforeEach(async () => {
	answerCacheState = null;
	heldChecks.length = 0;
	localStorage.clear();
	sessionStorage.clear();
	activeSession.newChat();
	Object.assign(activeSession.current, {
		sessionId: 'chat-a', mode: 'chat', profile: 'coding', isStreaming: false, liveTurnId: null,
		error: null, worktreeError: null
	});
	sends = vi.spyOn(activeSession, 'sendMessage').mockResolvedValue(true);
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

describe('Send activates once', () => {
	it('ignores a second Send while the first is still being checked', async () => {
		activeSession.setDraft('hello once');
		const view = render(ChatInput);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
		await waitFor(() => expect(editor.textContent).toBe('hello once'));
		const form = view.container.querySelector('form')!;
		// A desktop pointer, so Enter sends.
		vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('fine'), media: query,
			addEventListener() {}, removeEventListener() {} }));
		await fireEvent.submit(form);
		await fireEvent.submit(form);
		await fireEvent.keyDown(editor, { key: 'Enter' });
		await waitFor(() => expect(answerCacheState).not.toBeNull());
		answerCacheState!();
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
		expect(sends.mock.calls[0][0]).toBe('hello once');
		expect(editor.textContent).toBe('');
	});

	it('lets the next message go once the composer is cleared', async () => {
		let accept!: (ok: boolean) => void;
		sends.mockImplementationOnce(() => new Promise<boolean>((resolve) => { accept = resolve; }));
		activeSession.setDraft('first');
		const view = render(ChatInput);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
		await waitFor(() => expect(editor.textContent).toBe('first'));
		const form = view.container.querySelector('form')!;
		await fireEvent.submit(form);
		await waitFor(() => expect(answerCacheState).not.toBeNull());
		answerCacheState!();
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
		expect(editor.textContent).toBe('');
		// The first send is still in flight; a new message is not held behind it.
		answerCacheState = null;
		(editor as HTMLElement & { editor: import('@tiptap/core').Editor }).editor.commands.insertContent('second');
		await fireEvent.submit(form);
		await waitFor(() => expect(answerCacheState).not.toBeNull());
		answerCacheState!();
		await waitFor(() => expect(sends).toHaveBeenCalledTimes(2));
		expect(sends.mock.calls[1][0]).toBe('second');
		accept(true);
	});
});

describe('composer selection follows the chat', () => {
	it('restores the caret and a selection after switching chats', async () => {
		activeSession.setDraft('ABCDEFGHIJ');
		const view = render(ChatInput);
		const editorEl = view.container.querySelector<HTMLElement>('.tiptap')!;
		await waitFor(() => expect(editorEl.textContent).toBe('ABCDEFGHIJ'));
		const editor = (editorEl as HTMLElement & { editor: import('@tiptap/core').Editor }).editor;
		// Caret after "E": the paragraph opens at position 1.
		editor.commands.setTextSelection(6);

		openChat('chat-b');
		await tick();
		expect(editorEl.textContent).toBe('');
		editor.commands.insertContent('other');
		editor.commands.setTextSelection({ from: 2, to: 4 });

		openChat('chat-a');
		await tick();
		expect(editorEl.textContent).toBe('ABCDEFGHIJ');
		expect(editor.state.selection.from).toBe(6);
		expect(editor.state.selection.to).toBe(6);
		editor.commands.insertContent('x');
		expect(editor.getText()).toBe('ABCDExFGHIJ');

		openChat('chat-b');
		await tick();
		expect(editor.getText()).toBe('other');
		expect([editor.state.selection.from, editor.state.selection.to]).toEqual([2, 4]);
	});
});

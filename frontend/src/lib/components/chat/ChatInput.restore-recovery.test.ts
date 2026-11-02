/**
 * A reload can cut off a Restore the server already carried out. The next page
 * hands the words back once this chat's history is known, and an empty history
 * counts: the restored row may have been the chat's only one.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import '$lib/test-stubs/composer-dom';

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/cache-state') ? { cold: false, warn: false, large: false }
				: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

const NOTE_KEY = 'restore-note:chat:row-1';
const leaveNote = () => sessionStorage.setItem(NOTE_KEY, JSON.stringify({
	rowId: 'row-1', prompt: 'my stopped words', uploadPaths: [], page: 'an-earlier-page'
}));
const openChat = (state: Record<string, unknown>) => {
	Object.assign(activeSession.current, { sessionId: 'chat', mode: 'chat', profile: 'coding', messages: [], ...state });
};
const editorText = (view: ReturnType<typeof render>) => view.container.querySelector<HTMLElement>('.tiptap')!.textContent;
// Long enough for the composer to have acted on a note if it was going to.
const settle = () => new Promise((done) => setTimeout(done, 50));

beforeEach(async () => {
	localStorage.clear();
	sessionStorage.clear();
	activeSession.newChat();
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

it('hands the words back when the restored row was the only one in the chat', async () => {
	leaveNote();
	openChat({});
	const view = render(ChatInput);
	await waitFor(() => expect(editorText(view)).toBe('my stopped words'));
	expect(sessionStorage.getItem(NOTE_KEY)).toBeNull();
});

it.each([
	['still loading', { isLoadingSession: true }, { isLoadingSession: false }],
	['still restoring after a reload', { isRestoringSession: true }, { isRestoringSession: false }],
	['stalled', { historyStall: { reason: 'silent' } }, { historyStall: null }],
	['cut off by a lost connection', { wsReconnectRequired: true }, { wsReconnectRequired: false }],
	['unavailable after an error', { error: 'Connection lost. Please refresh the page.' }, { error: null }]
])('an empty history that is %s says nothing about the row, until it is confirmed', async (_name, unconfirmed, confirmed) => {
	leaveNote();
	openChat(unconfirmed);
	const view = render(ChatInput);
	await settle();
	expect(editorText(view)).toBe('');
	expect(sessionStorage.getItem(NOTE_KEY)).not.toBeNull();

	Object.assign(activeSession.current, confirmed);
	activeSession.setDraft('');
	await waitFor(() => expect(editorText(view)).toBe('my stopped words'));
	expect(sessionStorage.getItem(NOTE_KEY)).toBeNull();
});

it('leaves a note alone while its row is still on screen', async () => {
	leaveNote();
	openChat({ messages: [{ id: 'm1', role: 'user', content: 'my stopped words', metadata: { not_sent_id: 'row-1' } }] });
	const view = render(ChatInput);
	await settle();
	expect(editorText(view)).toBe('');
	expect(sessionStorage.getItem(NOTE_KEY)).not.toBeNull();
});

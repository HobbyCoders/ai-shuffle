/** #1361: New chat adopting the session its own first send created is not a
 *  navigation. What the user typed after Send stays in the editor. */
import { afterEach, beforeEach, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { auth } from '$lib/stores/auth.svelte';
import { readDraft } from '$lib/stores/session/draft-persistence';
import type { Editor } from '@tiptap/core';
import '$lib/test-stubs/composer-dom';

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
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

const FIRST = 'first message';
const TYPED = 'queued words';
let sends: MockInstance;
let settle: (sent: boolean) => void;

beforeEach(async () => {
	localStorage.clear();
	sessionStorage.clear();
	activeSession.newChat();
	Object.assign(activeSession.current, {
		sessionId: null, mode: 'chat', profile: 'coding', isStreaming: false, liveTurnId: null,
		error: null, worktreeError: null
	});
	appData.current.profiles = [{ id: 'coding', name: 'Coding' }] as never;
	activeSession.setDraft(FIRST);
	// The send is out; the server has not created the session yet.
	sends = vi.spyOn(activeSession, 'sendMessage').mockImplementation(
		() => new Promise<boolean>((resolve) => { settle = resolve; }));
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	appData.current.profiles = [];
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

/** Send the first message of a New chat, then type while it waits for its session. */
async function sendThenType() {
	const view = render(ChatInput);
	const editor = view.container.querySelector<HTMLElement>('.tiptap')! as HTMLElement & { editor: Editor };
	await waitFor(() => expect(editor.textContent).toBe(FIRST));
	await fireEvent.submit(view.container.querySelector('form')!);
	await waitFor(() => expect(sends).toHaveBeenCalledOnce());
	await waitFor(() => expect(editor.textContent).toBe(''));
	editor.editor.commands.insertContent(TYPED);
	await waitFor(() => expect(editor.textContent).toBe(TYPED));
	return editor;
}

/** What `session_created` does: the store takes the new id, the New chat's draft empty. */
function adoptCreatedSession() {
	Object.assign(activeSession.current, { sessionId: 'created-chat' });
	activeSession.setDraft('');
}

it('keeps text typed after the first Send when the new chat gets its session id', async () => {
	const editor = await sendThenType();
	const caret = editor.editor.state.selection.from;

	// Inside the draft debounce, as on CI: the typing is not saved yet.
	adoptCreatedSession();
	await new Promise((resolve) => setTimeout(resolve, 20));

	expect(editor.textContent).toBe(TYPED);
	expect(editor.editor.state.selection.from).toBe(caret);
	expect(activeSession.current.draft).toContain(TYPED);
	expect(readDraft('created-chat')).toContain(TYPED);
	// Moved, not copied: the next New chat starts empty.
	expect(readDraft(null)).toBeNull();
	settle(true);
});

it('puts a refused first message back in front of the text typed since', async () => {
	const editor = await sendThenType();
	adoptCreatedSession();
	await new Promise((resolve) => setTimeout(resolve, 20));

	settle(false);
	await waitFor(() => expect(editor.textContent).toContain(FIRST));
	expect(editor.textContent).toBe(`${FIRST}${TYPED}`);
	expect(readDraft('created-chat')).toMatch(new RegExp(`${FIRST}.*${TYPED}`));
});

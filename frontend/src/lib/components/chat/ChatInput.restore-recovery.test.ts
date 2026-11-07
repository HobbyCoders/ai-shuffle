/**
 * A reload can cut off a Restore the server already carried out. The next page
 * hands the words back once this chat's history is known, and an empty history
 * counts: the restored row may have been the chat's only one.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
const rewind = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', async (original) => ({ ...(await original<object>()), executeRewind: rewind }));
vi.mock('$lib/settings/navigation.svelte', () => ({ openSettings: vi.fn() }));
import ChatInput from './ChatInput.svelte';
import MessageActions from './MessageActions.svelte';
import { readDraft } from '$lib/stores/session/draft-persistence';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { promptStash } from '$lib/stores/session/prompt-stash';
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
const editorText = (view: { container: Element }) => view.container.querySelector<HTMLElement>('.tiptap')!.textContent;
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

it.each([
	['with a composer on screen', true],
	['with no composer on screen', false]
])('keeps both restores when their answers land together, %s', async (_name, composerShown) => {
	openChat({});
	activeSession.setDraft('');
	const composer = composerShown ? render(ChatInput) : null;
	await tick();
	const answers: Array<(result: unknown) => void> = [];
	rewind.mockImplementation(() => new Promise((resolve) => answers.push(resolve)));
	const onRewindComplete = (_warning?: string, prompt?: string, uploads?: string[]) => {
		activeSession.setPendingRestorePrompt(prompt ?? '', uploads);
	};
	const row = { sessionId: 'chat', messageRole: 'user', onRewindComplete } as const;
	render(MessageActions, { ...row, messageContent: 'first words', notSentMessageId: 'row-first' });
	render(MessageActions, { ...row, messageContent: 'second words', notSentMessageId: 'row-second' });
	const buttons = screen.getAllByRole('button', { name: 'Restore to composer' });
	await fireEvent.click(buttons[0]);
	await fireEvent.click(buttons[1]);
	expect(answers).toHaveLength(2);

	for (const answer of answers) answer({ success: true, unsent_restored: true });
	await tick();
	await tick();

	const kept = (composer ? editorText(composer) : readDraft('chat')) ?? '';
	expect(kept).toContain('first words');
	expect(kept).toContain('second words');
	expect(kept.split('first words')).toHaveLength(2);
	expect(kept.split('second words')).toHaveLength(2);
	expect(activeSession.current.pendingRestorePrompt).toBeNull();
});

describe('a rewound message cut off by a reload (#1083, review S3)', () => {
	const SENT_KEY = 'restore-note:chat:sent:0:13:abc';
	const typed = (text: string) => JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text }] }] });
	const leaveSentNote = (composerAtConfirm: string) => sessionStorage.setItem(SENT_KEY, JSON.stringify({
		rowId: 'sent:0:13:abc', prompt: 'rewound words', uploadPaths: [], page: 'an-earlier-page', composerAtConfirm
	}));
	const stashPreviews = () => { let stash: { preview: string }[] = []; promptStash.subscribe(v => { stash = v; })(); return stash.map(e => e.preview); };
	beforeEach(() => promptStash.setOwner('tester'));
	afterEach(() => promptStash.setOwner(null));

	it('keeps it apart from words typed after the rewind was confirmed', async () => {
		leaveSentNote('');
		openChat({});
		activeSession.setDraft(typed('fresh words'));
		const view = render(ChatInput);
		await waitFor(() => expect(stashPreviews()).toEqual(['rewound words']));
		expect(editorText(view)).toBe('fresh words');
		expect(sessionStorage.getItem(SENT_KEY)).toBeNull();
	});

	it('hands it back as before when nothing changed since the rewind was confirmed', async () => {
		leaveSentNote('draft from before');
		openChat({});
		activeSession.setDraft(typed('draft from before'));
		const view = render(ChatInput);
		await waitFor(() => expect(editorText(view)).toContain('rewound words'));
		expect(editorText(view)).toContain('draft from before');
		expect(stashPreviews()).toEqual([]);
	});
});

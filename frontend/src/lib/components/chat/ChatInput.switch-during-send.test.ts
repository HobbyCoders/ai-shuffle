import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { readDraft } from '$lib/stores/session/draft-persistence';
import { notify } from '$lib/stores/notifications';
import { sessionEphemeralCache } from '$lib/stores/session-cache';
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

	// Review S4: two sends waiting on the restore, then New chat. Each recovery
	// used to rebuild the draft from its own message alone, so one was lost.
	it('keeps both messages when two sends wait on the restore and New chat lands', async () => {
		let restored!: () => void;
		const restore = new Promise<void>((resolve) => { restored = resolve; });
		const sentTo: (string | null)[] = [];
		sends.mockImplementation(async (_prompt: string, options?: { stillAtDestination?: () => boolean }) => {
			await restore;
			if (options?.stillAtDestination && !options.stillAtDestination()) return false;
			sentTo.push(activeSession.current.sessionId);
			return true;
		});
		const view = render(ChatInput);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')! as HTMLElement & { editor: Editor };
		const form = view.container.querySelector('form')!;
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		await fireEvent.submit(form);
		await waitFor(() => expect(gate.release).toBeTruthy());
		gate.release!();
		await waitFor(() => expect(sends).toHaveBeenCalledTimes(1));
		await waitFor(() => expect(editor.textContent).toBe(''));

		gate.release = null;
		editor.editor.commands.insertContent('second message');
		await waitFor(() => expect(editor.textContent).toBe('second message'));
		await fireEvent.submit(form);
		await waitFor(() => expect(gate.release).toBeTruthy());
		gate.release!();
		await waitFor(() => expect(sends).toHaveBeenCalledTimes(2));
		await waitFor(() => expect(editor.textContent).toBe(''));

		editor.editor.commands.insertContent('third draft');
		await waitFor(() => expect(editor.textContent).toBe('third draft'));
		activeSession.newChat();
		await waitFor(() => expect(editor.textContent).toBe(''));
		restored();
		await new Promise((resolve) => setTimeout(resolve, 20));

		expect(sentTo).toEqual([]);
		const kept = readDraft('chat-a') ?? '';
		const at = (text: string) => kept.indexOf(text);
		expect(at(TEXT)).toBeGreaterThanOrEqual(0);
		expect(at('second message')).toBeGreaterThan(at(TEXT));
		expect(at('third draft')).toBeGreaterThan(at('second message'));
	});

	// Review S7: two sends refused in the chat that stays open (a connection
	// timeout) used to come back in reverse order.
	it('puts two refused sends back in the order they were sent', async () => {
		const results: ((sent: boolean) => void)[] = [];
		sends.mockImplementation(() => new Promise<boolean>((resolve) => { results.push(resolve); }));
		const view = render(ChatInput);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')! as HTMLElement & { editor: Editor };
		const form = view.container.querySelector('form')!;
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		await fireEvent.submit(form);
		await waitFor(() => expect(gate.release).toBeTruthy());
		gate.release!();
		await waitFor(() => expect(sends).toHaveBeenCalledTimes(1));
		await waitFor(() => expect(editor.textContent).toBe(''));

		gate.release = null;
		editor.editor.commands.insertContent('second message');
		await waitFor(() => expect(editor.textContent).toBe('second message'));
		await fireEvent.submit(form);
		await waitFor(() => expect(gate.release).toBeTruthy());
		gate.release!();
		await waitFor(() => expect(sends).toHaveBeenCalledTimes(2));
		await waitFor(() => expect(editor.textContent).toBe(''));

		editor.editor.commands.insertContent('later text');
		await waitFor(() => expect(editor.textContent).toBe('later text'));
		results[0](false);
		await waitFor(() => expect(editor.textContent).toContain(TEXT));
		results[1](false);
		await waitFor(() => expect(editor.textContent).toContain('second message'));

		const shown = editor.textContent ?? '';
		const at = (text: string) => shown.indexOf(text);
		expect(at(TEXT)).toBeGreaterThanOrEqual(0);
		expect(at('second message')).toBeGreaterThan(at(TEXT));
		expect(at('later text')).toBeGreaterThan(at('second message'));
	});

	// Review S5: emptying the composer during the wait is a choice, not a gap.
	// Send clears the composer before the check (#933), so what the user can
	// empty during the wait is text typed after Send; the called-off message,
	// which the user never removed, still comes back on its own.
	it('does not bring back text typed and deleted during the wait', async () => {
		const view = render(ChatInput);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')! as HTMLElement & { editor: Editor };
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(gate.release).toBeTruthy());
		expect(editor.textContent).toBe('');

		editor.editor.commands.insertContent('typed then deleted');
		await waitFor(() => expect(editor.textContent).toBe('typed then deleted'));
		editor.editor.commands.clearContent(true);
		await waitFor(() => expect(editor.textContent).toBe(''));
		activeSession.newChat();
		gate.release!();
		await new Promise((resolve) => setTimeout(resolve, 20));

		expect(sends).not.toHaveBeenCalled();
		const kept = readDraft('chat-a') ?? '';
		expect(kept).toContain(TEXT);
		expect(kept).not.toContain('typed then deleted');
	});

	// Review S2: an edit made while the check waited is the newest draft. The
	// composer cleared at Send (#933), so the edit is text typed after it, and
	// it is kept after the called-off message, not dropped for it.
	it('keeps the text typed during the wait after the called-off message', async () => {
		const view = render(ChatInput);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')! as HTMLElement & { editor: Editor };
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(gate.release).toBeTruthy());
		expect(editor.textContent).toBe('');

		editor.editor.commands.focus('end');
		editor.editor.commands.insertContent('and the correction');
		await waitFor(() => expect(editor.textContent).toBe('and the correction'));
		activeSession.newChat();
		await waitFor(() => expect(editor.textContent).toBe(''));
		gate.release!();
		await new Promise((resolve) => setTimeout(resolve, 20));

		expect(sends).not.toHaveBeenCalled();
		const kept = readDraft('chat-a') ?? '';
		expect(kept.indexOf(TEXT)).toBeGreaterThanOrEqual(0);
		expect(kept.indexOf('and the correction')).toBeGreaterThan(kept.indexOf(TEXT));
	});

	// PR #1212 review S2: Send, type a new draft, press New chat inside the
	// draft debounce, then reopen chat A before the check answers. The newer
	// text must come back with the called-off message, once.
	it('keeps text typed after Send when New chat and reopening chat A both land during the check', async () => {
		const view = render(ChatInput);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')! as HTMLElement & { editor: Editor };
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(gate.release).toBeTruthy());
		expect(editor.textContent).toBe('');

		editor.editor.commands.insertContent('typed after send');
		await waitFor(() => expect(editor.textContent).toBe('typed after send'));
		// Inside the 100 ms debounce: nothing has saved the new words yet.
		activeSession.newChat();
		await waitFor(() => expect(editor.textContent).toBe(''));
		// Reopen chat A the way the store does: its remembered draft comes back.
		Object.assign(activeSession.current, { sessionId: 'chat-a', mode: 'chat', profile: 'coding', isStreaming: false });
		activeSession.setDraft(sessionEphemeralCache.peekDraft('chat-a') || readDraft('chat-a') || '');
		await waitFor(() => expect(editor.textContent).toBe('typed after send'));
		gate.release!();
		await new Promise((resolve) => setTimeout(resolve, 30));

		expect(sends).not.toHaveBeenCalled();
		await waitFor(() => expect(editor.textContent).toContain(TEXT));
		const shown = editor.textContent ?? '';
		expect(shown.indexOf('typed after send')).toBeGreaterThan(shown.indexOf(TEXT));
		expect(shown.split('typed after send')).toHaveLength(2);
		expect(shown.split(TEXT)).toHaveLength(2);
		const kept = readDraft('chat-a') ?? '';
		expect(kept.split('typed after send')).toHaveLength(2);
		expect(kept.split(TEXT)).toHaveLength(2);
	});

	// PR #1212 review S4, and the one rule behind S2 and S4: while a send is
	// held, what the user last left in the composer, empty included, is the
	// chat's draft, saved with it when they leave. The held message comes back
	// in front of that draft exactly once, whether the check answers after the
	// user reopened the chat or while they were away.
	const later = (draft: string) => JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: draft }] }] });
	type Edit = { name: string; steps: (editor: Editor) => Promise<void>; left: string | null };
	const settle = () => new Promise((resolve) => setTimeout(resolve, 150));
	const edits: Edit[] = [
		{ name: 'nothing typed after Send', steps: async () => {}, left: null },
		{ name: 'typed, left inside the save delay', steps: async (e) => { e.commands.insertContent('later words'); }, left: 'later words' },
		{ name: 'typed and saved', steps: async (e) => { e.commands.insertContent('later words'); await settle(); }, left: 'later words' },
		{ name: 'typed, saved, then deleted', steps: async (e) => {
			e.commands.insertContent('later-deleted-draft');
			await settle();
			expect(readDraft('chat-a')).toContain('later-deleted-draft');
			e.commands.clearContent(true);
		}, left: null },
		{ name: 'typed, saved, then replaced', steps: async (e) => {
			e.commands.insertContent('first try');
			await settle();
			e.commands.setContent(later('second try'), { emitUpdate: true });
		}, left: 'second try' },
	];
	const forgotten = ['later-deleted-draft', 'first try'];
	describe.each([
		['the check answers after chat A is reopened', true],
		['the check answers while New chat is open', false],
	])('%s', (_when, reopenFirst) => {
		it.each(edits)('keeps exactly what was left: $name', async ({ steps, left }) => {
			const view = render(ChatInput);
			const editor = view.container.querySelector<HTMLElement>('.tiptap')! as HTMLElement & { editor: Editor };
			await waitFor(() => expect(editor.textContent).toBe(TEXT));
			await fireEvent.submit(view.container.querySelector('form')!);
			await waitFor(() => expect(gate.release).toBeTruthy());
			expect(editor.textContent).toBe('');
			await steps(editor.editor);
			activeSession.newChat();
			await waitFor(() => expect(editor.textContent).toBe(''));
			const reopen = async () => {
				Object.assign(activeSession.current, { sessionId: 'chat-a', mode: 'chat', profile: 'coding', isStreaming: false });
				activeSession.setDraft(sessionEphemeralCache.peekDraft('chat-a') || readDraft('chat-a') || '');
			};
			if (reopenFirst) await reopen();
			gate.release!();
			await new Promise((resolve) => setTimeout(resolve, 30));
			if (!reopenFirst) await reopen();

			expect(sends).not.toHaveBeenCalled();
			await waitFor(() => expect(editor.textContent).toContain(TEXT));
			for (const shown of [editor.textContent ?? '', readDraft('chat-a') ?? '']) {
				expect(shown.split(TEXT)).toHaveLength(2);
				for (const gone of forgotten) expect(shown).not.toContain(gone);
				if (left) {
					expect(shown.split(left)).toHaveLength(2);
					expect(shown.indexOf(left)).toBeGreaterThan(shown.indexOf(TEXT));
				}
			}
		});
	});

	// PR #1212 review S5: leaving New chat for another chat is a leave, even
	// after a send made in some other chat. Only New chat taking the id its own
	// send created is not. Uses the real switchSession and newChat callers.
	describe.each([
		['while a send from chat A is held', 'held'],
		['after a send from chat A went out', 'sent'],
		['with no send at all', 'none'],
	] as const)('a quick draft in New chat survives opening chat B %s', (_when, send) => {
		it('and comes back when New chat is opened again', async () => {
			const view = render(ChatInput);
			const editor = view.container.querySelector<HTMLElement>('.tiptap')! as HTMLElement & { editor: Editor };
			await waitFor(() => expect(editor.textContent).toBe(TEXT));
			if (send !== 'none') {
				await fireEvent.submit(view.container.querySelector('form')!);
				await waitFor(() => expect(gate.release).toBeTruthy());
				if (send === 'sent') {
					gate.release!();
					await waitFor(() => expect(sends).toHaveBeenCalledTimes(1));
				}
			}
			activeSession.newChat();
			await waitFor(() => expect(editor.textContent).toBe(''));
			editor.editor.commands.insertContent('my separate new chat draft');
			await waitFor(() => expect(editor.textContent).toBe('my separate new chat draft'));
			// Inside the 100 ms save delay.
			activeSession.switchSession('chat-b');
			await waitFor(() => expect(editor.textContent).toBe(''));
			if (send === 'held') gate.release!();
			await new Promise((resolve) => setTimeout(resolve, 30));
			expect(readDraft(null) ?? '').toContain('my separate new chat draft');
			activeSession.newChat();
			await waitFor(() => expect(editor.textContent).toBe('my separate new chat draft'));
			if (send === 'held') {
				expect(sends).not.toHaveBeenCalled();
				expect(readDraft('chat-a') ?? '').toContain(TEXT);
				expect(readDraft(null) ?? '').not.toContain(TEXT);
			}
		});
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

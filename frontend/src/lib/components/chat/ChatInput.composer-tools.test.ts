import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { tick } from 'svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { composerSuggestion } from '$lib/stores/session/composer-suggestion';
import { promptStash } from '$lib/stores/session/prompt-stash';
import { LARGE_PASTE_BYTES } from './tiptap/large-paste';
import '$lib/test-stubs/composer-dom';

// The composer's keyboard tools: ArrowUp recall, the Ctrl/Cmd+S prompt stash,
// unsent text returning to the composer, and large pastes becoming a file.
const uploads: string[] = [];

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url.includes('/upload')) {
			uploads.push(url);
			const data = { path: 'chat-uploads/pasted-text.txt', filename: 'pasted-text.txt', full_path: '/w/chat-uploads/pasted-text.txt', size: 40000 };
			return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
		}
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/commands') ? { commands: [] } : url.includes('/composer-suggestion') ? null : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

vi.stubGlobal('matchMedia', (query: string) => ({
	matches: query.includes('fine'), media: query, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}
}));

const userMessage = (id: string, content: string) => ({ id, role: 'user' as const, content });

beforeEach(async () => {
	sessionStorage.clear();
	localStorage.clear();
	uploads.length = 0;
	composerSuggestion.reset();
	promptStash.setOwner(null);
	promptStash.setOwner('user:test');
	activeSession.newChat();
	Object.assign(activeSession.current, {
		sessionId: 'tools', mode: 'chat', profile: 'coding', draft: '',
		isStreaming: false, liveTurnId: null,
		messages: [
			userMessage('u1', 'first prompt'),
			{ id: 'a1', role: 'assistant' as const, content: 'ok' },
			userMessage('u2', 'second prompt')
		]
	});
	activeSession.setDraft('');
	vi.spyOn(activeSession, 'sendMessage').mockResolvedValue(true);
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
});

function renderComposer() {
	const view = render(ChatInput);
	const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
	return { ...view, editor };
}

describe('ArrowUp prompt recall', () => {
	it('walks back through earlier prompts and returns to an empty composer', async () => {
		const { editor } = renderComposer();
		await fireEvent.keyDown(editor, { key: 'ArrowUp' });
		await waitFor(() => expect(editor.textContent).toBe('second prompt'));
		await fireEvent.keyDown(editor, { key: 'ArrowUp' });
		await waitFor(() => expect(editor.textContent).toBe('first prompt'));
		await fireEvent.keyDown(editor, { key: 'ArrowDown' });
		await waitFor(() => expect(editor.textContent).toBe('second prompt'));
		await fireEvent.keyDown(editor, { key: 'ArrowDown' });
		await waitFor(() => expect(editor.textContent).toBe(''));
	});

	it('lets go once the user edits the recalled prompt', async () => {
		const { editor } = renderComposer();
		await fireEvent.keyDown(editor, { key: 'ArrowUp' });
		await waitFor(() => expect(editor.textContent).toBe('second prompt'));
		await fireEvent.input(editor);
		const up = await fireEvent.keyDown(editor, { key: 'ArrowUp' });
		await tick();
		// Not handled: the key keeps its normal cursor-moving job.
		expect(up).toBe(true);
		expect(editor.textContent).toBe('second prompt');
	});

	it('does nothing when the composer already has text', async () => {
		activeSession.setDraft('my own words');
		const { editor } = renderComposer();
		await fireEvent.keyDown(editor, { key: 'ArrowUp' });
		await tick();
		expect(editor.textContent).toBe('my own words');
	});
});

describe('prompt stash', () => {
	it('Ctrl+S sets the draft aside and the list restores it', async () => {
		activeSession.setDraft('stash me for later');
		const { editor } = renderComposer();
		const handled = !(await fireEvent.keyDown(editor, { key: 's', ctrlKey: true }));
		expect(handled).toBe(true);
		await waitFor(() => expect(editor.textContent).toBe(''));
		expect(get(promptStash).map(e => e.preview)).toEqual(['stash me for later']);
		// Persisted under the signed-in account.
		expect(localStorage.getItem('ai-shuffle-prompt-stash:user:test')).toContain('stash me for later');

		await fireEvent.click(screen.getByRole('button', { name: 'Stashed prompts (1)' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Restore stashed prompt: stash me for later' }));
		await waitFor(() => expect(editor.textContent).toBe('stash me for later'));
		expect(get(promptStash)).toEqual([]);
	});

	it('deletes a stashed prompt from the list', async () => {
		promptStash.push('delete me');
		renderComposer();
		await fireEvent.click(await screen.findByRole('button', { name: 'Stashed prompts (1)' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Delete stashed prompt: delete me' }));
		expect(get(promptStash)).toEqual([]);
		await waitFor(() => expect(screen.queryByRole('button', { name: /Stashed prompts/ })).toBeNull());
	});

	it('restoring over a draft stashes that draft instead of losing it', async () => {
		promptStash.push('older idea');
		activeSession.setDraft('current idea');
		const { editor } = renderComposer();
		await fireEvent.click(await screen.findByRole('button', { name: 'Stashed prompts (1)' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Restore stashed prompt: older idea' }));
		await waitFor(() => expect(editor.textContent).toBe('older idea'));
		expect(get(promptStash).map(e => e.preview)).toEqual(['current idea']);
	});

	it('leaves the browser Save shortcut alone outside the composer', async () => {
		activeSession.setDraft('not stashed');
		renderComposer();
		const notPrevented = await fireEvent.keyDown(document.body, { key: 's', ctrlKey: true });
		expect(notPrevented).toBe(true);
		expect(get(promptStash)).toEqual([]);
	});
});

describe('unsent prompts return to the composer', () => {
	it('puts the unsent text in front of what the user already typed', async () => {
		activeSession.setDraft('typed after');
		const { editor } = renderComposer();
		activeSession.setPendingRestorePrompt('unsent first');
		await waitFor(() => expect(editor.textContent).toContain('unsent first'));
		const text = editor.textContent ?? '';
		expect(text.indexOf('unsent first')).toBeLessThan(text.indexOf('typed after'));
		expect(activeSession.current.pendingRestorePrompt).toBeNull();
	});
});

describe('large pastes', () => {
	function paste(target: HTMLElement, text: string) {
		const event = new Event('paste', { bubbles: true, cancelable: true }) as Event & { clipboardData: unknown };
		event.clipboardData = { getData: (type: string) => (type === 'text/plain' ? text : ''), types: ['text/plain'], files: [], items: [] };
		target.dispatchEvent(event);
	}

	it('become a text-file attachment instead of inline text', async () => {
		const { editor } = renderComposer();
		paste(editor, 'x'.repeat(LARGE_PASTE_BYTES));
		await waitFor(() => expect(uploads).toHaveLength(1));
		expect(uploads[0]).toContain('/workspace/upload');
		await waitFor(() => expect(editor.textContent).toContain('pasted-text.txt'));
		expect(editor.textContent).not.toContain('xxxx');
	});

	it('stay inline after Ctrl+Shift+V', async () => {
		const { editor } = renderComposer();
		await fireEvent.keyDown(editor, { key: 'V', ctrlKey: true, shiftKey: true });
		paste(editor, 'y'.repeat(LARGE_PASTE_BYTES));
		await tick();
		expect(uploads).toHaveLength(0);
		expect(editor.textContent).toContain('yyyy');
	});

	it('leave small pastes inline', async () => {
		const { editor } = renderComposer();
		paste(editor, 'short text');
		await tick();
		expect(uploads).toHaveLength(0);
		expect(editor.textContent).toContain('short text');
	});
});

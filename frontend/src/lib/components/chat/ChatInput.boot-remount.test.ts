/**
 * #811: text typed on the new-chat screen just before the composer is
 * replaced must follow it, even while the editor-to-store draft save is still
 * inside its 100 ms debounce. Svelte can mount the replacement before the old
 * composer's onDestroy saves, so the replacement must not seed itself from a
 * store that has not seen the text yet.
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
			: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

const TEXT = 'typed right after boot';

beforeEach(async () => {
	localStorage.clear();
	sessionStorage.clear();
	activeSession.newChat();
	Object.assign(activeSession.current, { sessionId: null, mode: 'chat', profile: 'coding', error: null, worktreeError: null });
	activeSession.setDraft('');
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

type EditorHost = HTMLElement & { editor?: { commands: { insertContent(text: string): boolean } } };

async function typeInto(container: HTMLElement): Promise<void> {
	const dom = container.querySelector<EditorHost>('.tiptap')!;
	await waitFor(() => expect(dom.editor).toBeTruthy());
	dom.editor!.commands.insertContent(TEXT);
	expect(dom.textContent).toBe(TEXT);
	// Still inside the 100 ms debounce: the store has not seen the text.
	expect(activeSession.current.draft).not.toContain(TEXT);
}

it('a composer mounted in place of one with debounced text opens with that text', async () => {
	const first = render(ChatInput);
	await typeInto(first.container);

	// Boot's New Chat (AppPage handleNewSession) resets the store's draft, and
	// the replacement composer mounts before the old one is destroyed.
	activeSession.newChat();
	const second = render(ChatInput);
	first.unmount();

	const editor = second.container.querySelector<HTMLElement>('.tiptap')!;
	await waitFor(() => expect(editor.textContent).toBe(TEXT));
	expect(activeSession.current.draft).toContain(TEXT);
});

it('a composer unmounted inside the draft debounce keeps its text for the next one', async () => {
	const first = render(ChatInput);
	await typeInto(first.container);
	first.unmount();

	expect(activeSession.current.draft).toContain(TEXT);
	const second = render(ChatInput);
	const editor = second.container.querySelector<HTMLElement>('.tiptap')!;
	await waitFor(() => expect(editor.textContent).toBe(TEXT));
});

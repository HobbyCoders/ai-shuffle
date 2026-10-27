/**
 * #707 review: a send clears the draft before its request settles. Until it
 * does, an update must not reload the page (the message would be lost if the
 * send then failed and wanted to restore it).
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { reloadBlockers } from '$lib/services/app-update';
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

beforeEach(async () => {
	localStorage.clear();
	sessionStorage.clear();
	activeSession.newChat();
	Object.assign(activeSession.current, {
		sessionId: 'chat', mode: 'chat', profile: 'coding', isStreaming: false, liveTurnId: null,
		error: null, worktreeError: null
	});
	activeSession.setDraft('send me');
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

it('a send in flight blocks the reload after its draft is cleared, and only until it settles', async () => {
	let settle!: (sent: boolean) => void;
	vi.spyOn(activeSession, 'sendMessage').mockImplementation(() => new Promise<boolean>((resolve) => { settle = resolve; }));
	const view = render(ChatInput);
	const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
	await waitFor(() => expect(editor.textContent).toBe('send me'));
	expect(reloadBlockers()).not.toContain('composer-sending');

	await fireEvent.submit(view.container.querySelector('form')!);
	await waitFor(() => expect(activeSession.sendMessage).toHaveBeenCalledOnce());
	// The draft is gone already; the request is not done.
	expect(editor.textContent).toBe('');
	expect(reloadBlockers()).toContain('composer-sending');

	settle(true);
	await waitFor(() => expect(reloadBlockers()).not.toContain('composer-sending'));
	cleanup();
	expect(reloadBlockers()).toEqual([]);
});

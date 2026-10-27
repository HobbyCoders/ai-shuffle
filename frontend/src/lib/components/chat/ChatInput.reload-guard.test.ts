/**
 * #707 review, #711: a send clears the draft before its request settles.
 * Until it does, an update must not reload the page (the message would be
 * lost if the send then failed and wanted to restore it), even after the
 * composer that started it unmounted.
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
	vi.useRealTimers();
	cleanup();
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

it('a send in flight blocks the reload after its composer unmounts, and only until it settles', async () => {
	// The real send path, held at its restore wait: the page still restores
	// the session this send targets.
	vi.useFakeTimers({ shouldAdvanceTime: true });
	activeSession.current.isRestoringSession = true;
	const view = render(ChatInput);
	const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
	await waitFor(() => expect(editor.textContent).toBe('send me'));
	expect(reloadBlockers()).not.toContain('message-sending');

	await fireEvent.submit(view.container.querySelector('form')!);
	// The draft is gone already; the request is not done.
	await waitFor(() => expect(editor.textContent).toBe(''));
	expect(reloadBlockers()).toContain('message-sending');

	// The composer leaves (an account switch, the view changing) mid-send.
	cleanup();
	expect(reloadBlockers()).toEqual(['message-sending']);

	// The restore never settles: the send gives up and its message goes back
	// to the session's draft, which keeps the page from here on.
	await vi.advanceTimersByTimeAsync(5_000);
	await waitFor(() => expect(reloadBlockers()).toEqual(['composer-draft']));
	expect(activeSession.current.draft).toContain('send me');
	activeSession.current.isRestoringSession = false;
});

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { git } from '$lib/stores/git.svelte';
import '$lib/test-stubs/composer-dom';

// Exercise the send guard independently of the context bar's intent-reset effect.
vi.mock('./ContextPillBar.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/git/status') ? { is_git_repo: false }
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
	git.clear();
	activeSession.newChat();
	Object.assign(activeSession.current, {
		sessionId: null, project: 'plain-folder', projectV2Id: null, mode: 'code', profile: 'coding',
		isStreaming: false, liveTurnId: null, error: null, worktreeError: null, messages: []
	});
	activeSession.setWorktreeIntent('worktree');
	activeSession.setDraft('Explain these files');
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	git.clear();
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

describe('plain-folder first send', () => {
	it.each(['plain-folder', 'previous-folder'])('uses only current repository capability (%s)', async statusProject => {
		await git.loadRepository(statusProject);
		let intentAtSend: string | null = null;
		const send = vi.spyOn(activeSession, 'sendMessage').mockImplementation(async () => {
			intentAtSend = activeSession.current.worktreeIntent;
			return true;
		});
		const { container } = render(ChatInput);
		await waitFor(() => expect(container.querySelector('.tiptap')?.textContent).toBe('Explain these files'));
		await fireEvent.submit(container.querySelector('form')!);
		await waitFor(() => expect(send).toHaveBeenCalledOnce());
		expect(intentAtSend).toBe(statusProject === 'plain-folder' ? 'local' : 'worktree');
	});
});

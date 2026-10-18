import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { git } from '$lib/stores/git.svelte';
import '$lib/test-stubs/composer-dom';

// Exercise the send guard independently of the context bar's intent-reset effect.
vi.mock('./ContextPillBar.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
const capability = vi.hoisted(() => {
	const state = { isRepo: false };
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/git/status') ? { is_git_repo: state.isRepo }
				: url.includes('/git/branches') ? { branches: [] }
					: url.includes('/git/graph') ? { commits: [], total: 0 }
							: url.includes('/git/worktrees') ? { worktrees: [], total: 0 }
						: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
	return state;
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
	// The explicit preference is valid only after a positive current-project
	// capability (#172). The later negative answer still exercises the send guard.
	capability.isRepo = true;
	await git.loadRepository('plain-folder');
	expect(git.current.hydrated).toBe(true);
	expect(git.current.status?.is_git_repo).toBe(true);
	expect(git.current.error).toBeNull();
	activeSession.setWorktreeIntent('worktree');
	capability.isRepo = false;
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
		capability.isRepo = statusProject === 'previous-folder';
		await git.loadRepository(statusProject);
		if (statusProject === 'previous-folder') {
			// A positive but unrelated repository must not block an explicit
			// preference for the current draft while its capability is unknown.
			activeSession.setWorktreeIntent('worktree');
			expect(activeSession.current.worktreeIntent).toBe('worktree');
		}
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

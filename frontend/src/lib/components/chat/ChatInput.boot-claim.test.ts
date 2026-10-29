/** A boot send is not navigation: keep ChatInput's existing epoch guards intact. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { auth } from '$lib/stores/auth.svelte';
import { platform } from '$lib/stores/platform.svelte';
import { composerPreferences } from '$lib/stores/composer-preferences';
import { finishBootReconciliation, getInitGeneration, hasBootSendClaim } from '$lib/stores/session/session-init';
import { registerComposerShell, type ComposerShellHooks } from './composer-shell';
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
const TEXT = 'first worktree message';
let releasePreferences: () => void;
let initializing: Promise<void>;
beforeEach(async () => {
	sessionStorage.clear(); activeSession.newChat();
	Object.assign(activeSession.current, { sessionId: null, mode: 'code', profile: 'coding', project: 'p', error: null,
		worktreeError: null, worktreeIntent: 'worktree', worktreeBranch: 'feature/test', worktreeMode: 'new' });
	appData.current.profiles = [{ id: 'coding', name: 'Coding' }] as never;
	vi.spyOn(activeSession, 'composerAwaitingProjectCatalog', 'get').mockReturnValue(false);
	vi.spyOn(composerPreferences, 'hydrate').mockReturnValue(new Promise<void>(done => { releasePreferences = done; }));
	initializing = activeSession.init();
	activeSession.setDraft(TEXT);
	await auth.checkAuth();
});
afterEach(async () => {
	releasePreferences(); await initializing;
	finishBootReconciliation(getInitGeneration());
	cleanup(); platform.set('desktop'); registerComposerShell({}); appData.current.profiles = []; activeSession.setDraft(''); vi.restoreAllMocks();
});

it('restores a refused first-send draft even after its own worktree create adopted an id', async () => {
	const generation = getInitGeneration();
	// The real sender admits the first send and claims boot BEFORE awaiting
	// this create. Its adopted chat then refuses the first query.
	vi.spyOn(activeSession, 'createWorktree').mockImplementation(async () => {
		expect(hasBootSendClaim(generation)).toBe(true);
		Object.assign(activeSession.current, { sessionId: 'created-worktree' });
		activeSession.setDraft('');
		return null;
	});
	const view = render(ChatInput);
	const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
	await waitFor(() => expect(editor.textContent).toBe(TEXT));
	await fireEvent.submit(view.container.querySelector('form')!);
	await waitFor(() => expect(activeSession.current.sessionId).toBe('created-worktree'));
	await waitFor(() => expect(editor.textContent).toBe(TEXT));
	expect(getInitGeneration()).toBe(generation);
	expect(activeSession.current.draft).toContain(TEXT);
	// The send failed, but its worktree already adopted a real chat.
	expect(hasBootSendClaim(generation)).toBe(true);
});

it('runs the phone composer successful-send reconciliation after its first send adopts a chat', async () => {
	platform.set('mobile');
	// Current mobile registration deliberately keeps the quick bar visible;
	// only its expansion reconciliation is installed, not one-line collapse.
	const reconcile = vi.fn<ComposerShellHooks['reconcileExpanded']>(() => false);
	registerComposerShell({ reconcileExpanded: reconcile });
	const generation = getInitGeneration();
	vi.spyOn(activeSession, 'createWorktree').mockImplementation(async () => {
		expect(hasBootSendClaim(generation)).toBe(true);
		Object.assign(activeSession.current, { sessionId: 'created-worktree' });
		activeSession.setDraft('');
		return 'created-worktree';
	});
	const view = render(ChatInput, { compact: true });
	const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
	await waitFor(() => expect(editor.textContent).toBe(TEXT));
	editor.focus(); await fireEvent.focusIn(editor);
	expect(view.container.querySelector('.mobile-collapsed')).toBeNull();
	await fireEvent.submit(view.container.querySelector('form')!);
	await waitFor(() => expect(reconcile).toHaveBeenCalledWith(expect.any(Boolean), expect.objectContaining({ hasDraft: false, composerFocused: false, controlsOpen: false }), 'successful-send'));
	expect(document.activeElement).not.toBe(editor);
	expect(reconcile.mock.results.some(result => result.value === false)).toBe(true);
	expect(getInitGeneration()).toBe(generation);
	expect(activeSession.current.sessionId).toBe('created-worktree');
});

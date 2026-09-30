import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { initialState, reduce } from '$lib/stores/session/v2/reducer';
import { createMessageSender } from '$lib/stores/session/message-sender';
import type { InternalMutableState } from '$lib/stores/session/ws-message-handlers';
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
let stopTimer: ReturnType<typeof setTimeout> | null = null;

beforeEach(async () => {
	sessionStorage.clear();
	activeSession.newChat();
	Object.assign(activeSession.current, { sessionId: 'local-working', mode: 'code', profile: 'coding', draft: '' });
	// Drive the same reducer event that produces the transcript's Working label.
	const live = reduce(initialState(), { type: 'turn.started', id: 'started', seq: 1,
		session_id: 'local-working', turn_id: 'live-turn' });
	Object.assign(activeSession.current, { isStreaming: live.isStreaming, liveTurnId: live.liveTurnId });
	activeSession.setDraft('');
	await auth.checkAuth();
});
afterEach(() => {
	if (stopTimer) clearTimeout(stopTimer);
	stopTimer = null;
	cleanup();
	vi.restoreAllMocks();
});

function captureStop() {
	const wsSend = vi.fn(() => true);
	const sender = createMessageSender({
		getState: () => activeSession.current,
		update: fn => { Object.assign(activeSession.current, fn(activeSession.current)); activeSession.setDraft(activeSession.current.draft); },
		internal: { pendingTools: new Set(), awaitingNewSession: false, pendingRequestId: null,
			stopRequestedBeforeSession: false, worktreeCreationGeneration: 0, worktreeCreateAbort: null } as InternalMutableState,
		wsConn: { isConnected: () => true, connect: vi.fn(), send: wsSend, resetReconnectAttempts: vi.fn() },
		sessionEphemeralCache: { remove: vi.fn() },
		appData: { loadSessions: vi.fn(), loadApiUserSessions: vi.fn() },
		persistSession: vi.fn(), stopTimeout: { get: () => stopTimer, set: value => { stopTimer = value; } },
		getActiveSession: () => sender
	});
	vi.spyOn(activeSession, 'stopGeneration').mockImplementation(sender.stopGeneration);
	return wsSend;
}

describe('running-session composer Stop', () => {
	it('renders and dispatches Stop for a local turn with an empty editor', async () => {
		const wsSend = captureStop();
		render(ChatInput);
		await fireEvent.click(screen.getByRole('button', { name: 'Stop generating' }));
		expect(wsSend).toHaveBeenCalledWith({ type: 'stop', session_id: 'local-working', turn_id: 'live-turn' });
	});

	it.each(['<p>   </p>', '<p><br></p><p><br></p>', '<p>Keep this draft</p>'])(
		'never hides Stop when real editor input changes to %s', async html => {
			const wsSend = captureStop();
			const { container } = render(ChatInput);
			const editor = container.querySelector<HTMLElement>('.tiptap')!;
			editor.focus();
			editor.innerHTML = html;
			await fireEvent.input(editor, { inputType: 'insertText', data: ' ' });
			await tick();
			await waitFor(() => expect(screen.getByRole('button', { name: 'Stop generating' })).toBeTruthy());
			if (html.includes('Keep this draft')) {
				await waitFor(() => expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(false));
			} else {
				expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();
			}
			const draft = activeSession.current.draft;
			await fireEvent.click(screen.getByRole('button', { name: 'Stop generating' }));
			expect(wsSend).toHaveBeenCalledWith({ type: 'stop', session_id: 'local-working', turn_id: 'live-turn' });
			expect(activeSession.current.draft).toBe(draft);
		}
	);

	it('removes Stop only after the turn settles, not when a draft appears', async () => {
		render(ChatInput);
		expect(screen.getByRole('button', { name: 'Stop generating' })).toBeTruthy();
		const settled = reduce(initialState(), { type: 'turn.completed', id: 'settled', seq: 2,
			session_id: 'local-working', turn_id: 'live-turn' });
		Object.assign(activeSession.current, { isStreaming: settled.isStreaming, liveTurnId: settled.liveTurnId });
		activeSession.setDraft('');
		await tick();
		expect(screen.queryByRole('button', { name: 'Stop generating' })).toBeNull();
		expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(true);
	});
});

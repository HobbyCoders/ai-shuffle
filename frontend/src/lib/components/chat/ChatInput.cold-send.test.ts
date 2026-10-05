import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import '$lib/test-stubs/composer-dom';

// #1831: before a send into a large chat whose provider cache went cold, the
// composer says what it costs and offers "Compact first" beside "Send anyway".
const calls: string[] = [];
let cache: Record<string, unknown> = {};

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		calls.push(`${init?.method ?? 'GET'} ${url}`);
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/cache-state')
				? cache
				: url.includes('/compact')
					? { turn_id: 't-compact', session_id: 'cold' }
					: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

const TEXT = 'carry on';
const COLD = {
	leg: 'delegated', context_tokens: 412_000, last_call_at: 1, idle_seconds: 5400,
	cache_ttl_seconds: 3600, threshold_tokens: 150_000, cold: true, large: true, warn: true
};
let sends: MockInstance;

beforeEach(async () => {
	calls.length = 0;
	localStorage.clear();
	sessionStorage.clear();
	activeSession.newChat();
	Object.assign(activeSession.current, {
		sessionId: 'cold', mode: 'chat', profile: 'coding', isStreaming: false, liveTurnId: null,
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

async function submit() {
	const view = render(ChatInput);
	const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
	await waitFor(() => expect(editor.textContent).toBe(TEXT));
	await fireEvent.submit(view.container.querySelector('form')!);
	return { ...view, editor };
}

describe('cold send warning', () => {
	it('asks before a large cold send, and Send anyway sends and is remembered', async () => {
		cache = COLD;
		const { editor } = await submit();
		await waitFor(() => expect(screen.getByText(/holds about 412k tokens/)).toBeTruthy());
		expect(sends).not.toHaveBeenCalled();
		expect(editor.textContent).toBe(TEXT);
		await fireEvent.click(screen.getByRole('button', { name: 'Send anyway' }));
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
		expect(sends.mock.calls[0][0]).toBe(TEXT);
		// The same chat at the same size is not asked again.
		cleanup();
		activeSession.setDraft(TEXT);
		await submit();
		await waitFor(() => expect(sends).toHaveBeenCalledTimes(2));
		expect(screen.queryByRole('button', { name: 'Send anyway' })).toBeNull();
	});

	it('Compact first runs the compaction and keeps the message', async () => {
		cache = COLD;
		const { editor } = await submit();
		await waitFor(() => expect(screen.getByRole('button', { name: 'Compact first' })).toBeTruthy());
		await fireEvent.click(screen.getByRole('button', { name: 'Compact first' }));
		await waitFor(() => expect(calls.some((c) => c.startsWith('POST') && c.includes('/sessions/cold/compact'))).toBe(true));
		await waitFor(() => expect(screen.queryByRole('button', { name: 'Compact first' })).toBeNull());
		expect(sends).not.toHaveBeenCalled();
		expect(editor.textContent).toBe(TEXT);
	});

	it('sends at once when the cache is warm or the chat is small', async () => {
		cache = { ...COLD, warn: false, cold: false };
		await submit();
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
		expect(screen.queryByRole('button', { name: 'Send anyway' })).toBeNull();
	});
});

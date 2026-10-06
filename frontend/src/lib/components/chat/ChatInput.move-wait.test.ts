import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import type { ChatMessage } from '$lib/stores/types';
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

const row = (status: string, extra: Record<string, unknown> = {}): ChatMessage => ({
	id: `handoff-${status}`, role: 'system', type: 'system', content: '', systemSubtype: 'handoff',
	systemData: { status, worker: 'DesktopWorker', by: 'owner', ...extra }
});

function start(messages: ChatMessage[]) {
	activeSession.newChat();
	Object.assign(activeSession.current, { sessionId: 'moving-chat', mode: 'code', profile: 'coding', draft: '', messages });
	activeSession.setDraft('');
}

function placeholder(container: HTMLElement): string | null {
	return container.querySelector('[data-placeholder]')?.getAttribute('data-placeholder') ?? null;
}

beforeEach(async () => {
	sessionStorage.clear();
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
});

// #1819: messages sent while a move waits are held and sent after it.
describe('composer while a move waits', () => {
	it('says the chat moves after this turn', async () => {
		start([row('queued', { after: 'turn' })]);
		const { container } = render(ChatInput);
		await waitFor(() => expect(placeholder(container)).toBe('Moves to DesktopWorker after this turn'));
	});

	it('says the chat moves after the queued messages', async () => {
		start([row('queued', { after: 'queued' })]);
		const { container } = render(ChatInput);
		await waitFor(() => expect(placeholder(container)).toBe('Moves to DesktopWorker after the queued messages'));
	});

	it('goes back to normal once the move is cancelled', async () => {
		start([row('queued'), row('cancelled')]);
		const { container } = render(ChatInput);
		await waitFor(() => expect(placeholder(container)).toBe('Describe a task or ask a question'));
	});
});

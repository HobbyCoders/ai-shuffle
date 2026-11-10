import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import type { ChatMessage } from '$lib/stores/types';
import '$lib/test-stubs/composer-dom';

// #1364: a note chip is sent as this worker's file path to a chat on this
// worker and as `@Notes/<id>` to one on another worker, decided at send time.

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

const PEER = { id: 'peer-b', name: 'Worker B' };
const PATH = '/viewer/data/notes/plan.md';
const NOTE_ID = 'note-0123456789ab';

function draftWith(noteId: string | null): string {
	return JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content: [
		{ type: 'text', text: 'read ' },
		{ type: 'fileChip', attrs: {
			path: PATH, fullPath: PATH, filename: 'Plan', size: 0, type: 'note', mention: true,
			...(noteId ? { noteId } : {})
		} }
	] }] });
}

let sends: MockInstance;

function start(sessionId: string | null, noteId: string | null = NOTE_ID, extra: Record<string, unknown> = {}) {
	activeSession.newChat();
	Object.assign(activeSession.current, {
		sessionId, mode: 'chat', profile: 'coding', isStreaming: false, liveTurnId: null,
		error: null, worktreeError: null, ...extra
	});
	activeSession.setDraft(draftWith(noteId));
}

async function send(beforeSend?: () => void): Promise<string> {
	const view = render(ChatInput);
	await waitFor(() => expect(view.container.querySelector('[data-file-chip]')).toBeTruthy());
	beforeSend?.();
	await fireEvent.submit(view.container.querySelector('form')!);
	await waitFor(() => expect(sends).toHaveBeenCalledOnce());
	return sends.mock.calls[0][0] as string;
}

beforeEach(async () => {
	localStorage.clear();
	sessionStorage.clear();
	sends = vi.spyOn(activeSession, 'sendMessage').mockResolvedValue(true);
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

describe('a note chip at send time', () => {
	it('sends the file path to a new chat that runs on this worker', async () => {
		start(null);
		expect(await send()).toBe(`read @${PATH}`);
	});

	it('sends the note id when "Runs on" moves to another worker after the pick', async () => {
		start(null);
		expect(await send(() => activeSession.setDeviceIntent(PEER))).toBe(`read @Notes/${NOTE_ID}`);
	});

	it('sends the file path when "Runs on" comes back to this worker after the pick', async () => {
		start(null, NOTE_ID, { deviceIntent: PEER });
		expect(await send(() => activeSession.setDeviceIntent(null))).toBe(`read @${PATH}`);
	});

	it('sends the note id to an open chat whose owner is not known', async () => {
		start('open-chat', NOTE_ID, { isRemote: undefined });
		expect(await send()).toBe(`read @Notes/${NOTE_ID}`);
	});

	it('sends the note id to a chat on this worker while a move holds its messages', async () => {
		const moving: ChatMessage = {
			id: 'handoff-queued', role: 'system', type: 'system', content: '', systemSubtype: 'handoff',
			systemData: { status: 'queued', worker: 'Worker B', by: 'owner', after: 'turn' }
		};
		start('moving-chat', NOTE_ID, { isRemote: false, messages: [moving] });
		expect(await send()).toBe(`read @Notes/${NOTE_ID}`);
	});

	it('sends the path of a chip saved before chips kept the note id', async () => {
		start(null, null, { deviceIntent: PEER });
		expect(await send()).toBe(`read @${PATH}`);
	});
});

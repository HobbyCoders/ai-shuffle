import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { appData } from '$lib/stores/app-data.svelte';
import { startLiveSync } from '$lib/services/live-sync';
import type { ChatMessage } from '$lib/stores/types';
import '$lib/test-stubs/composer-dom';

// #1364: a note chip is sent as this worker's file path to a chat on this
// worker and as `@Notes/<id>` to one on another worker, decided at send time.

// The session list the server holds, and a hold on the next list read.
const net = vi.hoisted(() => ({ rows: [] as Record<string, unknown>[], hold: null as Promise<void> | null }));
vi.hoisted(() => {
	// live-sync loads the theme store, which reads the system theme on import.
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url.includes('/sessions/cursor') && net.hold) await net.hold;
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true,
				preference_identity: { user_type: 'account', user_id: 'owner' } }
			: url.includes('/commands') ? { commands: [] }
			: url.includes('/preferences/all') ? { preferences: {}, versions: {} }
			: url.includes('/sessions/count') ? { total: 0 }
			: url.includes('membership=settled')
				? { items: net.rows, next_cursor: null, next_cursor_id: null, has_more: false }
			: url.includes('/sessions/cursor') ? { items: [], next_cursor: null, next_cursor_id: null, has_more: false }
			: [];
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

describe('a note chip after a hand-off signal', () => {
	// What a managed-account or paired socket gets for a hand-off: a bare
	// "sessions" signal, never the owner. Until the list it triggers lands, the
	// row's `is_remote: false` is the old owner's answer.
	class Socket {
		static OPEN = 1; static CLOSED = 3; static all: Socket[] = []; readyState = 1;
		onopen?: () => void; onmessage?: (event: { data: string }) => void; onclose?: (event: { code: number }) => void;
		constructor() { Socket.all.push(this); }
		send = vi.fn(); close = vi.fn();
		emit(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }); }
	}
	let stop = () => {};
	let release = () => {};
	const row = { id: 'local-chat', title: 'Plan', is_remote: false, device_name: null };

	beforeEach(async () => {
		Socket.all = [];
		vi.stubGlobal('WebSocket', Socket);
		net.rows = [row];
		net.hold = null;
		await appData.loadSessions();
		start('local-chat', NOTE_ID, { isRemote: false });
		stop = startLiveSync();
		await waitFor(() => expect(Socket.all).toHaveLength(1));
		net.hold = new Promise((resolve) => { release = () => { net.hold = null; resolve(); }; });
	});
	afterEach(() => {
		release();
		stop();
		net.rows = [];
	});

	it('sends the note id while the list the signal asked for has not landed', async () => {
		Socket.all[0].emit({ type: 'resources_changed', resources: ['sessions'] });
		expect(await send()).toBe(`read @Notes/${NOTE_ID}`);
	});

	it('sends the path again once the list says the chat still runs here', async () => {
		Socket.all[0].emit({ type: 'resources_changed', resources: ['sessions'] });
		await new Promise((resolve) => setTimeout(resolve, 150));
		const answered = appData.sessionOwnership().signals;
		release();
		await waitFor(() => expect(appData.sessionOwnership().answered).toBe(answered));
		expect(await send()).toBe(`read @${PATH}`);
	});
});

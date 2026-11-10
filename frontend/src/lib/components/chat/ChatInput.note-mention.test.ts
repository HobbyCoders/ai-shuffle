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
// worker and as `@Notes/<id>` to one on another worker. For an open chat the
// server is asked at send time; any answer but "runs here" sends the id.

// The session list the server holds, and what `/live` answers.
const net = vi.hoisted(() => ({
	rows: [] as Record<string, unknown>[],
	live: (() => ({ session_meta: { is_remote: false } })) as () => unknown,
	liveCalls: [] as string[]
}));
vi.hoisted(() => {
	// live-sync loads the theme store, which reads the system theme on import.
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		if (url.includes('/live?')) {
			// Only the send-time ask: a fresh request, never a shared one.
			if (init?.cache === 'no-store') net.liveCalls.push(url);
			const answer = await new Promise<unknown>((resolve, reject) => {
				init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
				Promise.resolve().then(net.live).then(resolve, reject);
			});
			if (answer instanceof Response) return answer;
			return new Response(JSON.stringify(answer), { status: 200, headers: { 'Content-Type': 'application/json' } });
		}
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
const REF = `read @Notes/${NOTE_ID}`;
const BY_PATH = `read @${PATH}`;

function draftWith(noteId: string | null, chip = true): string {
	return JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content: [
		{ type: 'text', text: chip ? 'read ' : 'read the plan' },
		...(chip ? [{ type: 'fileChip', attrs: {
			path: PATH, fullPath: PATH, filename: 'Plan', size: 0, type: 'note', mention: true,
			...(noteId ? { noteId } : {})
		} }] : [])
	] }] });
}

let sends: MockInstance;

function start(sessionId: string | null, noteId: string | null = NOTE_ID, extra: Record<string, unknown> = {}, chip = true) {
	activeSession.newChat();
	Object.assign(activeSession.current, {
		sessionId, mode: 'chat', profile: 'coding', isStreaming: false, liveTurnId: null,
		error: null, worktreeError: null, ...extra
	});
	activeSession.setDraft(draftWith(noteId, chip));
}

async function send(beforeSend?: () => void, waitMs?: number): Promise<string> {
	const view = render(ChatInput);
	await waitFor(() => expect(view.container.querySelector('.ProseMirror')?.textContent).toContain('read'));
	beforeSend?.();
	await fireEvent.submit(view.container.querySelector('form')!);
	await waitFor(() => expect(sends).toHaveBeenCalledOnce(), waitMs ? { timeout: waitMs } : undefined);
	return sends.mock.calls[0][0] as string;
}

beforeEach(async () => {
	localStorage.clear();
	sessionStorage.clear();
	net.live = () => ({ session_meta: { is_remote: false } });
	net.liveCalls = [];
	sends = vi.spyOn(activeSession, 'sendMessage').mockResolvedValue(true);
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

describe('a note chip in a new chat', () => {
	it('sends the file path when it runs on this worker, without asking the server', async () => {
		start(null);
		expect(await send()).toBe(BY_PATH);
		expect(net.liveCalls).toEqual([]);
	});

	it('sends the note id when "Runs on" moves to another worker after the pick', async () => {
		start(null);
		expect(await send(() => activeSession.setDeviceIntent(PEER))).toBe(REF);
	});

	it('sends the file path when "Runs on" comes back to this worker after the pick', async () => {
		start(null, NOTE_ID, { deviceIntent: PEER });
		expect(await send(() => activeSession.setDeviceIntent(null))).toBe(BY_PATH);
	});

	it('sends the path of a chip saved before chips kept the note id', async () => {
		start(null, null, { deviceIntent: PEER });
		expect(await send()).toBe(BY_PATH);
	});
});

describe('a note chip in an open chat asks the server at send time', () => {
	it('sends the file path when the server says the chat runs here', async () => {
		start('open-chat', NOTE_ID, { isRemote: true });
		expect(await send()).toBe(BY_PATH);
		expect(net.liveCalls).toHaveLength(1);
		expect(net.liveCalls[0]).toContain('/sessions/open-chat/live?include_messages=false&owner_fields=true');
	});

	it('sends the note id when the server says the chat runs elsewhere', async () => {
		net.live = () => ({ session_meta: { is_remote: true } });
		start('open-chat', NOTE_ID, { isRemote: false });
		expect(await send()).toBe(REF);
	});

	it('sends the note id when the request fails', async () => {
		net.live = () => new Response('{"detail":"nope"}', { status: 500, headers: { 'Content-Type': 'application/json' } });
		start('open-chat', NOTE_ID, { isRemote: false });
		expect(await send()).toBe(REF);
	});

	it('sends the note id when the request is refused', async () => {
		net.live = () => new Response('{"detail":"Forbidden"}', { status: 403, headers: { 'Content-Type': 'application/json' } });
		start('open-chat', NOTE_ID, { isRemote: false });
		expect(await send()).toBe(REF);
	});

	it('sends the note id when there is no network', async () => {
		net.live = () => { throw new TypeError('Failed to fetch'); };
		start('open-chat', NOTE_ID, { isRemote: false });
		expect(await send()).toBe(REF);
	});

	it('sends the note id when a worker too old to report the owner answers', async () => {
		net.live = () => ({ session_meta: { title: 'Plan' } });
		start('open-chat', NOTE_ID, { isRemote: false });
		expect(await send()).toBe(REF);
	});

	it('sends the note id, and still sends, when the server takes too long', async () => {
		net.live = () => new Promise(() => {});
		start('open-chat', NOTE_ID, { isRemote: false });
		const started = Date.now();
		expect(await send(undefined, 4000)).toBe(REF);
		expect(Date.now() - started).toBeGreaterThanOrEqual(1900);
	}, 8000);

	it('asks nothing for a message with no note chip', async () => {
		start('open-chat', NOTE_ID, { isRemote: false }, false);
		expect(await send()).toBe('read the plan');
		expect(net.liveCalls).toEqual([]);
	});

	it('asks nothing for a chip saved before chips kept the note id', async () => {
		start('open-chat', null, { isRemote: true });
		expect(await send()).toBe(BY_PATH);
		expect(net.liveCalls).toEqual([]);
	});

	it('sends the note id, asking nothing, while a move holds the chat\'s messages', async () => {
		const moving: ChatMessage = {
			id: 'handoff-queued', role: 'system', type: 'system', content: '', systemSubtype: 'handoff',
			systemData: { status: 'queued', worker: 'Worker B', by: 'owner', after: 'turn' }
		};
		start('moving-chat', NOTE_ID, { isRemote: false, messages: [moving] });
		expect(await send()).toBe(REF);
		expect(net.liveCalls).toEqual([]);
	});

	it('sends nothing into another chat opened while the server was asked', async () => {
		let answer = (_: unknown) => {};
		net.live = () => new Promise((resolve) => { answer = resolve; });
		start('open-chat', NOTE_ID, { isRemote: false });
		const view = render(ChatInput);
		await waitFor(() => expect(view.container.querySelector('[data-file-chip]')).toBeTruthy());
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(net.liveCalls).toHaveLength(1));
		activeSession.newChat();
		answer({ session_meta: { is_remote: false } });
		await new Promise((resolve) => setTimeout(resolve, 100));
		expect(sends).not.toHaveBeenCalled();
	});
});

describe('a note chip after a hand-off signal', () => {
	// What a managed-account or paired socket gets for a hand-off: a bare
	// "sessions" signal, never the owner, so the page's own flags can be stale.
	class Socket {
		static OPEN = 1; static CLOSED = 3; static all: Socket[] = []; readyState = 1;
		onopen?: () => void; onmessage?: (event: { data: string }) => void; onclose?: (event: { code: number }) => void;
		constructor() { Socket.all.push(this); }
		send = vi.fn(); close = vi.fn();
		emit(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }); }
	}
	let stop = () => {};

	async function signalled(rows: Record<string, unknown>[], sessionId: string) {
		Socket.all = [];
		vi.stubGlobal('WebSocket', Socket);
		net.rows = rows;
		await appData.loadSessions();
		start(sessionId, NOTE_ID, { isRemote: false });
		stop = startLiveSync();
		await waitFor(() => expect(Socket.all).toHaveLength(1));
		Socket.all[0].emit({ type: 'resources_changed', resources: ['sessions'] });
		await new Promise((resolve) => setTimeout(resolve, 150));
	}
	afterEach(() => {
		stop();
		net.rows = [];
	});

	it('sends the file path to a chat on this worker that has no sidebar row', async () => {
		// A tag filter, a deep link or an unloaded page leaves the open chat out
		// of the list; the signal must not turn its notes into references.
		await signalled([{ id: 'other-chat', title: 'Other', is_remote: false, device_name: null }], 'unlisted-chat');
		expect(await send()).toBe(BY_PATH);
	});

	it('sends the note id when the page still says local but the server says elsewhere', async () => {
		await signalled([{ id: 'local-chat', title: 'Plan', is_remote: false, device_name: null }], 'local-chat');
		net.live = () => ({ session_meta: { is_remote: true } });
		expect(await send()).toBe(REF);
	});
});

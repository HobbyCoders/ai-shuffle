import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { appData } from '$lib/stores/app-data.svelte';
import { startLiveSync } from '$lib/services/live-sync';
import type { ChatMessage } from '$lib/stores/types';
import { readDraft, writeDraft } from '$lib/stores/session/draft-persistence';
import { TAB_ACTIVE_SESSION_KEY } from '$lib/stores/device-id';
import '$lib/test-stubs/composer-dom';

// #1364: a note chip is sent as this worker's file path to a chat on this
// worker and as `@Notes/<id>` to one on another worker. For an open chat the
// server is asked at send time; any answer but "runs here" sends the id.

// The session list the server holds, and what `/live` answers.
// A voice call on one chat (S1 of PR #1442's review): its text sends are
// recorded and each waits for an acknowledgement the test gives or withholds.
const voice = vi.hoisted(() => ({
	active: null as string | null,
	sent: [] as string[],
	acks: [] as { resolve: (ok: boolean) => void; reject: (error: Error) => void }[]
}));
vi.mock('$lib/services/voiceCall', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/services/voiceCall')>()),
	isCallActiveFor: (sessionId: string) => voice.active === sessionId,
	sendVoiceText: (_owner: string, text: string) => {
		voice.sent.push(text);
		return new Promise<boolean>((resolve, reject) => { voice.acks.push({ resolve, reject }); });
	}
}));
const net = vi.hoisted(() => ({
	rows: [] as Record<string, unknown>[],
	live: (() => ({ session_meta: { is_remote: false } })) as () => unknown,
	liveCalls: [] as string[],
	// The cold-cache check's answer (#1831): nothing to warn about.
	cache: (() => ({ cold: false, warn: false, large: false })) as () => unknown
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
		if (url.includes('/cache-state')) {
			return new Response(JSON.stringify(await net.cache()), { status: 200, headers: { 'Content-Type': 'application/json' } });
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
// What the store would put on the wire: like the real send, it asks for the
// note form (notesForm) only after its own waits.
let sent: string[] = [];

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
	await waitFor(() => expect(sent).toHaveLength(1), waitMs ? { timeout: waitMs } : undefined);
	expect(sends).toHaveBeenCalledOnce();
	return sent[0];
}

beforeEach(async () => {
	localStorage.clear();
	sessionStorage.clear();
	net.live = () => ({ session_meta: { is_remote: false } });
	net.liveCalls = [];
	net.cache = () => ({ cold: false, warn: false, large: false });
	sent = [];
	sends = vi.spyOn(activeSession, 'sendMessage').mockImplementation(async (prompt, options) => {
		const form = options?.notesForm;
		sent.push(form && await form.runsHere() ? form.pathPrompt : prompt);
		// The frame is written: like the store, say so.
		options?.onDispatched?.();
		return true;
	});
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

	it('keeps two quick sends in the order typed when the first waits for its note form', async () => {
		let answer = (_: unknown) => {};
		net.live = () => new Promise((resolve) => { answer = resolve; });
		start('open-chat', NOTE_ID, { isRemote: false, isStreaming: true });
		const view = render(ChatInput);
		await waitFor(() => expect(view.container.querySelector('[data-file-chip]')).toBeTruthy());
		const form = view.container.querySelector('form')!;
		await fireEvent.submit(form);
		await waitFor(() => expect(net.liveCalls).toHaveLength(1));
		// A second message while the first is still asking: it cannot overtake.
		const editor = (view.container.querySelector('.ProseMirror') as HTMLElement & { editor: { commands: { setContent(text: string): void } } }).editor;
		editor.commands.setContent('second');
		await fireEvent.submit(form);
		await new Promise((resolve) => setTimeout(resolve, 50));
		expect(sent).toEqual([]);
		answer({ session_meta: { is_remote: false } });
		// The second waited for the first (#1344) and goes right after it.
		await waitFor(() => expect(sent).toEqual([BY_PATH, 'second']));
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

describe('the note form is asked after every wait of the real send (S16)', () => {
	// The real store send, not a mock: its socket and restore waits run, and
	// the chat moves to another worker while the send waits.
	class Socket {
		static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
		static all: Socket[] = [];
		readyState = 0;
		onopen?: () => void; onmessage?: (event: { data: string }) => void; onclose?: (event: { code: number }) => void;
		onerror?: () => void;
		frames: Record<string, unknown>[] = [];
		constructor() { Socket.all.push(this); }
		send = vi.fn((data: string) => { this.frames.push(JSON.parse(data)); });
		close = vi.fn(() => { this.readyState = 3; });
		open() { this.readyState = 1; this.onopen?.(); }
	}
	const queries = () => Socket.all.flatMap((socket) => socket.frames.filter((frame) => frame.type === 'query'));

	beforeEach(() => {
		sends.mockRestore();
		voice.active = null;
		voice.sent = [];
		voice.acks = [];
		Socket.all = [];
		vi.stubGlobal('WebSocket', Socket);
	});
	afterEach(() => {
		// Gone without a close event: the next send opens a new socket.
		for (const socket of Socket.all) socket.readyState = Socket.CLOSED;
	});

	async function compose(sessionId: string, extra: Record<string, unknown> = {}) {
		start(sessionId, NOTE_ID, { isRemote: false, ...extra });
		const view = render(ChatInput);
		await waitFor(() => expect(view.container.querySelector('[data-file-chip]')).toBeTruthy());
		return view;
	}

	it('sends the note id when the chat moves while the cold-cache check waits', async () => {
		let answerCache = (_: unknown) => {};
		net.cache = () => new Promise((resolve) => { answerCache = resolve; });
		const view = await compose('cold-chat');
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(view.container.querySelector('.ProseMirror')?.textContent).toBe(''));
		// The chat runs here when Send is pressed, then moves during the check.
		net.live = () => ({ session_meta: { is_remote: true } });
		answerCache({ cold: false, warn: false, large: false });
		await waitFor(() => expect(Socket.all).toHaveLength(1));
		Socket.all[0].open();
		await waitFor(() => expect(queries()).toHaveLength(1));
		expect(queries()[0]).toMatchObject({ session_id: 'cold-chat', prompt: REF });
	});

	it('sends the note id when the chat moves while the store waits for the socket', async () => {
		const view = await compose('steer-chat', { isStreaming: true });
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(Socket.all).toHaveLength(1));
		// Asked before the socket is up, the answer would be stale: it is not asked yet.
		expect(net.liveCalls).toEqual([]);
		net.live = () => ({ session_meta: { is_remote: true } });
		Socket.all[0].open();
		await waitFor(() => expect(queries()).toHaveLength(1));
		expect(queries()[0]).toMatchObject({ session_id: 'steer-chat', prompt: REF });
		expect(net.liveCalls).toHaveLength(1);
	});

	it('sends the note id when the chat moves while the store waits for a restore', async () => {
		const view = await compose('restoring-chat', { isRestoringSession: true });
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(view.container.querySelector('.ProseMirror')?.textContent).toBe(''));
		net.live = () => ({ session_meta: { is_remote: true } });
		activeSession.current.isRestoringSession = false;
		await waitFor(() => expect(Socket.all).toHaveLength(1));
		Socket.all[0].open();
		await waitFor(() => expect(queries()).toHaveLength(1));
		expect(queries()[0]).toMatchObject({ session_id: 'restoring-chat', prompt: REF });
	});

	it('asks the server for a saved tab chat restoring after a reload, before the store has its id', async () => {
		// The reload's state: the store has no chat yet, the editor already
		// holds the tab's saved chat and its draft, "Runs on" is this worker.
		activeSession.newChat();
		// A fresh tab load: no "New chat was picked" mark from the reset above.
		sessionStorage.clear();
		sessionStorage.setItem(TAB_ACTIVE_SESSION_KEY, JSON.stringify({ sessionId: 'saved-chat', title: 'Saved' }));
		writeDraft('saved-chat', draftWith(NOTE_ID));
		Object.assign(activeSession.current, {
			sessionId: null, isRestoringSession: true, deviceIntent: null, mode: 'chat', profile: 'coding',
			isStreaming: false, liveTurnId: null, error: null, worktreeError: null, isRemote: false
		});
		const view = render(ChatInput);
		await waitFor(() => expect(view.container.querySelector('[data-file-chip]')).toBeTruthy());
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(view.container.querySelector('.ProseMirror')?.textContent).toBe(''));
		// The chat moves to another worker during the restore, then is adopted.
		net.live = () => ({ session_meta: { is_remote: true } });
		Object.assign(activeSession.current, { sessionId: 'saved-chat', isRestoringSession: false });
		await waitFor(() => expect(Socket.all).toHaveLength(1));
		Socket.all[0].open();
		await waitFor(() => expect(queries()).toHaveLength(1));
		expect(queries()[0]).toMatchObject({ session_id: 'saved-chat', prompt: REF });
		expect(net.liveCalls).toHaveLength(1);
		expect(net.liveCalls[0]).toContain('/sessions/saved-chat/live?');
	});

	it('still sends the file path to a chat that runs here after the waits', async () => {
		const view = await compose('local-chat');
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(Socket.all).toHaveLength(1));
		Socket.all[0].open();
		await waitFor(() => expect(queries()).toHaveLength(1));
		expect(queries()[0]).toMatchObject({ session_id: 'local-chat', prompt: BY_PATH });
	});

	it('writes a second quick send after the first one\'s frame, not in the gap after its note answer', async () => {
		let answer = (_: unknown) => {};
		net.live = () => new Promise((resolve) => { answer = resolve; });
		const view = await compose('order-chat', { isStreaming: true });
		const form = view.container.querySelector('form')!;
		await fireEvent.submit(form);
		await waitFor(() => expect(Socket.all).toHaveLength(1));
		Socket.all[0].open();
		await waitFor(() => expect(net.liveCalls).toHaveLength(1));
		const editor = (view.container.querySelector('.ProseMirror') as HTMLElement & { editor: { commands: { setContent(text: string): void } } }).editor;
		editor.commands.setContent('second');
		await fireEvent.submit(form);
		await new Promise((resolve) => setTimeout(resolve, 50));
		expect(queries()).toEqual([]);
		answer({ session_meta: { is_remote: false } });
		await waitFor(() => expect(queries()).toHaveLength(2));
		expect(queries().map((frame) => frame.prompt)).toEqual([BY_PATH, 'second']);
	});

	it('lets the next send go once a voice call note send has written its frame, and never sends it again when its acknowledgement is lost', async () => {
		voice.active = 'voice-chat';
		const view = await compose('voice-chat', { isStreaming: true });
		const form = view.container.querySelector('form')!;
		await fireEvent.submit(form);
		await waitFor(() => expect(voice.sent).toEqual([BY_PATH]));
		const editor = (view.container.querySelector('.ProseMirror') as HTMLElement & { editor: { commands: { setContent(text: string): void } } }).editor;
		editor.commands.setContent('second');
		await fireEvent.submit(form);
		// The first frame is written, its acknowledgement still awaited: the second goes.
		await waitFor(() => expect(voice.sent).toEqual([BY_PATH, 'second']));
		voice.acks[1].resolve(true);
		// The first one's acknowledgement never comes: it times out.
		voice.acks[0].reject(new Error('Could not confirm message delivery. Check the conversation before retrying.'));
		await waitFor(() => expect(view.container.querySelector('[data-file-chip]')).toBeTruthy());
		await new Promise((resolve) => setTimeout(resolve, 150));
		// Its words are back in the composer; nothing went out again by itself.
		expect(voice.sent).toEqual([BY_PATH, 'second']);
		expect(view.container.querySelector('.ProseMirror')?.textContent).toContain('read');
	});

	it('sends nothing into another chat opened while the server was asked, and keeps the draft with its chat', async () => {
		let answer = (_: unknown) => {};
		net.live = () => new Promise((resolve) => { answer = resolve; });
		const view = await compose('open-chat', { isStreaming: true });
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(Socket.all).toHaveLength(1));
		Socket.all[0].open();
		await waitFor(() => expect(net.liveCalls).toHaveLength(1));
		activeSession.newChat();
		answer({ session_meta: { is_remote: false } });
		await new Promise((resolve) => setTimeout(resolve, 150));
		expect(queries()).toEqual([]);
		expect(readDraft('open-chat')).toContain(NOTE_ID);
	});

	// #1444: the order lives in the store, so it holds whichever component sent.
	it('writes a plain send that waited for the socket before a note send made as the socket opens (#1444 case 1)', async () => {
		start('reconnect-chat', NOTE_ID, { isRemote: false }, false);
		const view = render(ChatInput);
		await waitFor(() => expect(view.container.querySelector('.ProseMirror')?.textContent).toContain('read the plan'));
		const form = view.container.querySelector('form')!;
		await fireEvent.submit(form);
		// The socket is down: the store opens one and waits for it.
		await waitFor(() => expect(Socket.all).toHaveLength(1));
		await waitFor(() => expect(view.container.querySelector('.ProseMirror')?.textContent).toBe(''));
		const editor = (view.container.querySelector('.ProseMirror') as HTMLElement & { editor: { commands: { setContent(content: unknown): void } } }).editor;
		editor.commands.setContent(JSON.parse(draftWith(NOTE_ID)));
		// It opens, and the note send goes before the store's poll has seen it.
		Socket.all[0].open();
		await fireEvent.submit(form);
		await waitFor(() => expect(queries()).toHaveLength(2));
		expect(queries().map((frame) => frame.prompt)).toEqual(['read the plan', BY_PATH]);
	});

	it('writes a Browser panel prompt sent while a note send waits for its ownership answer after the note (#1444 case 3)', async () => {
		let answer = (_: unknown) => {};
		net.live = () => new Promise((resolve) => { answer = resolve; });
		const view = await compose('panel-chat', { isStreaming: true });
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(Socket.all).toHaveLength(1));
		Socket.all[0].open();
		await waitFor(() => expect(net.liveCalls).toHaveLength(1));
		// What BrowserPanel's sendElementPrompt does: straight to the store.
		const panel = activeSession.sendMessage('panel prompt');
		await new Promise((resolve) => setTimeout(resolve, 50));
		expect(queries()).toEqual([]);
		answer({ session_meta: { is_remote: false } });
		await waitFor(() => expect(queries()).toHaveLength(2));
		expect(queries().map((frame) => frame.prompt)).toEqual([BY_PATH, 'panel prompt']);
		await expect(panel).resolves.toBe(true);
	});
});

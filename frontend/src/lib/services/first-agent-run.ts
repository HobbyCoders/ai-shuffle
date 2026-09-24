/**
 * The setup wizard's first agent run: one short prompt in a new chat session,
 * with the reply streamed back so the owner sees an agent answer.
 *
 * It uses the same paths as the app's composer: the turn is started over the
 * chat WebSocket (`/api/v1/ws/chat`, a `query` frame with `mode: 'chat'`, so
 * the session is a chat session with no project checkout and no file tools),
 * and the reply is read from the session's event feed
 * (`/api/v1/sessions/{id}/events`). The run goes through the worker's normal
 * turn start, so the credential and license gates apply to it exactly as to
 * any other run, and whatever they refuse comes back as the error.
 *
 * Two refusals carry a code on the chat WebSocket's `error` frame
 * (`{ type: 'error', session_id, message, code }`):
 * - `agent_setup_required`: the credential gate refused the turn (no license,
 *   or no credential for the active Claude Code mode). `message` says what is
 *   missing and where to fix it.
 * - `loop_guard_confirm`: the subscription loop guard held an unattended turn
 *   for the owner's confirmation. `message` says why and where to confirm it.
 * The session's event feed reports the same refusal as `turn.aborted` without
 * the code, often first, so an abort waits briefly for the coded frame.
 */
import { api, getErrorMessage, type Profile } from '$lib/api/client';
import { wsUrl } from '$lib/api/runtime';
import { createSseFeed, type SseFeed } from '$lib/stores/session/v2/sse-feed';

export const FIRST_RUN_PROMPT = 'Say hello in one short sentence, then list three things you can help me with, one line each.';

/** How long to wait for the reply before giving up. */
export const FIRST_RUN_TIMEOUT_MS = 180_000;

/** How long an abort from the feed waits for the WebSocket's coded error frame. */
export const ABORT_GRACE_MS = 1000;

/** The credential gate refused the turn: setup is not finished. */
export const SETUP_REQUIRED_CODE = 'agent_setup_required';
/** The subscription loop guard held the turn for the owner's confirmation. */
export const LOOP_GUARD_CODE = 'loop_guard_confirm';
export type FirstRunErrorCode = typeof SETUP_REQUIRED_CODE | typeof LOOP_GUARD_CODE;

function errorCode(value: unknown): FirstRunErrorCode | null {
	return value === SETUP_REQUIRED_CODE || value === LOOP_GUARD_CODE ? value : null;
}

export type FirstRunPhase = 'starting' | 'waiting' | 'streaming' | 'done' | 'failed' | 'needs-input';

export interface FirstRunState {
	phase: FirstRunPhase;
	/** The reply so far. */
	text: string;
	sessionId: string | null;
	/** Why the run failed or stopped, fit to show the user. */
	error: string | null;
	/** The worker's code for a refused or held turn, when it gave one. */
	code: FirstRunErrorCode | null;
}

export interface FirstRunHandle {
	/** Settles with the final state; never rejects. */
	done: Promise<FirstRunState>;
	/** Stop listening. The worker finishes or stops the turn on its own. */
	cancel: () => void;
}

const STARTER_AGENT = {
	name: 'Assistant',
	description: 'Created by setup for your first run. Rename, change or delete it any time.',
	config: {},
};

/** The agent the first run uses: the first existing one, or a new "Assistant". */
export async function ensureStarterAgent(): Promise<Profile> {
	const agents = await api.get<Profile[]>('/agents');
	if (Array.isArray(agents) && agents.length > 0) return agents[0];
	return api.post<Profile>('/agents', STARTER_AGENT);
}

function randomId(): string {
	try {
		if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
	} catch {
		// Fall through to the non-cryptographic id; it only matches our own frame.
	}
	return `setup-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function str(value: unknown): string {
	return typeof value === 'string' ? value : '';
}

interface StartOptions {
	profileId: string;
	prompt?: string;
	onUpdate?: (state: FirstRunState) => void;
	timeoutMs?: number;
	/** Test seams. */
	openSocket?: (url: string) => WebSocket;
	openFeed?: (sessionId: string, onEvent: (event: Record<string, unknown>) => void) => Pick<SseFeed, 'stop'>;
}

export function startFirstAgentRun(options: StartOptions): FirstRunHandle {
	const prompt = options.prompt ?? FIRST_RUN_PROMPT;
	const requestId = randomId();
	const state: FirstRunState = { phase: 'starting', text: '', sessionId: null, error: null, code: null };
	let socket: WebSocket | null = null;
	let feed: Pick<SseFeed, 'stop'> | null = null;
	let settled = false;
	let sawDelta = false;
	let timeout: ReturnType<typeof setTimeout> | undefined;
	let doneGrace: ReturnType<typeof setTimeout> | undefined;
	let abortGrace: ReturnType<typeof setTimeout> | undefined;
	let resolveDone!: (value: FirstRunState) => void;
	const done = new Promise<FirstRunState>((resolve) => { resolveDone = resolve; });

	const publish = () => options.onUpdate?.({ ...state });

	function teardown() {
		clearTimeout(timeout);
		clearTimeout(doneGrace);
		clearTimeout(abortGrace);
		try { feed?.stop(); } catch { /* already stopped */ }
		feed = null;
		if (socket) {
			const s = socket;
			socket = null;
			s.onmessage = null; s.onclose = null; s.onerror = null;
			try { s.close(1000, 'Setup run finished'); } catch { /* already closed */ }
		}
	}

	function finish(phase: 'done' | 'failed' | 'needs-input', error: string | null = null, code: FirstRunErrorCode | null = null) {
		if (settled) return;
		settled = true;
		state.phase = phase;
		state.error = error;
		state.code = code;
		if (phase === 'done' && !state.text.trim()) {
			state.phase = 'failed';
			state.error = 'The agent finished without a reply. Open the session from the sidebar to see what happened.';
		}
		teardown();
		publish();
		resolveDone({ ...state });
	}

	function onFeedEvent(event: Record<string, unknown>) {
		if (settled) return;
		switch (event.type) {
			case 'assistant.text_delta': {
				sawDelta = true;
				state.text += str(event.text);
				state.phase = 'streaming';
				publish();
				break;
			}
			case 'message.assistant': {
				// A closed reply with no deltas (or the full text after them).
				if (str(event.kind) === 'thinking') break;
				const content = str(event.content);
				if (content && (!sawDelta || content.length > state.text.length)) {
					state.text = content;
					state.phase = 'streaming';
					publish();
				}
				break;
			}
			case 'turn.completed':
				finish('done');
				break;
			case 'turn.aborted': {
				// A refused turn is reported here without its code; the
				// WebSocket's error frame carries it and settles the run first
				// when it arrives within the grace.
				const message = str(event.message) || 'The run stopped before the agent answered.';
				clearTimeout(abortGrace);
				abortGrace = setTimeout(() => finish('failed', message), ABORT_GRACE_MS);
				break;
			}
			case 'error':
				finish('failed', str(event.message) || 'The agent could not start.');
				break;
			case 'interaction.requested':
				finish('needs-input', 'The agent is asking for a decision. Open the session from the sidebar to answer it.');
				break;
		}
	}

	function onSocketMessage(raw: MessageEvent) {
		let frame: Record<string, unknown>;
		try { frame = JSON.parse(String(raw.data)); } catch { return; }
		const type = str(frame.type);
		if (type === 'ping') {
			try { socket?.send(JSON.stringify({ type: 'pong' })); } catch { /* closing */ }
			return;
		}
		if (type === 'session_created' && str(frame.request_id) === requestId && !state.sessionId) {
			state.sessionId = str(frame.session_id);
			state.phase = 'waiting';
			publish();
			const open = options.openFeed ?? ((id, onEvent) => createSseFeed(id, { onEvent }));
			feed = open(state.sessionId, onFeedEvent);
			return;
		}
		// Frames for other sessions share this socket; only ours matter.
		const frameSession = str(frame.session_id);
		if (frameSession && state.sessionId && frameSession !== state.sessionId) return;
		if (type === 'error') {
			finish('failed', str(frame.message) || 'The agent could not start.', errorCode(frame.code));
		} else if (type === 'permission_request' || type === 'user_question') {
			finish('needs-input', 'The agent is asking for a decision. Open the session from the sidebar to answer it.');
		} else if (type === 'done' && state.sessionId) {
			// The feed normally reports completion itself; give it a moment.
			clearTimeout(doneGrace);
			doneGrace = setTimeout(() => finish('done'), 3000);
		}
	}

	try {
		const url = wsUrl('/api/v1/ws/chat', { device_id: `setup-${requestId}` });
		socket = options.openSocket ? options.openSocket(url) : new WebSocket(url);
		socket.onopen = () => {
			try {
				socket?.send(JSON.stringify({ type: 'query', prompt, profile: options.profileId, mode: 'chat', request_id: requestId }));
			} catch (e) {
				finish('failed', getErrorMessage(e, 'Could not reach the worker.'));
			}
		};
		socket.onmessage = onSocketMessage;
		socket.onerror = () => { if (!state.sessionId) finish('failed', 'Could not reach the worker. Check that it is running, then try again.'); };
		socket.onclose = (event: CloseEvent) => {
			if (settled || state.sessionId) return;
			finish('failed', event.code === 4001 ? 'This browser is not signed in to the worker. Reload the page and sign in.' : 'The connection to the worker closed before the run started.');
		};
	} catch (e) {
		finish('failed', getErrorMessage(e, 'Could not reach the worker.'));
	}

	timeout = setTimeout(() => {
		if (state.text.trim()) finish('done');
		else finish('failed', 'No answer arrived in time. Open the session from the sidebar to see what happened, or try again.');
	}, options.timeoutMs ?? FIRST_RUN_TIMEOUT_MS);
	publish();

	return { done, cancel: () => finish('failed', 'Cancelled.') };
}

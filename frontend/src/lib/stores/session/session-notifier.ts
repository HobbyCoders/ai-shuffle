/**
 * Session Notifier - WebSocket notification listener.
 *
 * The notifier listens for WebSocket events and triggers polls.
 * It NEVER updates session state directly.
 *
 * Responsibilities:
 * 1. Listen for 'update' / stream events from WebSocket
 * 2. Call sessionPoller.pollNow() when updates arrive
 * 3. Call sessionPoller.startStreaming() when streaming starts
 * 4. That's it. No state management, no buffering, no merging.
 *
 * This module is intentionally simple - all complexity lives in
 * the backend (live_session_store.py) and the poller.
 */

import { sessionPoller } from './session-poller';

// ─── Types ───────────────────────────────────────────────────────────────────

/** Minimal WebSocket event shape from the backend */
interface WsNotification {
	type?: string;
	event_type?: string;
	session_id?: string;
	is_streaming?: boolean;
	[key: string]: unknown;
}

type NotifierCallback = (event: WsNotification) => void;

// ─── Notifier Class ──────────────────────────────────────────────────────────

class SessionNotifier {
	/** Callbacks for events that need additional handling (permissions, questions, etc.) */
	private _listeners: NotifierCallback[] = [];

	/**
	 * Process an incoming WebSocket message for a session.
	 *
	 * Called from the WebSocket onmessage handler in tabs.ts.
	 *
	 * @param sessionId - The session this message belongs to
	 * @param data - The parsed WebSocket message
	 */
	handleMessage(sessionId: string, data: WsNotification): void {
		const msgType = data.type;
		const eventType = data.event_type;

		// ── Stream lifecycle events → start/stop polling ──────────────────

		if (msgType === 'start' || msgType === 'stream_start' || eventType === 'stream_start') {
			// Streaming has started - begin fast polling
			sessionPoller.startStreaming(sessionId);
			return;
		}

		if (msgType === 'done' || msgType === 'stopped' || msgType === 'interrupted' || eventType === 'stream_end') {
			// Streaming ended - trigger one final poll, poller will stop itself
			sessionPoller.pollNow(sessionId);
			return;
		}

		// ── Update notifications → trigger immediate poll ─────────────────

		// Any streaming data event means state has changed on the server
		if (isUpdateEvent(msgType) || isUpdateEvent(eventType)) {
			// Resilient fallback: ANY server-side update event for a session we
			// are NOT currently polling means the server holds live state we are
			// not tracking. The critical case is a SERVER-INITIATED WAKE turn (an
			// unpark: a backgrounded agent came to rest while this session was
			// parked, so the CLI/app started a follow-up turn). Its `stream_start`
			// broadcast is NOT reliably re-delivered to a re-attached / idle socket
			// (see session-poller's switch-back note), and it can also arrive as a
			// `message_added` / `system` / `subagent_*` / `messages_updated` event
			// rather than a `stream_chunk`. Previously only the `stream_chunk`
			// family restarted a stopped poller; every other update event fell to a
			// one-shot `pollNow`, which renders the delivered messages but never
			// ARMS a running poller — so the wake's `is_streaming=true` flip is
			// never observed and the streaming indicator + stop button never appear
			// (the session looks idle yet is streaming and cannot be interrupted).
			//
			// Start a real poll loop instead: it converges on `/live`'s
			// `is_streaming` regardless of the missed WS edge, and self-limits — a
			// poll that turns out fully idle (no streaming, no active tasks, no
			// `unpark_pending`) stops the poller on the very next tick.
			if (!sessionPoller.isPolling(sessionId)) {
				sessionPoller.startStreaming(sessionId);
				return;
			}
			sessionPoller.pollNow(sessionId);
		}

		// ── Forward to listeners for non-polling concerns ─────────────────
		// (permission requests, user questions, browser tool results, etc.)
		// Inject session_id so listeners can use it (e.g., browser store for screenshots)
		const enrichedData = { ...data, session_id: sessionId };
		for (const listener of this._listeners) {
			try {
				listener(enrichedData);
			} catch (e) {
				console.error('[Notifier] Listener error:', e);
			}
		}
	}

	/**
	 * Register a listener for events that need special handling.
	 * Returns an unsubscribe function.
	 */
	onEvent(callback: NotifierCallback): () => void {
		this._listeners.push(callback);
		return () => {
			this._listeners = this._listeners.filter(l => l !== callback);
		};
	}
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Events that indicate server state has changed */
const UPDATE_EVENTS = new Set([
	// Streaming data events
	'chunk',
	'stream_delta',
	'stream_block_start',
	'stream_block_stop',
	'stream_message_delta',
	'system',
	// Tool events
	'tool_use',
	'tool_result',
	// Subagent events
	'subagent_start',
	'subagent_text',
	'subagent_chunk',
	'subagent_tool_use',
	'subagent_tool_result',
	'subagent_done',
	// Usage & error
	'usage_update',
	'error',
	// Plugin events
	'plugin_message',
	// Sync engine broadcast events
	'stream_chunk',
	'message_added',
	'messages_updated',
	'streaming_update',
]);

function isUpdateEvent(type: string | undefined): boolean {
	return type !== undefined && UPDATE_EVENTS.has(type);
}

// ─── Export singleton ────────────────────────────────────────────────────────

export const sessionNotifier = new SessionNotifier();

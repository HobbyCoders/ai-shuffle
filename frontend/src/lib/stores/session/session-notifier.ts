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
			sessionPoller.pollNow(sessionId);
		}

		// ── Forward to listeners for non-polling concerns ─────────────────
		// (permission requests, user questions, etc.)
		for (const listener of this._listeners) {
			try {
				listener(data);
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

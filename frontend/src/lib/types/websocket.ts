/**
 * WebSocket Message Types — Discriminated unions for all WS messages
 * between the frontend and backend.
 *
 * These types cover:
 * - Client -> Server messages (sent via wsConn.send())
 * - Server -> Client messages (received in ws-message-handlers.ts)
 * - Server -> Client sync events (broadcast via SyncEngine, have event_type field)
 * - Server -> Client user-level messages (broadcast to all user devices)
 *
 * The backend (Python) does not consume these types — they exist purely
 * for frontend type safety. The backend's message shapes are documented
 * here as the source of truth.
 *
 * Discriminant fields:
 * - ClientMessage / ServerMessage: discriminated on `type`
 * - SyncEvent: discriminated on `event_type`
 * - UserMessage: discriminated on `type`
 */

// =============================================================================
// Client -> Server Messages
// =============================================================================

/** Send a new query to Claude. Creates a session if session_id is omitted/null. */
export interface WsClientQuery {
	type: 'query';
	prompt: string;
	session_id?: string | null;
	profile: string;
	project?: string | null;
	overrides?: {
		model?: string;
		permission_mode?: string;
		thinking_mode?: string;
		effort?: string;
	};
	worktree_id?: string | null;
}

/** Stop/interrupt the currently running query. */
export interface WsClientStop {
	type: 'stop';
	session_id: string;
}

/**
 * Phase 1 of the two-phase interrupt+query protocol. The client asks the
 * server to interrupt the currently streaming query and drain the SDK pipe.
 * On success, the server replies with `interrupt_confirmed` and the client
 * then sends a regular `query` message. On failure the server replies with
 * `interrupt_failed` so the client can restore the user input.
 */
export interface WsClientInterruptForQuery {
	type: 'interrupt_for_query';
	session_id: string;
}

/** Load/switch to an existing session. Server responds with `history`. */
export interface WsClientLoadSession {
	type: 'load_session';
	session_id: string;
	force_jsonl?: boolean;
	/** @deprecated Use force_jsonl instead. Kept for backwards compatibility. */
	force?: boolean;
}

/** Close the current session (unregister device from session). */
export interface WsClientCloseSession {
	type: 'close_session';
}

/** Respond to a permission request from the SDK. */
export interface WsClientPermissionResponse {
	type: 'permission_response';
	request_id: string;
	session_id?: string;
	decision: 'allow' | 'deny';
	remember: 'none' | 'session' | 'profile';
	pattern?: string;
}

/** Request the list of pending permission requests for the current session. */
export interface WsClientGetPendingPermissions {
	type: 'get_pending_permissions';
}

/** Respond to a user question from the SDK. */
export interface WsClientUserQuestionResponse {
	type: 'user_question_response';
	request_id: string;
	session_id?: string;
	tool_use_id: string;
	answers: Record<string, string | string[]>;
}

/** Request the list of pending user questions for the current session. */
export interface WsClientGetPendingQuestions {
	type: 'get_pending_questions';
}

/** Broadcast tab state to other devices for tab sync. */
export interface WsClientTabsUpdate {
	type: 'tabs_update';
	tabs_data: {
		tabs?: unknown[];
		activeTabId?: string;
	};
}

/** Pong response to server ping (keep-alive). */
export interface WsClientPong {
	type: 'pong';
}

/** All possible messages the client can send to the server. */
export type ClientMessage =
	| WsClientQuery
	| WsClientStop
	| WsClientInterruptForQuery
	| WsClientLoadSession
	| WsClientCloseSession
	| WsClientPermissionResponse
	| WsClientGetPendingPermissions
	| WsClientUserQuestionResponse
	| WsClientGetPendingQuestions
	| WsClientTabsUpdate
	| WsClientPong;

// =============================================================================
// Server -> Client Messages (direct, per-session)
// =============================================================================

/** Full session history sent on load_session. */
export interface WsServerHistory {
	type: 'history';
	session_id: string;
	session: Record<string, unknown>;
	messages: unknown[];
	isStreaming: boolean;
	streamingBuffer?: unknown[];
	streamingUsage?: Record<string, number>;
	activeBlockType?: string;
	pendingPermissions?: unknown[];
	pendingQuestions?: unknown[];
}

/** New session was created (sent immediately after session creation). */
export interface WsServerSessionCreated {
	type: 'session_created';
	session_id: string;
	worktree_id?: string;
}

/** Query accepted and streaming is about to begin. */
export interface WsServerStart {
	type: 'start';
	session_id: string;
	message_id: string;
}

/** Text content chunk (legacy, when include_partial_messages=False). */
export interface WsServerChunk {
	type: 'chunk';
	content: string;
	session_id?: string;
}

/** Real-time streaming delta (text, thinking, or tool_input). */
export interface WsServerStreamDelta {
	type: 'stream_delta';
	content: string;
	delta_type: 'text' | 'thinking' | 'tool_input';
	index?: number;
	session_id?: string;
}

/** Start of a new streaming content block. */
export interface WsServerStreamBlockStart {
	type: 'stream_block_start';
	block_type: 'thinking' | 'text' | 'tool_use';
	index?: number;
	content_block?: Record<string, unknown>;
	session_id?: string;
}

/** End of a streaming content block. */
export interface WsServerStreamBlockStop {
	type: 'stream_block_stop';
	index?: number;
	session_id?: string;
}

/** Final message metadata with output token usage (end of an assistant turn). */
export interface WsServerStreamMessageDelta {
	type: 'stream_message_delta';
	delta?: Record<string, unknown>;
	usage?: Record<string, number>;
	session_id?: string;
}

/** Start of an SDK streaming message (carries input token usage). */
export interface WsServerStreamStart {
	type: 'stream_start';
	message?: {
		usage?: {
			input_tokens?: number;
			output_tokens?: number;
			cache_creation_input_tokens?: number;
			cache_read_input_tokens?: number;
		};
		[key: string]: unknown;
	};
	session_id?: string;
}

/** Tool use event from the SDK. */
export interface WsServerToolUse {
	type: 'tool_use';
	id: string;
	name: string;
	input: Record<string, unknown>;
	session_id?: string;
}

/** Tool result event from the SDK. */
export interface WsServerToolResult {
	type: 'tool_result';
	tool_use_id: string;
	output: string;
	is_error?: boolean;
	session_id?: string;
}

/** System message (e.g., /context output, local commands). */
export interface WsServerSystem {
	type: 'system';
	subtype?: string;
	data?: Record<string, unknown>;
	content?: string;
	session_id?: string;
}

/** Subagent task started. */
export interface WsServerSubagentStart {
	type: 'subagent_start';
	agent_id: string;
	agent_type?: string;
	description?: string;
	prompt?: string;
	tool_id?: string;
	session_id?: string;
}

/** Text chunk from a subagent. */
export interface WsServerSubagentChunk {
	type: 'subagent_chunk';
	agent_id: string;
	content: string;
	session_id?: string;
}

/** Tool use within a subagent. */
export interface WsServerSubagentToolUse {
	type: 'subagent_tool_use';
	agent_id: string;
	id: string;
	name: string;
	input: Record<string, unknown>;
	session_id?: string;
}

/** Tool result within a subagent. */
export interface WsServerSubagentToolResult {
	type: 'subagent_tool_result';
	agent_id: string;
	tool_use_id: string;
	output: string;
	is_error?: boolean;
	session_id?: string;
}

/** Subagent completed. */
export interface WsServerSubagentDone {
	type: 'subagent_done';
	agent_id: string;
	result?: string;
	is_error?: boolean;
	session_id?: string;
}

/** Query completed successfully with metadata. */
export interface WsServerDone {
	type: 'done';
	session_id?: string;
	metadata?: {
		total_cost_usd?: number;
		tokens_in?: number;
		tokens_out?: number;
		model?: string;
		[key: string]: unknown;
	};
}

/** Query was stopped/interrupted by the user. */
export interface WsServerStopped {
	type: 'stopped';
	session_id: string;
}

/** Query was interrupted (SDK-level interrupt completed). */
export interface WsServerInterrupted {
	type: 'interrupted';
	session_id?: string;
	metadata?: Record<string, unknown>;
}

/**
 * Phase-1 ACK of a `interrupt_for_query` request. The streaming pipe has
 * been drained and the client may now send its follow-up `query`.
 */
export interface WsServerInterruptConfirmed {
	type: 'interrupt_confirmed';
	session_id: string;
}

/**
 * Phase-1 NACK of a `interrupt_for_query` request. The client should
 * surface an error and restore the user input instead of silently
 * dropping it.
 */
export interface WsServerInterruptFailed {
	type: 'interrupt_failed';
	session_id: string | null;
	message: string;
}

/** Error occurred during query or message processing. */
export interface WsServerError {
	type: 'error';
	message: string;
	session_id?: string;
}

/** Session not found (e.g., deleted or invalid ID). */
export interface WsServerSessionNotFound {
	type: 'session_not_found';
	session_id: string;
}

/** Session is already open on another tab of the same device. */
export interface WsServerSessionAlreadyOpen {
	type: 'session_already_open';
	session_id: string;
	message: string;
}

/** Session was closed (confirmation of close_session). */
export interface WsServerSessionClosed {
	type: 'session_closed';
	session_id: string;
}

/** Session is locked by another device. */
export interface WsServerSessionLocked {
	type: 'session_locked';
	locked_by: string;
	session_id?: string;
}

/** Session was taken over by another device. */
export interface WsServerSessionTakeover {
	type: 'session_takeover';
	new_device: string;
	session_id?: string;
}

/** Server ping — client should respond with pong. */
export interface WsServerPing {
	type: 'ping';
	timestamp?: number | null;
}

/** Heartbeat during streaming with pending permission/question counts. */
export interface WsServerHeartbeat {
	type: 'heartbeat';
	timestamp: number;
	pending_permissions?: number;
	pending_questions?: number;
}

/** Permission request from the SDK (tool needs approval). */
export interface WsServerPermissionRequest {
	type: 'permission_request';
	request_id: string;
	tool_name: string;
	tool_input: Record<string, unknown>;
	queue_position?: number;
	queue_total?: number;
	session_id?: string;
}

/** Batch update: some permission requests were resolved. */
export interface WsServerPermissionQueueUpdate {
	type: 'permission_queue_update';
	resolved_ids: string[];
	session_id?: string;
}

/** Acknowledgment that a permission response was processed. */
export interface WsServerPermissionResponseAck {
	type: 'permission_response_ack';
	request_id: string;
	result?: unknown;
	session_id?: string;
}

/** A permission request timed out. */
export interface WsServerPermissionTimeout {
	type: 'permission_timeout';
	request_id: string;
	session_id?: string;
}

/** Full list of pending permissions for the current session. */
export interface WsServerPendingPermissions {
	type: 'pending_permissions';
	session_id: string;
	requests: unknown[];
}

/** User question from the SDK (interactive questions for the user). */
export interface WsServerUserQuestion {
	type: 'user_question';
	request_id: string;
	tool_use_id: string;
	questions: Array<{
		question: string;
		header?: string;
		options?: Array<{ label: string; description?: string }>;
		multiSelect?: boolean;
	}>;
	session_id?: string;
}

/** Acknowledgment that a user question response was processed. */
export interface WsServerUserQuestionResponseAck {
	type: 'user_question_response_ack';
	request_id: string;
	success?: boolean;
	session_id?: string;
}

/** A user question timed out. */
export interface WsServerUserQuestionTimeout {
	type: 'user_question_timeout';
	request_id: string;
	session_id?: string;
}

/** Full list of pending user questions for the current session. */
export interface WsServerPendingQuestions {
	type: 'pending_questions';
	session_id: string;
	questions: unknown[];
}

/** Message was queued for later processing. */
export interface WsServerMessageQueued {
	type: 'message_queued';
	session_id?: string;
}

/** A queued message is being processed. */
export interface WsServerQueuedMessageProcessing {
	type: 'queued_message_processing';
	session_id?: string;
}

/** Plugin message from an MCP plugin or similar. */
export interface WsServerPluginMessage {
	type: 'plugin_message';
	[key: string]: unknown;
}

/** Token usage update during streaming (agentic loops). */
export interface WsServerUsageUpdate {
	type: 'usage_update';
	usage: Record<string, number>;
	session_id?: string;
}

// =============================================================================
// Server -> Client User-level Messages (broadcast to all user devices)
// =============================================================================

/** Tab sync data from another device. */
export interface WsServerTabsSync {
	type: 'tabs_sync';
	tabs: unknown[];
	activeTabId?: string;
	source_device_id?: string;
}

/** A new session was created on another device. */
export interface WsServerUserSessionCreated {
	type: 'user_session_created';
	session_id: string;
}

/** A session's streaming state changed (for side panel indicators). */
export interface WsServerUserStreamingChanged {
	type: 'user_streaming_changed';
	session_id: string;
	is_streaming: boolean;
	project_id?: string | null;
	worktree_id?: string | null;
}

/** Snapshot of all currently streaming sessions for this user (sent on connect). */
export interface WsServerUserStreamingSessions {
	type: 'user_streaming_sessions';
	sessions: Array<{
		session_id: string;
		project_id?: string | null;
		worktree_id?: string | null;
	}>;
	session_ids?: string[];
}

/** A branch was changed on another device. */
export interface WsServerUserBranchChanged {
	type: 'user_branch_changed';
	project_id: string;
	branch_name: string;
}

// =============================================================================
// Server -> Client Sync Events (SyncEngine broadcasts, have event_type field)
// =============================================================================

/** Base shape for SyncEngine broadcast events. */
interface SyncEventBase {
	event_type: string;
	session_id: string;
	timestamp?: string;
	source_device_id?: string | null;
}

/** Streaming started on another device. */
export interface WsSyncStreamStart extends SyncEventBase {
	event_type: 'stream_start';
	data: {
		message_id: string;
		usage?: Record<string, number>;
	};
}

/** Streaming chunk from another device. */
export interface WsSyncStreamChunk extends SyncEventBase {
	event_type: 'stream_chunk';
	data: {
		message_id: string;
		chunk_type: string;
		content?: string;
		[key: string]: unknown;
	};
}

/** Streaming ended on another device. */
export interface WsSyncStreamEnd extends SyncEventBase {
	event_type: 'stream_end';
	data: {
		message_id: string;
		metadata: Record<string, unknown>;
		interrupted: boolean;
	};
}

/** Usage update during streaming (agentic loops). */
export interface WsSyncUsageUpdate extends SyncEventBase {
	event_type: 'usage_update';
	data: {
		usage: Record<string, number>;
	};
}

/** A new message was added (user message from another device). */
export interface WsSyncMessageAdded extends SyncEventBase {
	event_type: 'message_added';
	data: {
		message: Record<string, unknown>;
	};
}

/** Session metadata was updated. */
export interface WsSyncSessionUpdated extends SyncEventBase {
	event_type: 'session_updated';
	data: {
		updates: Record<string, unknown>;
	};
}

/** Session was rewound (messages removed). */
export interface WsSyncSessionRewound extends SyncEventBase {
	event_type: 'session_rewound';
	data: {
		target_uuid: string;
		messages_removed: number;
	};
}

/** Another device opened a session. */
export interface WsSyncSessionOpened extends SyncEventBase {
	event_type: 'session_opened';
	data: {
		device_id: string;
		is_new: boolean;
	};
}

/** Another device closed a session. */
export interface WsSyncSessionClosed extends SyncEventBase {
	event_type: 'session_closed';
	data: {
		device_id: string;
	};
}

/** Initial state sent on legacy sync WebSocket connect. */
export interface WsSyncState extends SyncEventBase {
	event_type: 'state';
	data: {
		session_id: string;
		is_streaming: boolean;
		connected_devices: number;
		streaming_messages?: unknown[];
		session?: Record<string, unknown>;
		messages?: unknown[];
	};
}

/** All possible sync events from SyncEngine. */
export type SyncEvent =
	| WsSyncStreamStart
	| WsSyncStreamChunk
	| WsSyncStreamEnd
	| WsSyncUsageUpdate
	| WsSyncMessageAdded
	| WsSyncSessionUpdated
	| WsSyncSessionRewound
	| WsSyncSessionOpened
	| WsSyncSessionClosed
	| WsSyncState;

// =============================================================================
// Union of ALL server -> client messages
// =============================================================================

/** All possible direct server messages (discriminated on `type`). */
export type ServerMessage =
	// Session lifecycle
	| WsServerHistory
	| WsServerSessionCreated
	| WsServerSessionNotFound
	| WsServerSessionAlreadyOpen
	| WsServerSessionClosed
	| WsServerSessionLocked
	| WsServerSessionTakeover
	// Streaming lifecycle
	| WsServerStart
	| WsServerStreamStart
	| WsServerChunk
	| WsServerStreamDelta
	| WsServerStreamBlockStart
	| WsServerStreamBlockStop
	| WsServerStreamMessageDelta
	| WsServerDone
	| WsServerStopped
	| WsServerInterrupted
	| WsServerInterruptConfirmed
	| WsServerInterruptFailed
	// Tool events
	| WsServerToolUse
	| WsServerToolResult
	| WsServerSystem
	// Subagent events
	| WsServerSubagentStart
	| WsServerSubagentChunk
	| WsServerSubagentToolUse
	| WsServerSubagentToolResult
	| WsServerSubagentDone
	// Errors
	| WsServerError
	// Keep-alive
	| WsServerPing
	| WsServerHeartbeat
	// Permissions
	| WsServerPermissionRequest
	| WsServerPermissionQueueUpdate
	| WsServerPermissionResponseAck
	| WsServerPermissionTimeout
	| WsServerPendingPermissions
	// User questions
	| WsServerUserQuestion
	| WsServerUserQuestionResponseAck
	| WsServerUserQuestionTimeout
	| WsServerPendingQuestions
	// Message queue
	| WsServerMessageQueued
	| WsServerQueuedMessageProcessing
	// Plugins
	| WsServerPluginMessage
	// Usage
	| WsServerUsageUpdate
	// User-level broadcasts
	| WsServerTabsSync
	| WsServerUserSessionCreated
	| WsServerUserStreamingChanged
	| WsServerUserStreamingSessions
	| WsServerUserBranchChanged;

// =============================================================================
// Type Guards
// =============================================================================

/** Check if a message is a SyncEngine broadcast event (has event_type). */
export function isSyncEvent(msg: unknown): msg is SyncEvent {
	return typeof msg === 'object' && msg !== null && 'event_type' in msg;
}

/** Check if a message is a direct server message (has type). */
export function isServerMessage(msg: unknown): msg is ServerMessage {
	return typeof msg === 'object' && msg !== null && 'type' in msg && !('event_type' in msg);
}

/**
 * Narrow a server message by its type field.
 *
 * Usage:
 *   if (msg.type === 'history') {
 *     // msg is narrowed to WsServerHistory
 *   }
 */
export function narrowServerMessage<T extends ServerMessage['type']>(
	msg: ServerMessage,
	type: T,
): msg is Extract<ServerMessage, { type: T }> {
	return msg.type === type;
}

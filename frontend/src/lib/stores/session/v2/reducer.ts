/**
 * V2 session reducer — PURE `(state, wireEvent) -> state`.
 *
 * V2 Phase 1 (docs/V2_PHASE1_DESIGN.md §6). For `v2_stream` sessions the
 * transcript is rebuilt deterministically from the append-only event log
 * (`GET /api/v1/sessions/{id}/events`) instead of the poller + 3-phase merge.
 *
 * INVARIANTS:
 *   - No I/O, no `Date.now()`, no randomness. Every timestamp is derived from
 *     the event's `created_at`, every id from event fields. Reducing the same
 *     log twice from `initialState()` yields deeply-equal state.
 *   - The message shapes produced here MUST match the `ChatMessage` contract
 *     that `MessageArea.svelte` renders. See the per-event handlers below for
 *     the exact field mapping.
 *   - Unknown event types are ignored (forward compatibility).
 */

import type { ChatMessage, SubagentChildMessage, Task, TeammateStatus } from '../../types';

// ─── Wire event shape ─────────────────────────────────────────────────────────

/**
 * A frame off the SSE feed: the DB row's payload merged with its envelope
 * (see app/api/events.py::_wire). `type` discriminates; all other fields are
 * payload/envelope and read per-type below.
 */
export interface WireEvent {
	type: string;
	id: string;
	seq: number;
	session_id?: string;
	created_at?: string;
	command_id?: string | null;
	run_id?: string | null;
	turn_id?: string | null;
	[k: string]: unknown;
}

// ─── State ────────────────────────────────────────────────────────────────────

export interface V2SessionState {
	messages: ChatMessage[];
	isStreaming: boolean;
	parked: boolean;
	/** Per-task summaries keyed by task_id — drives the tasks panel + parked
	 *  indicator. Shape matches the `Task` the UI's activeTasks already renders. */
	activeTaskSummaries: Task[];
	/** Last `turn.usage` payload (raw dict). feed-controller derives the context
	 *  gauge / token totals from it best-effort. */
	usage: Record<string, unknown> | null;
	lastSeq: number;
	/** Deterministic bookkeeping: id of the currently-open streaming text /
	 *  thinking message for the active turn (null = none open). Derived purely
	 *  from the event order, so replay is idempotent. */
	openTextId: string | null;
	openThinkingId: string | null;
	/** Id of the turn the FE currently believes is live (set on `turn.started`,
	 *  cleared on `turn.completed`/`turn.aborted`). Read at Stop-press time so
	 *  the stop can target that specific turn — a stop delivered after its turn
	 *  settled then no-ops server-side instead of interrupting the successor
	 *  (GitHub #585). */
	liveTurnId: string | null;
	/** Quiet-pump "ok" ack suppression. When a `message.user` renders a
	 *  `kind==='pump'` task-notification card we begin watching the live turn's
	 *  assistant reply: an exact "ok" ack is a bookkeeping artifact of the pump
	 *  wake and must not surface. Holds the `liveTurnId` we are watching (null =
	 *  not watching). Purely event-derived, so a from_seq=0 replay reproduces the
	 *  same suppression (R8). */
	pendingPumpAck: string | null;
	/** Buffered assistant text accumulated while `pendingPumpAck` is active and
	 *  the content is still an ack-prefix. Never rendered while it stays an
	 *  ack-prefix; the instant it exceeds the prefix the whole buffer is revealed
	 *  as a normal streaming message (delta-buffering mechanism, R5b). Always an
	 *  ack-prefix (or '') at any clear point, so dropping it is safe. */
	pumpAckBuffer: string;
	/** Model the SDK's MAIN lane is ACTUALLY running this turn, as reported by
	 *  the backend `system.note`/`model_info` event (null before the first note).
	 *  Drives the composer's reroute indicator (actual-vs-requested). Purely
	 *  event-derived, so a from_seq=0 replay self-restores it on reload. */
	liveModel: string | null;
	/** The model that was REQUESTED for the turn `liveModel` reports on (from
	 *  the same `model_info` note). Comparing actual-vs-requested per turn
	 *  avoids flagging a mere selection change as a reroute. */
	liveRequestedModel: string | null;
	/** Resolved model provider for the turn `liveModel` reports on (from the
	 *  same `model_info` note). null = native Anthropic; non-null = a gateway
	 *  provider session (e.g. OpenRouter). A provider session can never be
	 *  "rerouted" in the Anthropic-family sense, so the composer suppresses the
	 *  reroute pill whenever this is non-null (R1). */
	liveProvider: string | null;
	/** `transcript.rewound` bookkeeping: message id → seq of the event that
	 *  CREATED it. A message absent from this map is optimistic (seeded outside
	 *  the reducer, not yet reconciled to an event) and is EXEMPT from rewind
	 *  removal. Kept off the rendered ChatMessage so the shape stays byte-parity
	 *  with `convertServerMessage`. */
	messageOriginSeq: Record<string, number>;
	/** Same, for `activeTaskSummaries` entries (keyed by task id). */
	taskOriginSeq: Record<string, number>;
	/** Ids of subagent cards that the LAST `turn.completed` speculatively settled
	 *  to 'completed' via `settleStaleBackgroundWork`. If a `turn.parked` follows
	 *  (background work genuinely remains — the park gate ran AFTER turn.completed),
	 *  these were settled wrongly and are restored to 'running'. A genuine terminal
	 *  event (subagent.completed / terminal task.updated) removes an id; the next
	 *  turn.completed overwrites the whole set — so it never leaks across turns. */
	speculativelySettledCardIds: string[];
	/** Same, for `activeTaskSummaries` entries (keyed by task id). */
	speculativelySettledTaskIds: string[];
}

export function initialState(): V2SessionState {
	return {
		messages: [],
		isStreaming: false,
		parked: false,
		activeTaskSummaries: [],
		usage: null,
		lastSeq: 0,
		openTextId: null,
		openThinkingId: null,
		liveTurnId: null,
		pendingPumpAck: null,
		pumpAckBuffer: '',
		liveModel: null,
		liveRequestedModel: null,
		liveProvider: null,
		messageOriginSeq: {},
		taskOriginSeq: {},
		speculativelySettledCardIds: [],
		speculativelySettledTaskIds: [],
	};
}

// ─── Small immutable helpers ──────────────────────────────────────────────────

function replaceById(messages: ChatMessage[], id: string, patch: Partial<ChatMessage>): ChatMessage[] {
	return messages.map((m) => (m.id === id ? { ...m, ...patch } : m));
}

function findByToolId(messages: ChatMessage[], toolId: string): ChatMessage | undefined {
	return messages.find((m) => m.type === 'tool_use' && m.toolId === toolId);
}

function findSubagentByTask(messages: ChatMessage[], taskId: string): ChatMessage | undefined {
	// Subagent cards are correlated across started/progress/completed by task_id
	// (agent_id only appears on `subagent.started`). See shape note in reduce().
	return messages.find((m) => m.type === 'subagent' && m.toolId === taskId);
}

/** Close any open streaming text/thinking message (finalize it, clear pointers). */
function closeOpenText(state: V2SessionState): V2SessionState {
	if (!state.openTextId) return state;
	return {
		...state,
		messages: replaceById(state.messages, state.openTextId, { streaming: false }),
		openTextId: null,
	};
}

function closeOpenThinking(state: V2SessionState): V2SessionState {
	if (!state.openThinkingId) return state;
	return {
		...state,
		messages: replaceById(state.messages, state.openThinkingId, { streaming: false }),
		openThinkingId: null,
	};
}

function closeOpen(state: V2SessionState): V2SessionState {
	return closeOpenThinking(closeOpenText(state));
}

/**
 * Settle stale background work on a terminal turn (#609).
 *
 * INVARIANT (app/core/session_runtime.py::_finalize park gate): `turn.completed`
 * is emitted ONLY when NO active background work remains — a turn still holding
 * live background agents/tasks PARKS (turn.parked) instead of completing. So any
 * subagent card still 'running', or task summary still 'running', at the moment
 * turn.completed lands has MISSED its terminal `subagent.completed` / terminal
 * `task.updated` event (the #543 wake-turn delivery gap) and is stale.
 *
 * Reconcile it against that authoritative signal: finalize such cards/summaries
 * to 'completed'. This heals both live and on reload/replay with no /live fetch
 * (so it stays inside the pure reducer and replay determinism is preserved —
 * folding the same log twice yields the same settled state). A card that
 * subagent.completed already settled with its richer result is left untouched
 * (only status === 'running' is rewritten).
 */
function settleStaleBackgroundWork(state: V2SessionState): V2SessionState {
	// Record exactly which cards/summaries THIS turn.completed speculatively
	// settles so a following turn.parked can revert them — the park gate
	// runs AFTER turn.completed, so turn.parked proves the settle was premature.
	// This set OVERWRITES any prior turn's record (below), which is what stops it
	// leaking across turns.
	let cardsTouched = false;
	const settledCardIds: string[] = [];
	const messages = state.messages.map((m) => {
		if (m.type === 'subagent' && m.agentStatus === 'running') {
			cardsTouched = true;
			settledCardIds.push(m.id);
			const children = (m.agentChildren ?? []).map((c) =>
				c.streaming ? { ...c, streaming: false } : c
			);
			return { ...m, agentStatus: 'completed' as const, agentChildren: children };
		}
		return m;
	});
	let summariesTouched = false;
	const settledTaskIds: string[] = [];
	const activeTaskSummaries = state.activeTaskSummaries.map((t) => {
		if (t.status === 'running') {
			summariesTouched = true;
			settledTaskIds.push(t.id);
			return { ...t, status: 'completed' as const };
		}
		return t;
	});
	if (!cardsTouched && !summariesTouched) {
		// Nothing stale this turn. Still overwrite (clear) any prior record so a
		// stale settle from an earlier turn can never be revived by a later park.
		if (
			state.speculativelySettledCardIds.length === 0 &&
			state.speculativelySettledTaskIds.length === 0
		)
			return state; // preserve identity (true no-op)
		return { ...state, speculativelySettledCardIds: [], speculativelySettledTaskIds: [] };
	}
	return {
		...state,
		messages: cardsTouched ? messages : state.messages,
		activeTaskSummaries: summariesTouched ? activeTaskSummaries : state.activeTaskSummaries,
		speculativelySettledCardIds: settledCardIds,
		speculativelySettledTaskIds: settledTaskIds,
	};
}

function str(v: unknown): string {
	return typeof v === 'string' ? v : '';
}

/** Append a message and record its origin seq (for `transcript.rewound`). */
function appendMessage(state: V2SessionState, msg: ChatMessage, seq: number): V2SessionState {
	return {
		...state,
		messages: [...state.messages, msg],
		messageOriginSeq: { ...state.messageOriginSeq, [msg.id]: seq },
	};
}

/** Record an origin seq once — the earliest (creation) seq wins on replay. */
function withOrigin(map: Record<string, number>, id: string, seq: number): Record<string, number> {
	return map[id] !== undefined ? map : { ...map, [id]: seq };
}

/** Prefer an event payload `timestamp` over the envelope `created_at`. */
function stampTs(event: WireEvent, createdAt: string): string {
	return str(event.timestamp) || createdAt;
}

/**
 * Fold a subagent/team progress child into the accumulated children list,
 * mirroring the `subagent.progress` handler exactly. Returns the SAME array
 * reference (no-op) for an empty `chunk` so the caller can skip a re-render.
 */
function accumulateProgressChild(
	children: SubagentChildMessage[],
	event: WireEvent,
	kind: string,
	payload: Record<string, unknown>
): SubagentChildMessage[] {
	if (kind === 'tool_use') {
		return [
			...children,
			{
				id: event.id,
				type: 'tool_use',
				content: '',
				toolName: str(payload.name ?? payload.tool_name),
				toolId: str(payload.tool_use_id ?? payload.id) || undefined,
				toolInput: obj(payload.input),
				toolStatus: 'running',
			},
		];
	}
	if (kind === 'tool_result') {
		const resultText = str(payload.output ?? payload.content ?? event.note);
		const targetToolId = str(payload.tool_use_id ?? payload.id);
		const out = [...children];
		if (targetToolId) {
			for (let i = out.length - 1; i >= 0; i--) {
				if (out[i].type === 'tool_use' && out[i].toolId === targetToolId) {
					out[i] = {
						...out[i],
						toolResult: resultText,
						toolStatus: payload.is_error ? 'error' : 'complete',
					};
					return out;
				}
			}
		}
		out.push({
			id: event.id,
			type: 'tool_result',
			content: resultText,
			toolName: str(payload.name ?? payload.tool_name) || undefined,
		});
		return out;
	}
	// chunk (or unknown): accumulate streamed text into a trailing text child.
	const text = str(event.note ?? payload.text ?? payload.content);
	if (!text) return children; // no-op (same ref)
	const out = [...children];
	const last = out[out.length - 1];
	if (last && last.type === 'text' && last.streaming) {
		out[out.length - 1] = { ...last, content: last.content + text };
	} else {
		out.push({ id: event.id, type: 'text', content: text, streaming: true });
	}
	return out;
}

function teamMemberId(teamName: string, teammateName: string): string {
	return `team-${teamName}::${teammateName}`;
}

function findTeamMember(
	messages: ChatMessage[],
	teamName: string,
	teammateName: string
): ChatMessage | undefined {
	return messages.find(
		(m) => m.type === 'team_member' && m.teamName === teamName && m.teammateName === teammateName
	);
}

function obj(v: unknown): Record<string, unknown> | undefined {
	return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
}

// ─── Task-notification parsing ────────────────────────────────────────────────
// Background-task wake turns persist their synthesized prompt as a
// `message.user` event whose body is one or more `<task-notification>` XML
// blocks (app/core/turn_scheduler.py::_build_task_notification_prompt). The
// helpers below turn each block into a collapsible system card instead of a raw
// user bubble.

/** First `<tag>…</tag>` inner text (lazy), or '' when absent. The backend
 *  does not XML-escape field values (app/core/turn_scheduler.py::_esc is a
 *  plain str() passthrough), so the raw inner text is returned as-is. */
function extractTag(block: string, tag: string): string {
	const m = block.match(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`));
	return m ? m[1] : '';
}

/** The block's header slice — the text before the first `<summary>` or
 *  `<result>` (whichever index is lower; the whole block if neither present).
 *  The real `<kind>` element is emitted immediately after the opening
 *  `<task-notification>` tag, always before those fields, so it is always inside
 *  this slice. Restricting `<kind>` extraction to the header prevents an
 *  injection via UNESCAPED summary/result text. */
function kindHeaderSlice(block: string): string {
	const s = block.indexOf('<summary>');
	const r = block.indexOf('<result>');
	const idxs = [s, r].filter((i) => i >= 0);
	return idxs.length ? block.slice(0, Math.min(...idxs)) : block;
}

/** A usage number is kept as Number when it round-trips cleanly, otherwise the
 *  raw trimmed string; empty → undefined (so the field is omitted). */
function parseUsageNum(raw: string): number | string | undefined {
	const t = raw.trim();
	if (!t) return undefined;
	const n = Number(t);
	return Number.isFinite(n) && String(n) === t ? n : t;
}

/** Parse one `<task-notification>` block into `systemData` for a system card. */
function parseTaskNotification(block: string): Record<string, unknown> {
	const usage: Record<string, number | string> = {};
	const subagentTokens = parseUsageNum(extractTag(block, 'subagent_tokens'));
	const toolUses = parseUsageNum(extractTag(block, 'tool_uses'));
	const durationMs = parseUsageNum(extractTag(block, 'duration_ms'));
	if (subagentTokens !== undefined) usage.subagent_tokens = subagentTokens;
	if (toolUses !== undefined) usage.tool_uses = toolUses;
	if (durationMs !== undefined) usage.duration_ms = durationMs;
	return {
		task_id: extractTag(block, 'task-id'),
		tool_use_id: extractTag(block, 'tool-use-id'),
		status: extractTag(block, 'status'),
		summary: extractTag(block, 'summary'),
		result: extractTag(block, 'result'),
		output_file: extractTag(block, 'output-file'),
		// `own` (a task the parent itself launched → normal notification card) or
		// `pump` (an internal step of a still-running background agent → compact
		// chip + "ok" ack suppression). Absent tag = legacy own card. (R7/R5)
		// Extracted from the block HEADER only (text before the first `<summary>`
		// or `<result>`): the backend emits `<kind>` immediately after the opening
		// `<task-notification>` tag (turn_scheduler.py) and does NOT XML-escape
		// summary/result text, so an own notification whose body contains a literal
		// `<kind>pump</kind>` must not be misclassified as pump.
		kind: extractTag(kindHeaderSlice(block), 'kind') || 'own',
		usage,
	};
}

// ─── Quiet-pump "ok" ack classification (R5a) ─────────────────────────────────

/** True while a streaming assistant accumulation is still within the "ok" ack
 *  prefix set `{'', 'o', 'ok', 'ok.', 'ok!'}` (case-insensitive, trimmed) — the
 *  content is suppressed only as long as this holds. */
function isAckPrefix(content: string): boolean {
	const t = content.trim().toLowerCase();
	return t === '' || t === 'o' || t === 'ok' || t === 'ok.' || t === 'ok!';
}

/** True if `content` is a complete "ok" ack: trim + lowercase, strip ONE
 *  trailing `.` or `!`, equals `"ok"`. */
function isAckContent(content: string): boolean {
	let t = content.trim().toLowerCase();
	if (t.endsWith('.') || t.endsWith('!')) t = t.slice(0, -1);
	return t === 'ok';
}

// ─── Task state mapping ───────────────────────────────────────────────────────

const TASK_STATE_MAP: Record<string, Task['status']> = {
	running: 'running',
	active: 'running',
	pending: 'running',
	in_progress: 'running',
	completed: 'completed',
	done: 'completed',
	success: 'completed',
	failed: 'error',
	error: 'error',
	killed: 'killed',
	cancelled: 'killed',
};

function mapTaskStatus(state: string): Task['status'] {
	return TASK_STATE_MAP[state] ?? 'running';
}

// ─── Tool-result rejection classification (#612) ──────────────────────────────

/**
 * Map a raw model-API image-rejection error (surfaced verbatim on an
 * `is_error` `tool.result`, e.g. from a Read that attached an image) to a
 * short, friendly card message. CONSERVATIVE: returns a friendly string only
 * on a confident substring match, else `null` so the caller keeps today's raw
 * passthrough. A miss must degrade to the raw text, never a wrong substitution.
 */
function classifyToolRejection(raw: string): string | null {
	const s = raw.toLowerCase();
	// Image too large (byte size or pixel dimensions).
	if (s.includes('image exceeds') || s.includes('exceeds 5 mb') || s.includes('dimensions exceed')) {
		return 'Image too large for the model to read (limit ~5 MB / 8000 px). Try a smaller or downscaled version.';
	}
	// Unsupported / invalid image format. `invalid_request_error` only counts
	// when the message is clearly about an image (avoids catching unrelated
	// request errors).
	if (
		s.includes('unsupported image') ||
		s.includes('invalid image') ||
		(s.includes('invalid_request_error') && s.includes('image'))
	) {
		return "The model couldn't read this image (unsupported or invalid format).";
	}
	return null;
}

// ─── Reducer ──────────────────────────────────────────────────────────────────

export function reduce(state: V2SessionState, event: WireEvent): V2SessionState {
	const createdAt = str(event.created_at);
	// lastSeq only ever advances (replay is monotonic).
	const seq = typeof event.seq === 'number' ? event.seq : state.lastSeq;
	const withSeq = seq > state.lastSeq ? { ...state, lastSeq: seq } : state;
	state = withSeq;

	switch (event.type) {
		// ── Turn lifecycle ──────────────────────────────────────────────
		case 'turn.started':
			// Track the live turn id (#585) so a Stop can target THIS turn.
			// Also reset liveModel/liveRequestedModel: the new turn's model is
			// unknown until its own model_info note arrives; without this, a
			// previous turn's reroute banner lingers even after the user aligns
			// their selection. Replay order preserves correctness: a backfill
			// replays turn.started then that turn's model_info.
			return {
				...state,
				isStreaming: true,
				parked: false,
				liveTurnId: typeof event.turn_id === 'string' ? event.turn_id : state.liveTurnId,
				pendingPumpAck: null,
				pumpAckBuffer: '',
				liveModel: null,
				liveRequestedModel: null,
				liveProvider: null,
			};

		case 'turn.parked': {
			// A turn parked on background work: not "streaming" but still live.
			let next = closeOpen({ ...state, isStreaming: false, parked: true });
			// turn.parked lands AFTER turn.completed (backend emits turn.completed
			// unconditionally, then the park gate emits turn.parked if background
			// work remains — session_runtime._finalize). So any card / task summary
			// the preceding turn.completed speculatively settled to 'completed'
			// (settleStaleBackgroundWork) was settled WRONGLY: the park proves that
			// work is still running. Restore those to 'running' so the ActiveTasks
			// strip / subagent card stay visible. Ids that got a genuine
			// terminal event since the settle were already dropped from the record
			// and are left settled.
			const { speculativelySettledCardIds: cardIds, speculativelySettledTaskIds: taskIds } = next;
			if (cardIds.length || taskIds.length) {
				let messages = next.messages;
				if (cardIds.length) {
					const revert = new Set(cardIds);
					messages = messages.map((m) =>
						revert.has(m.id) && m.type === 'subagent' && m.agentStatus === 'completed'
							? { ...m, agentStatus: 'running' as const }
							: m
					);
				}
				let activeTaskSummaries = next.activeTaskSummaries;
				if (taskIds.length) {
					const revert = new Set(taskIds);
					activeTaskSummaries = activeTaskSummaries.map((t) =>
						revert.has(t.id) && t.status === 'completed'
							? { ...t, status: 'running' as const }
							: t
					);
				}
				next = {
					...next,
					messages,
					activeTaskSummaries,
					speculativelySettledCardIds: [],
					speculativelySettledTaskIds: [],
				};
			}
			return next;
		}

		case 'turn.completed':
			// turn.completed fires unconditionally when the pump drains — background
			// work may still remain, in which case the park gate emits turn.parked
			// right after. Speculatively settle any still-'running' subagent card /
			// task summary that missed its terminal event (#609 self-heal); a
			// following turn.parked reverts that settle. See settleStaleBackgroundWork.
			return settleStaleBackgroundWork(
				closeOpen({
					...state,
					isStreaming: false,
					parked: false,
					liveTurnId: null,
					pendingPumpAck: null,
					pumpAckBuffer: '',
					usage: obj(event.usage) ?? state.usage,
				})
			);

		case 'turn.aborted': {
			const reason = str(event.reason);
			let next = closeOpen(state);
			if (reason === 'interrupt') {
				// Mark any still-running tool card as errored (mirrors the legacy
				// interrupt cleanup in message-sender).
				next = {
					...next,
					messages: next.messages.map((m) =>
						m.type === 'tool_use' && m.toolStatus === 'running'
							? { ...m, streaming: false, toolStatus: 'error' as const }
							: m.streaming
								? { ...m, streaming: false }
								: m
					),
				};
				// Append the "[Stopped]" marker to the truncated assistant text
				// (mirrors the legacy end_streaming(interrupted=True) append).
				// Only mark output produced by THIS turn — scan back no further
				// than the last user message so a stop-before-first-output never
				// stamps a previous turn's response.
				const msgs = next.messages;
				let target = -1;
				for (let i = msgs.length - 1; i >= 0; i--) {
					const m = msgs[i];
					if (m.role === 'user') break;
					if (m.role === 'assistant' && m.type === 'text' && m.content.trim()) {
						target = i;
						break;
					}
				}
				if (target !== -1 && !msgs[target].content.endsWith('[Stopped]')) {
					const marked = [...msgs];
					marked[target] = {
						...marked[target],
						content: marked[target].content + '\n\n[Stopped]',
					};
					next = { ...next, messages: marked };
				}
			} else if (reason === 'error') {
				// Surface the abort's error message in the transcript (#606 item 2).
				// Spawn-time refusals (e.g. WorktreeUnavailableError) reach the log as
				// turn.aborted(reason='error', message=...) via turn_recorder's
				// extra_payload — without this, a viewer on a NON-runner device sees
				// streaming stop with no explanation. Rendered as the standard
				// error-styled system card (SystemMessage.svelte case 'error').
				//
				// Double-display note: on the RUNNER's own device the legacy WS
				// 'error' handler surfaces this same message as a transient BANNER
				// (activeSession.error), never a transcript message — so appending
				// here cannot duplicate a transcript entry. The guard below only
				// defends against the same/repeated event folding twice (replay).
				const message = str(event.message);
				if (message) {
					const last = next.messages[next.messages.length - 1];
					const duplicate =
						last?.role === 'system' &&
						last.systemSubtype === 'error' &&
						last.content === message;
					if (!duplicate) {
						next = appendMessage(next, {
							id: event.id,
							role: 'system',
							type: 'system',
							content: message,
							systemSubtype: 'error',
							timestamp: createdAt || undefined,
						}, seq);
					}
				}
			}
			return {
				...next,
				isStreaming: false,
				parked: false,
				liveTurnId: null,
				pendingPumpAck: null,
				pumpAckBuffer: '',
			};
		}

		case 'turn.usage':
			return { ...state, usage: obj(event.usage) ?? state.usage };

		// ── User message (+ optimistic reconcile) ───────────────────────
		case 'message.user': {
			const content = str(event.content);

			// Background-task wake turns persist their synthesized prompt as a
			// message.user event whose body is one or more <task-notification>
			// XML blocks (turn_scheduler::_build_task_notification_prompt).
			// Render each as a collapsible "task notification" system card rather
			// than a raw user chat bubble. If the marker substring is present but
			// NOTHING parses, fall through to the normal user-bubble path below.
			if (content.includes('<task-notification>')) {
				const blocks = content.match(/<task-notification>[\s\S]*?<\/task-notification>/g) || [];
				if (blocks.length > 0) {
					const notifStamp = stampTs(event, createdAt);
					let next = state;
					let anyPump = false;
					blocks.forEach((block, i) => {
						const data = parseTaskNotification(block);
						if (data.kind === 'pump') anyPump = true;
						const msg: ChatMessage = {
							id: blocks.length === 1 ? event.id : `${event.id}-tn-${i}`,
							role: 'system',
							type: 'system',
							content: '',
							systemSubtype: 'task_notification',
							systemData: data,
							timestamp: notifStamp || undefined,
						};
						next = appendMessage(next, msg, seq);
					});
					// A pump card arms "ok"-ack suppression for the live turn; a purely
					// own notification (or nothing pumped) clears any stale watch.
					return anyPump
						? { ...next, pendingPumpAck: next.liveTurnId, pumpAckBuffer: '' }
						: { ...next, pendingPumpAck: null, pumpAckBuffer: '' };
				}
				// substring present but no complete block parsed → safe fallback.
			}

			const commandId = typeof event.command_id === 'string' ? event.command_id : null;
			// 1. Reconcile against an optimistic message with a matching command_id.
			let idx = -1;
			if (commandId) {
				idx = state.messages.findIndex((m) => m.role === 'user' && m.commandId === commandId);
			}
			// 2. Fallback: same content, appended ≤30s before this event.
			if (idx === -1) {
				const eventMs = Date.parse(createdAt);
				idx = state.messages.findIndex((m) => {
					if (m.role !== 'user' || m.content !== content) return false;
					// Only reconcile a message that hasn't already been reconciled
					// (no server id yet — its id differs from this event's id).
					if (m.id === event.id) return true;
					const ms = m.timestamp ? Date.parse(m.timestamp) : NaN;
					if (Number.isNaN(ms) || Number.isNaN(eventMs)) return false;
					return Math.abs(eventMs - ms) <= 30_000;
				});
			}
			const stamp = stampTs(event, createdAt);
			const userMsg = (id: string): ChatMessage => ({
				id,
				role: 'user',
				type: 'text',
				content,
				timestamp: stamp || undefined,
				...(commandId ? { commandId } : {}),
			});
			if (idx >= 0) {
				// Replace in place — keep the optimistic id for stable Svelte keying.
				// The reconciled message now has an origin seq (it is no longer a
				// pending optimistic message), so it participates in rewinds.
				const keepId = state.messages[idx].id;
				const messages = [...state.messages];
				messages[idx] = userMsg(keepId);
				return {
					...state,
					messages,
					messageOriginSeq: withOrigin(state.messageOriginSeq, keepId, seq),
					pendingPumpAck: null,
					pumpAckBuffer: '',
				};
			}
			return { ...appendMessage(state, userMsg(event.id), seq), pendingPumpAck: null, pumpAckBuffer: '' };
		}

		// ── Assistant text / thinking deltas ────────────────────────────
		case 'assistant.text_delta': {
			const text = str(event.text);
			let next = closeOpenThinking(state);
			// Quiet-pump suppression: while watching the live pump turn's reply,
			// buffer assistant text instead of rendering it. As long as the
			// accumulation is still an ack-prefix it stays hidden (no message
			// created — avoids any visible flash / renderer leak). The instant it
			// exceeds the prefix (the model is narrating/acting) reveal the whole
			// buffer as a normal streaming message and stop watching. (R5a/R5b)
			if (next.pendingPumpAck && next.pendingPumpAck === next.liveTurnId && !next.openTextId) {
				const buffer = next.pumpAckBuffer + text;
				if (isAckPrefix(buffer)) {
					return { ...next, pumpAckBuffer: buffer };
				}
				const revealId = `text-${event.id}`;
				const revealMsg: ChatMessage = {
					id: revealId,
					role: 'assistant',
					type: 'text',
					content: buffer,
					streaming: true,
					timestamp: createdAt || undefined,
				};
				return {
					...appendMessage({ ...next, pendingPumpAck: null, pumpAckBuffer: '' }, revealMsg, seq),
					openTextId: revealId,
				};
			}
			if (next.openTextId) {
				const open = next.messages.find((m) => m.id === next.openTextId);
				return {
					...next,
					messages: replaceById(next.messages, next.openTextId, {
						content: (open?.content ?? '') + text,
					}),
				};
			}
			const id = `text-${event.id}`;
			const msg: ChatMessage = {
				id,
				role: 'assistant',
				type: 'text',
				content: text,
				streaming: true,
				timestamp: createdAt || undefined,
			};
			return { ...appendMessage(next, msg, seq), openTextId: id };
		}

		// ── Closed assistant message (never merges/opens a stream) ───────
		case 'message.assistant': {
			const content = str(event.content);
			const kind = str(event.kind) === 'thinking' ? 'thinking' : 'text';
			// Quiet-pump suppression, closed / no-delta path (R8 replay parity):
			// a closed assistant text arriving while we watch the live pump turn is
			// dropped entirely when it is either an exact "ok" ack OR empty /
			// whitespace-only. The empty case covers the new silent no-reply
			// instruction — the model ends the turn with no text — so a whitespace-
			// only closed bubble never leaks into the transcript.
			if (
				kind === 'text' &&
				state.pendingPumpAck &&
				state.pendingPumpAck === state.liveTurnId &&
				(isAckContent(content) || content.trim() === '')
			) {
				return { ...closeOpen(state), pendingPumpAck: null, pumpAckBuffer: '' };
			}
			// Always a CLOSED bubble: close any open stream first, then append.
			// It must never merge into or reopen a streaming text/thinking msg.
			const next = closeOpen(state);
			const metadata: Record<string, unknown> = {};
			if (str(event.model)) metadata.model = str(event.model);
			const msg: ChatMessage = {
				id: event.id,
				role: 'assistant',
				type: kind,
				content,
				streaming: false,
				timestamp: stampTs(event, createdAt) || undefined,
				...(str(event.uuid) ? { jsonlUuid: str(event.uuid) } : {}),
				...(str(event.profile_id) ? { profileId: str(event.profile_id) } : {}),
				...(Object.keys(metadata).length ? { metadata } : {}),
			};
			// A rendered (non-ack) closed text means the model narrated → stop
			// watching. Thinking closes never clear the watch (mirror deltas).
			const settled = appendMessage(next, msg, seq);
			return kind === 'text'
				? { ...settled, pendingPumpAck: null, pumpAckBuffer: '' }
				: settled;
		}

		case 'assistant.thinking_delta': {
			const text = str(event.text);
			let next = closeOpenText(state);
			if (next.openThinkingId) {
				const open = next.messages.find((m) => m.id === next.openThinkingId);
				return {
					...next,
					messages: replaceById(next.messages, next.openThinkingId, {
						content: (open?.content ?? '') + text,
					}),
				};
			}
			const id = `thinking-${event.id}`;
			const msg: ChatMessage = {
				id,
				role: 'assistant',
				type: 'thinking',
				content: text,
				streaming: true,
				timestamp: createdAt || undefined,
			};
			return { ...appendMessage(next, msg, seq), openThinkingId: id };
		}

		// ── Tool cards ──────────────────────────────────────────────────
		case 'tool.use': {
			const toolUseId = str(event.tool_use_id);
			if (!toolUseId) return state;
			// A tool use means the model is acting → stop watching for an "ok" ack.
			let next = { ...closeOpen(state), pendingPumpAck: null, pumpAckBuffer: '' };
			const input = obj(event.input);
			const screenshot = str(event.screenshot);
			const existing = findByToolId(next.messages, toolUseId);
			if (existing) {
				// Card already created by an earlier input_delta — fill in name/input.
				return {
					...next,
					messages: replaceById(next.messages, existing.id, {
						toolName: str(event.name),
						...(input ? { toolInput: input } : {}),
						...(screenshot ? { toolScreenshot: screenshot } : {}),
					}),
				};
			}
			const msg: ChatMessage = {
				id: `tooluse-${toolUseId}`,
				role: 'assistant',
				type: 'tool_use',
				content: '',
				toolName: str(event.name),
				toolId: toolUseId,
				toolStatus: 'running',
				streaming: true,
				timestamp: stampTs(event, createdAt) || undefined,
				...(input ? { toolInput: input } : {}),
				...(screenshot ? { toolScreenshot: screenshot } : {}),
			};
			return appendMessage(next, msg, seq);
		}

		case 'tool.input_delta': {
			const toolUseId = str(event.tool_use_id);
			const partial = str(event.partial_json);
			if (!toolUseId) return state;
			const existing = findByToolId(state.messages, toolUseId);
			if (existing) {
				return {
					...state,
					messages: replaceById(state.messages, existing.id, {
						partialToolInput: (existing.partialToolInput ?? '') + partial,
					}),
				};
			}
			// A background subagent launch emits input_delta for the launch
			// tool_use block but never tool.use/tool.result. Once the subagent
			// card exists for that id (launch tool_use_id == task_id), the card
			// is the rendering surface — do not recreate the orphan placeholder.
			if (findSubagentByTask(state.messages, toolUseId)) return state;
			// input_delta before the tool.use envelope: open a placeholder card.
			const next = closeOpen(state);
			const msg: ChatMessage = {
				id: `tooluse-${toolUseId}`,
				role: 'assistant',
				type: 'tool_use',
				content: '',
				toolName: '',
				toolId: toolUseId,
				toolStatus: 'running',
				streaming: true,
				partialToolInput: partial,
				timestamp: createdAt || undefined,
			};
			return appendMessage(next, msg, seq);
		}

		case 'tool.result': {
			const toolUseId = str(event.tool_use_id);
			if (!toolUseId) return state;
			const existing = findByToolId(state.messages, toolUseId);
			if (!existing) return state; // standalone tool_result is not rendered
			const rawResult = str(event.content_preview);
			// On an API rejection (#612), swap the raw provider error text for a
			// friendly message when we confidently recognize it; otherwise keep the
			// raw preview (conservative — a miss degrades to today's passthrough).
			const toolResult = event.is_error
				? classifyToolRejection(rawResult) ?? rawResult
				: rawResult;
			return {
				...state,
				messages: replaceById(state.messages, existing.id, {
					toolResult,
					toolStatus: event.is_error ? 'error' : 'complete',
					streaming: false,
				}),
			};
		}

		// ── Subagent card (correlated by task_id) ───────────────────────
		case 'subagent.started': {
			const taskId = str(event.task_id);
			if (!taskId) return state;
			let next = closeOpen(state);
			if (findSubagentByTask(next.messages, taskId)) return next; // idempotent
			// The launch tool_use block emitted only tool.input_delta (never
			// tool.use/tool.result), opening a `tooluse-${taskId}` placeholder
			// that would spin forever. The subagent card is the designed surface
			// for the launch — drop the orphan placeholder now the card exists.
			next = {
				...next,
				messages: next.messages.filter((m) => !(m.type === 'tool_use' && m.toolId === taskId)),
			};
			const msg: ChatMessage = {
				id: `subagent-${taskId}`,
				role: 'assistant',
				type: 'subagent',
				content: '',
				toolId: taskId,
				agentId: typeof event.agent_id === 'string' ? event.agent_id : undefined,
				agentType: typeof event.agent_type === 'string' ? event.agent_type : undefined,
				agentDescription: typeof event.description === 'string' ? event.description : undefined,
				agentPrompt: typeof event.prompt_preview === 'string' ? event.prompt_preview : undefined,
				agentStatus: 'running',
				agentChildren: [],
				timestamp: createdAt || undefined,
			};
			return appendMessage(next, msg, seq);
		}

		case 'subagent.progress': {
			const taskId = str(event.task_id);
			const card = findSubagentByTask(state.messages, taskId);
			if (!card) return state;
			const kind = str(event.kind);
			const children = [...(card.agentChildren ?? [])];
			const payload = obj(event.payload) ?? {};
			if (kind === 'tool_use') {
				children.push({
					id: event.id,
					type: 'tool_use',
					content: '',
					toolName: str(payload.name ?? payload.tool_name),
					toolId: str(payload.tool_use_id ?? payload.id) || undefined,
					toolInput: obj(payload.input),
					toolStatus: 'running',
				});
			} else if (kind === 'tool_result') {
				const resultText = str(payload.output ?? payload.content ?? event.note);
				const targetToolId = str(payload.tool_use_id ?? payload.id);
				// Merge into the matching tool_use child when identifiable.
				let merged = false;
				if (targetToolId) {
					for (let i = children.length - 1; i >= 0; i--) {
						if (children[i].type === 'tool_use' && children[i].toolId === targetToolId) {
							children[i] = {
								...children[i],
								toolResult: resultText,
								toolStatus: payload.is_error ? 'error' : 'complete',
							};
							merged = true;
							break;
						}
					}
				}
				if (!merged) {
					children.push({
						id: event.id,
						type: 'tool_result',
						content: resultText,
						toolName: str(payload.name ?? payload.tool_name) || undefined,
					});
				}
			} else {
				// chunk (or unknown): accumulate streamed text into a trailing text child.
				const text = str(event.note ?? payload.text ?? payload.content);
				if (!text) return state;
				const last = children[children.length - 1];
				if (last && last.type === 'text' && last.streaming) {
					children[children.length - 1] = { ...last, content: last.content + text };
				} else {
					children.push({ id: event.id, type: 'text', content: text, streaming: true });
				}
			}
			// Re-open belt: progress only ever flows while the agent genuinely
			// works, so any progress after a premature settle (from a terminal
			// chip that landed early) must re-open the card. Only 'completed'
			// re-opens — never 'error'/'killed' (those are authoritative).
			const reopen = card.agentStatus === 'completed' ? { agentStatus: 'running' as const } : {};
			return {
				...state,
				messages: replaceById(state.messages, card.id, { agentChildren: children, ...reopen }),
			};
		}

		case 'subagent.completed': {
			const taskId = str(event.task_id);
			const card = findSubagentByTask(state.messages, taskId);
			if (!card) return state;
			const status = str(event.status);
			const agentStatus: ChatMessage['agentStatus'] = status === 'completed' ? 'completed' : 'error';
			// Finalize any streaming children.
			const children = (card.agentChildren ?? []).map((c) =>
				c.streaming ? { ...c, streaming: false } : c
			);
			// Genuine terminal for this card: drop it from the speculative-settle
			// record so a following turn.parked cannot revive it to 'running'.
			const speculativelySettledCardIds = state.speculativelySettledCardIds.includes(card.id)
				? state.speculativelySettledCardIds.filter((c) => c !== card.id)
				: state.speculativelySettledCardIds;
			return {
				...state,
				speculativelySettledCardIds,
				messages: replaceById(state.messages, card.id, {
					agentStatus,
					agentChildren: children,
					content: str(event.result_preview),
				}),
			};
		}

		// ── Team-member card (correlated by team_name + teammate_name) ──
		case 'team.member_started': {
			const teamName = str(event.team_name);
			const teammateName = str(event.teammate_name);
			if (!teamName || !teammateName) return state;
			const next = closeOpen(state);
			if (findTeamMember(next.messages, teamName, teammateName)) return next; // idempotent
			const msg: ChatMessage = {
				id: teamMemberId(teamName, teammateName),
				role: 'assistant',
				type: 'team_member',
				content: '',
				teamName,
				teammateName,
				teammateStatus: 'running',
				agentChildren: [],
				...(str(event.description) ? { agentDescription: str(event.description) } : {}),
				...(str(event.prompt) ? { agentPrompt: str(event.prompt) } : {}),
				timestamp: stampTs(event, createdAt) || undefined,
			};
			return appendMessage(next, msg, seq);
		}

		case 'team.member_progress': {
			const teamName = str(event.team_name);
			const teammateName = str(event.teammate_name);
			const card = findTeamMember(state.messages, teamName, teammateName);
			if (!card) return state;
			const before = card.agentChildren ?? [];
			const children = accumulateProgressChild(before, event, str(event.kind), obj(event.payload) ?? {});
			if (children === before) return state; // empty chunk: no-op
			return { ...state, messages: replaceById(state.messages, card.id, { agentChildren: children }) };
		}

		case 'team.member_completed': {
			const teamName = str(event.team_name);
			const teammateName = str(event.teammate_name);
			const card = findTeamMember(state.messages, teamName, teammateName);
			if (!card) return state;
			const status = str(event.status);
			const teammateStatus: TeammateStatus =
				status === 'completed' ? 'completed' : status === 'killed' ? 'killed' : 'error';
			const children = (card.agentChildren ?? []).map((c) => (c.streaming ? { ...c, streaming: false } : c));
			const patch: Partial<ChatMessage> = { teammateStatus, agentChildren: children };
			const resultPreview = str(event.result_preview);
			if (resultPreview) patch.content = resultPreview;
			const usage = obj(event.usage);
			if (usage) patch.agentUsage = usage as ChatMessage['agentUsage'];
			return { ...state, messages: replaceById(state.messages, card.id, patch) };
		}

		// ── Background task summaries (parked visibility / tasks panel) ──
		case 'task.updated': {
			const taskId = str(event.task_id);
			if (!taskId) return state;
			const status = mapTaskStatus(str(event.state));
			const existing = state.activeTaskSummaries.find((t) => t.id === taskId);
			const summary: Task = {
				id: taskId,
				subject: typeof event.description === 'string' ? event.description : existing?.subject ?? '',
				status,
				// The contract carries no kind for task.updated; default 'agent'
				// (these are Task-tool / background-work updates). See shape note.
				kind: existing?.kind ?? 'agent',
				summary: typeof event.summary === 'string' ? event.summary : existing?.summary ?? null,
				toolUseId: typeof event.tool_use_id === 'string' ? event.tool_use_id : existing?.toolUseId ?? null,
				startedAt: existing?.startedAt || createdAt,
				updatedAt: createdAt || existing?.updatedAt || '',
			};
			const activeTaskSummaries = existing
				? state.activeTaskSummaries.map((t) => (t.id === taskId ? summary : t))
				: [...state.activeTaskSummaries, summary];
			const taskOriginSeq = withOrigin(state.taskOriginSeq, taskId, seq);
			let messages = state.messages;
			let speculativelySettledCardIds = state.speculativelySettledCardIds;
			let speculativelySettledTaskIds = state.speculativelySettledTaskIds;
			// A background subagent finishing BETWEEN turns reports only via this
			// chip (the drain pump dispatches no subagent.completed), so a
			// terminal update must also settle the joined subagent card — else a
			// replay leaves it spinning forever. Join through tool_use_id (the
			// card key); never downgrade a card subagent.completed already
			// settled with its richer result.
			if (status === 'completed' || status === 'error' || status === 'killed') {
				const card = summary.toolUseId
					? findSubagentByTask(messages, summary.toolUseId)
					: undefined;
				if (card && card.agentStatus === 'running') {
					const children = (card.agentChildren ?? []).map((c) =>
						c.streaming ? { ...c, streaming: false } : c
					);
					messages = replaceById(messages, card.id, {
						agentStatus: status === 'completed' ? 'completed' : 'error',
						agentChildren: children,
					});
				}
				// This is a GENUINE terminal event: drop this summary (and any joined
				// card) from the speculative-settle record so a following turn.parked
				// does NOT revive it to 'running' (terminal event wins).
				if (speculativelySettledTaskIds.includes(taskId)) {
					speculativelySettledTaskIds = speculativelySettledTaskIds.filter((t) => t !== taskId);
				}
				if (card && speculativelySettledCardIds.includes(card.id)) {
					speculativelySettledCardIds = speculativelySettledCardIds.filter((c) => c !== card.id);
				}
			}
			return {
				...state,
				messages,
				activeTaskSummaries,
				taskOriginSeq,
				speculativelySettledCardIds,
				speculativelySettledTaskIds,
			};
		}

		// ── System messages ─────────────────────────────────────────────
		case 'system.note': {
			// `background_tasks_changed` is a CLI/SDK bookkeeping event that
			// exists only to drive the active-tasks strip (which is fed from
			// `task.updated` -> activeTaskSummaries, NOT from this note). It
			// carries no conversation content, so mapping it to a transcript
			// message renders its raw JSON payload as a "Background Tasks
			// Changed" card (issue #638). Filter it here -- the earliest
			// mapping point -- so no message is ever created. activeTasks
			// state is untouched (this event never feeds it).
			if (str(event.subtype) === 'background_tasks_changed') return state;
			// `model_info` reports the model the SDK's MAIN lane is actually
			// running (vs. what was requested) at turn start and whenever it
			// changes mid-turn (rerouting). It drives the composer's reroute
			// indicator only — never a transcript card — so fold it into
			// `liveModel` and return without appending, exactly like
			// `background_tasks_changed` above. A from_seq=0 replay re-applies
			// these notes, so `liveModel` self-restores on reload.
			if (str(event.subtype) === 'model_info') {
				const model = str(obj(event.data)?.model);
				const requested = str(obj(event.data)?.requested_model);
				const provider = str(obj(event.data)?.provider);
				return {
					...state,
					liveModel: model || null,
					liveRequestedModel: requested || null,
					liveProvider: provider || null,
				};
			}
			const data = obj(event.data);
			const msg: ChatMessage = {
				id: event.id,
				role: 'system',
				type: 'system',
				content: str(data?.content ?? data?.text),
				systemSubtype: str(event.subtype),
				...(data ? { systemData: data } : {}),
				timestamp: stampTs(event, createdAt) || undefined,
			};
			return appendMessage(closeOpen(state), msg, seq);
		}

		case 'system.compaction_boundary': {
			const msg: ChatMessage = {
				id: event.id,
				role: 'system',
				type: 'system',
				content: '',
				systemSubtype: 'compaction_boundary',
				timestamp: createdAt || undefined,
			};
			return appendMessage(closeOpen(state), msg, seq);
		}

		// ── Transcript rewind (replay-deterministic truncation) ─────────
		case 'transcript.rewound': {
			const fromSeq = typeof event.from_seq === 'number' ? event.from_seq : NaN;
			if (Number.isNaN(fromSeq)) return state; // malformed: no-op
			// Drop every message/task CREATED at seq >= fromSeq. Optimistic
			// messages (absent from messageOriginSeq) are exempt. Reducing the
			// full log from seq 0 reproduces the same truncation → determinism.
			const keptMessages = state.messages.filter((m) => {
				const origin = state.messageOriginSeq[m.id];
				return origin === undefined || origin < fromSeq;
			});
			const messageOriginSeq: Record<string, number> = {};
			for (const [id, origin] of Object.entries(state.messageOriginSeq)) {
				if (origin < fromSeq) messageOriginSeq[id] = origin;
			}
			const taskOriginSeq: Record<string, number> = {};
			const activeTaskSummaries = state.activeTaskSummaries.filter((t) => {
				const origin = state.taskOriginSeq[t.id];
				if (origin === undefined || origin < fromSeq) {
					if (origin !== undefined) taskOriginSeq[t.id] = origin;
					return true;
				}
				return false;
			});
			// Clear open pointers if their target message was truncated away.
			const liveIds = new Set(keptMessages.map((m) => m.id));
			const openTextId = state.openTextId && liveIds.has(state.openTextId) ? state.openTextId : null;
			const openThinkingId =
				state.openThinkingId && liveIds.has(state.openThinkingId) ? state.openThinkingId : null;
			return {
				...state,
				messages: keptMessages,
				messageOriginSeq,
				taskOriginSeq,
				activeTaskSummaries,
				openTextId,
				openThinkingId,
			};
		}

		// ── Phase-1 non-goals (stay on legacy WS) / envelope-only ───────
		// Queue UI, interrupt affordance, permissions/questions and artifacts
		// are handled by the legacy WS path or deferred; they do not mutate the
		// rendered transcript here.
		case 'turn.queued':
		case 'turn.interrupting':
		case 'interaction.requested':
		case 'interaction.answered':
		case 'interaction.expired':
		case 'artifact.created':
			return state;

		// ── Unknown types: ignore (forward compatibility) ───────────────
		default:
			return state;
	}
}

/** Convenience: fold an ordered event log from scratch (used by replay/tests). */
export function reduceAll(events: WireEvent[], from: V2SessionState = initialState()): V2SessionState {
	return events.reduce(reduce, from);
}

// Re-export for callers that seed optimistic messages.
export type { ChatMessage, SubagentChildMessage };

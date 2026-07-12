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
 *   - The message shapes produced here MUST match what `convertServerMessage`
 *     (session-poller.ts:1139) produces so `MessageArea.svelte` renders them
 *     unchanged. See the per-event handlers below for the exact field mapping.
 *   - Unknown event types are ignored (forward compatibility).
 */

import type { ChatMessage, SubagentChildMessage, Task } from '../../types';

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

function str(v: unknown): string {
	return typeof v === 'string' ? v : '';
}

function obj(v: unknown): Record<string, unknown> | undefined {
	return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
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
			return { ...state, isStreaming: true, parked: false };

		case 'turn.parked':
			// A turn parked on background work: not "streaming" but still live.
			return closeOpen({ ...state, isStreaming: false, parked: true });

		case 'turn.completed':
			return closeOpen({
				...state,
				isStreaming: false,
				parked: false,
				usage: obj(event.usage) ?? state.usage,
			});

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
			}
			return { ...next, isStreaming: false, parked: false };
		}

		case 'turn.usage':
			return { ...state, usage: obj(event.usage) ?? state.usage };

		// ── User message (+ optimistic reconcile) ───────────────────────
		case 'message.user': {
			const content = str(event.content);
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
			const userMsg = (id: string): ChatMessage => ({
				id,
				role: 'user',
				type: 'text',
				content,
				timestamp: createdAt || undefined,
				...(commandId ? { commandId } : {}),
			});
			if (idx >= 0) {
				// Replace in place — keep the optimistic id for stable Svelte keying.
				const keepId = state.messages[idx].id;
				const messages = [...state.messages];
				messages[idx] = userMsg(keepId);
				return { ...state, messages };
			}
			return { ...state, messages: [...state.messages, userMsg(event.id)] };
		}

		// ── Assistant text / thinking deltas ────────────────────────────
		case 'assistant.text_delta': {
			const text = str(event.text);
			let next = closeOpenThinking(state);
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
			return { ...next, messages: [...next.messages, msg], openTextId: id };
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
			return { ...next, messages: [...next.messages, msg], openThinkingId: id };
		}

		// ── Tool cards ──────────────────────────────────────────────────
		case 'tool.use': {
			const toolUseId = str(event.tool_use_id);
			if (!toolUseId) return state;
			let next = closeOpen(state);
			const input = obj(event.input);
			const existing = findByToolId(next.messages, toolUseId);
			if (existing) {
				// Card already created by an earlier input_delta — fill in name/input.
				return {
					...next,
					messages: replaceById(next.messages, existing.id, {
						toolName: str(event.name),
						...(input ? { toolInput: input } : {}),
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
				timestamp: createdAt || undefined,
				...(input ? { toolInput: input } : {}),
			};
			return { ...next, messages: [...next.messages, msg] };
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
			return { ...next, messages: [...next.messages, msg] };
		}

		case 'tool.result': {
			const toolUseId = str(event.tool_use_id);
			if (!toolUseId) return state;
			const existing = findByToolId(state.messages, toolUseId);
			if (!existing) return state; // standalone tool_result is not rendered
			return {
				...state,
				messages: replaceById(state.messages, existing.id, {
					toolResult: str(event.content_preview),
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
			return { ...next, messages: [...next.messages, msg] };
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
			return { ...state, messages: replaceById(state.messages, card.id, { agentChildren: children }) };
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
			return {
				...state,
				messages: replaceById(state.messages, card.id, {
					agentStatus,
					agentChildren: children,
					content: str(event.result_preview),
				}),
			};
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
			let messages = state.messages;
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
			}
			return { ...state, messages, activeTaskSummaries };
		}

		// ── System messages ─────────────────────────────────────────────
		case 'system.note': {
			const data = obj(event.data);
			const msg: ChatMessage = {
				id: event.id,
				role: 'system',
				type: 'system',
				content: str(data?.content ?? data?.text),
				systemSubtype: str(event.subtype),
				...(data ? { systemData: data } : {}),
				timestamp: createdAt || undefined,
			};
			return closeOpen({ ...state, messages: [...state.messages, msg] });
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
			return closeOpen({ ...state, messages: [...state.messages, msg] });
		}

		// ── Phase-1 non-goals (stay on legacy WS) / envelope-only ───────
		// Queue UI, interrupt affordance, permissions/questions, rewind and
		// artifacts are handled by the legacy WS path or deferred; they do not
		// mutate the rendered transcript here.
		case 'turn.queued':
		case 'turn.interrupting':
		case 'interaction.requested':
		case 'interaction.answered':
		case 'interaction.expired':
		case 'transcript.rewound':
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

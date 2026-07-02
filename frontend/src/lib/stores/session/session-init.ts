/**
 * Session Init — Initialization, destruction, and poll/stream-end callbacks.
 *
 * Handles:
 * - init() / destroy() lifecycle
 * - Session-restore guards (sessionStorage-based)
 * - setupPollCallback() — poller → activeSession state merging
 * - setupOnStreamEnd() — stream-end cleanup
 *
 * Plain .ts file — no $state/$derived/$effect runes (see KNOWN_ISSUES.md Root Cause 10).
 */

import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import type { ChatMessage, Task } from '../types';
import type { SessionUsage } from './session-state';
import type { PollAppliedData } from './session-poller';
import { STREAM_DEBUG } from '$lib/utils/debug-log';
import { notify } from '../notifications';

// ─── Interfaces ──────────────────────────────────────────────────────────────

export interface SessionInitDeps {
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	getState: () => ActiveSessionState;
	notifySubscribers: () => void;
	defaultState: () => ActiveSessionState;
	setState: (s: ActiveSessionState) => void;
	internal: InternalMutableState;

	// Session infrastructure
	sessionPoller: {
		onPollApplied: (cb: (sessionId: string, data: PollAppliedData) => void) => void;
		onStreamEnd: (cb: (sessionId: string, usage: SessionUsage) => void) => void;
		onPollError: (cb: (sessionId: string, consecutiveErrors: number, error: unknown) => void) => void;
		setInterruptGuard?: (cb: (sessionId: string) => boolean) => void;
		loadSession: (sessionId: string) => Promise<PollAppliedData | null>;
		startStreaming: (sessionId: string) => void;
		stop: (sessionId: string) => void;
		stopAll: () => void;
		isPolling: (sessionId: string) => boolean;
	};

	// External modules
	wsConn: {
		connect: () => void;
		disconnect: () => void;
		destroy: () => void;
	};
	switcher: {
		loadSelectionFromBackend: () => Promise<{ selectedProfile?: string; selectedProject?: string } | null>;
		loadPersistedSession: () => Promise<import('../active-session').PersistedSession | null>;
		getPersistedProfile: () => string;
		getPersistedProject: () => string;
		destroy: () => void;
	};
	appData: {
		loadSessions: () => void;
	};
	disconnect: () => void;
	persistSession: () => void;
	persistTimer: {
		get: () => ReturnType<typeof setTimeout> | null;
		clear: () => void;
	};
}

// ─── Session-restore guards ─────────────────────────────────────────────────
// IMPORTANT: These use sessionStorage, NOT module-level variables.
// Module-level `let` variables in .svelte.ts files reset when the component
// remounts (Svelte re-evaluates the module), which defeated the previous
// design.  sessionStorage survives module re-evaluation and component
// destroy/init cycles within the same browser tab.

const SKIP_RESTORE_KEY = '__aihub_skipSessionRestore';
const INIT_GEN_KEY = '__aihub_initGeneration';

export function getSkipRestore(): boolean {
	if (typeof window === 'undefined') return false;
	return sessionStorage.getItem(SKIP_RESTORE_KEY) === '1';
}

export function setSkipRestore(value: boolean) {
	if (typeof window === 'undefined') return;
	if (value) {
		sessionStorage.setItem(SKIP_RESTORE_KEY, '1');
	} else {
		sessionStorage.removeItem(SKIP_RESTORE_KEY);
	}
}

export function getInitGeneration(): number {
	if (typeof window === 'undefined') return 0;
	return parseInt(sessionStorage.getItem(INIT_GEN_KEY) || '0', 10);
}

export function bumpInitGeneration(): number {
	const next = getInitGeneration() + 1;
	if (typeof window !== 'undefined') {
		sessionStorage.setItem(INIT_GEN_KEY, String(next));
	}
	return next;
}

// ─── Helper ──────────────────────────────────────────────────────────────────

/** Build a partial store update from SessionUsage.
 *
 * Always returns token fields so that contextUsed is set even when the
 * server reports 0 (e.g. session with no JSONL data). Without this,
 * switchSession's contextUsed:null reset is never overwritten and the
 * component shows stale/wrong values on the next render.
 */
export function usageStoreUpdate(usage: SessionUsage): Partial<ActiveSessionState> {
	return {
		totalTokensIn: usage.inputTokens,
		totalTokensOut: usage.outputTokens,
		totalCacheCreationTokens: usage.cacheCreationTokens,
		totalCacheReadTokens: usage.cacheReadTokens,
		contextUsed: usage.contextUsed,
	};
}

/**
 * Phase 2 of the poll merge: reconcile the live WS partial-text buffer
 * (`wsStreamContent`) with the poller's authoritative message list.
 *
 * Extracted as a pure, exported function so the staleness/clear logic can be
 * unit-tested against the REAL merge code (the "dead air after tools" bug).
 *
 * Three cases while the turn is streaming:
 *  - poller already has a streaming text AND the WS buffer is ahead of it →
 *    render the longer WS buffer into that message (the WS-ahead head-start).
 *  - poller has NO streaming text (a post-tool think-gap, or turn start before
 *    the first text block) → re-append the turn-start synthetic so the bubble
 *    survives the poll. Its content comes ONLY from genuinely-live WS-ahead
 *    text; otherwise it is an EMPTY phantom that gap-indicator.ts treats as
 *    transparent so the bottom gap dots can show.
 *  - poller HAS a streaming text but the WS buffer is empty/behind → nothing to
 *    merge; drop the (now-consumed) buffer.
 *
 * THE BUG this guards against: `wsStreamContent` is cleared by the WS layer
 * ONLY at turn boundaries (handleDone / stopped / the `start` event), never
 * when a text block finalizes mid-turn into a tool call. So once any text
 * streams in a turn, the buffer stays non-empty for the rest of the turn. In a
 * post-tool think-gap the poller has no streaming text, so the old code
 * re-appended the synthetic with the stale buffer as its content — a NON-EMPTY
 * streaming assistant-text at the tail. That is not an empty phantom, so it
 * suppressed the gap dots (and rendered nothing the user could perceive) →
 * total dead air for many seconds. The fix drops the buffer the moment a
 * finalized assistant text already contains it.
 *
 * @returns the (possibly new) message array + the (possibly cleared) buffer.
 */
export function mergeWsStreamBuffer(
	finalMessages: ChatMessage[],
	wsStreamContent: string,
	priorMessages: ChatMessage[],
	isStreaming: boolean
): { messages: ChatMessage[]; wsStreamContent: string } {
	let finalWsContent = wsStreamContent;

	if (!isStreaming) {
		return { messages: finalMessages, wsStreamContent: '' };
	}

	// wsStreamContent is cleared only at turn boundaries by the WS layer, never
	// when a text block finalizes mid-turn into a tool call. If the MOST RECENT
	// finalized (non-streaming) assistant text already contains the buffer, the
	// buffer is the leftover of that just-ended block — stale — so drop it before
	// it can leak into a think-gap synthetic and masquerade as live streaming
	// (the "dead air after tools" bug).
	//
	// Match ONLY the most recent finalized text, not any historical one. The
	// buffer always tracks the latest block (each finalize drops it here, the next
	// block refills it from empty), so a genuine stale buffer always equals the
	// last finalized text and is still caught — but a NEW block's live partial
	// ("Let me", "Here's") that merely prefixes an OLDER message is no longer
	// dropped as false-stale, which would needlessly defeat the WS-ahead
	// head-start.
	if (finalWsContent) {
		let lastFinalizedText: string | null = null;
		for (let i = finalMessages.length - 1; i >= 0; i--) {
			const m = finalMessages[i];
			if (m.role === 'assistant' && m.type === 'text' && !m.streaming) {
				lastFinalizedText = m.content || '';
				break;
			}
		}
		if (lastFinalizedText !== null && lastFinalizedText.startsWith(finalWsContent)) {
			finalWsContent = '';
		}
	}

	let pollerStreamIdx = -1;
	for (let i = finalMessages.length - 1; i >= 0; i--) {
		if (
			finalMessages[i].role === 'assistant' &&
			finalMessages[i].type === 'text' &&
			finalMessages[i].streaming
		) {
			pollerStreamIdx = i;
			break;
		}
	}

	if (pollerStreamIdx >= 0 && finalWsContent) {
		// WS-ahead head-start: render the longer live buffer into the poller's
		// still-streaming text.
		const pollerContent = finalMessages[pollerStreamIdx].content || '';
		if (finalWsContent.length > pollerContent.length) {
			finalMessages = [...finalMessages];
			finalMessages[pollerStreamIdx] = { ...finalMessages[pollerStreamIdx], content: finalWsContent };
		}
	} else if (pollerStreamIdx < 0) {
		// No streaming text in the poller data (post-tool think-gap or turn
		// start). Re-append the turn-start synthetic so the bubble survives the
		// poll. Source its content ONLY from finalWsContent — which now is
		// either genuinely-live WS-ahead text or empty (stale was dropped
		// above). Never fall back to the prior synthetic's content: that would
		// perpetuate stale content across merges and recreate the dead-air bug.
		const currentSynthetic = priorMessages.find(
			(m: ChatMessage) => m.role === 'assistant' && m.streaming && m.type === 'text'
		);
		if (currentSynthetic) {
			const appendedContent = finalWsContent;
			// An empty synthetic that duplicates nothing still appends as a
			// harmless empty phantom; a stale-content one is recognized as
			// already finalized and skipped. Compare against the content we are
			// actually about to append, not the prior synthetic's (stale) content.
			const alreadyFinalized = finalMessages.some(
				(m: ChatMessage) =>
					m.role === 'assistant' &&
					m.type === 'text' &&
					!m.streaming &&
					m.content === (appendedContent || currentSynthetic.content)
			);
			if (!alreadyFinalized) {
				finalMessages = [...finalMessages, { ...currentSynthetic, content: appendedContent }];
			}
		}
	} else {
		// Poller has a streaming text but the WS buffer is empty/behind — nothing
		// to merge; drop the consumed buffer.
		finalWsContent = '';
	}

	return { messages: finalMessages, wsStreamContent: finalWsContent };
}

/**
 * Shallow structural compare of two active-task lists. Returns true when they
 * are equivalent for rendering (same length + same per-task fields that affect
 * display). Lets the poll callback keep the existing array reference when only
 * messages/usage changed, avoiding needless ActiveTasks re-renders.
 */
function sameTasks(existing: Task[], incoming: Task[]): boolean {
	if (existing === incoming) return true;
	if (existing.length !== incoming.length) return false;
	for (let i = 0; i < existing.length; i++) {
		const a = existing[i];
		const b = incoming[i];
		if (
			a.id !== b.id ||
			a.status !== b.status ||
			a.kind !== b.kind ||
			a.subject !== b.subject ||
			a.summary !== b.summary ||
			a.updatedAt !== b.updatedAt
		) {
			return false;
		}
	}
	return true;
}

function sameAgentUsage(
	existing: ChatMessage['agentUsage'],
	incoming: ChatMessage['agentUsage']
): boolean {
	if (existing === incoming) return true;
	if (!existing || !incoming) return !existing && !incoming;
	return (
		existing.total_tokens === incoming.total_tokens &&
		existing.tool_uses === incoming.tool_uses &&
		existing.duration_ms === incoming.duration_ms &&
		existing.input_tokens === incoming.input_tokens &&
		existing.output_tokens === incoming.output_tokens
	);
}

/**
 * Phase 3 of the poll merge — stabilize message identity across polls.
 *
 * The poller rebuilds the message array every cycle and the backend may hand a
 * given block a different id over its lifetime (e.g. a streaming text first
 * appears under the per-turn `message_id` synthetic, then settles to a stable
 * `text-{uuid}`). To avoid re-rendering churn we adopt the EXISTING client id
 * for a server message that matches an existing one — by id, or by position +
 * content-prefix when the id changed.
 *
 * THE DUPLICATE-ID BUG this guards against:
 * A streaming turn injects a synthetic placeholder seeded with the per-TURN
 * `message_id` (one id for the whole turn). As the turn emits text → tool →
 * text → tool …, each NEW streaming text block position-matches the PRIOR
 * streaming text / synthetic — and because an empty synthetic has zero-length
 * content, the prefix check (`shorter === 0`) matches ANYTHING. Without a guard
 * every distinct server `text-{uuid}` block would be swapped onto that one
 * synthetic id, so the whole turn's text blocks collapse to a single id.
 * `displayItems` then dedups by id and silently drops all but the first text —
 * the "assistant messages vanish mid-stream but reappear on refresh" bug (a
 * cold load has no synthetic to collapse onto).
 *
 * The guard: an existing (client) id may be adopted by AT MOST ONE message per
 * pass. Any later match that would reuse an already-claimed id keeps the
 * server's own (unique) id instead. A final safety net guarantees the output
 * never contains two messages with the same id, regardless of how the inputs
 * arrived (e.g. a stale duplicate already present in `incoming`).
 *
 * Pure function (no store access) so it can be unit-tested directly.
 *
 * @returns the stabilized array + whether anything actually changed (when
 *   `changed` is false the caller can keep the `incoming` reference as-is).
 */
export function stabilizeMessageIdentity(
	incoming: ChatMessage[],
	prior: ChatMessage[]
): { messages: ChatMessage[]; changed: boolean } {
	if (prior.length === 0 || incoming.length === 0) {
		return { messages: incoming, changed: false };
	}

	const existingById = new Map<string, ChatMessage>();
	for (const m of prior) {
		if (m.id) existingById.set(m.id, m);
	}

	let needsStabilization = false;
	// Every id already emitted in this pass. An existing client id may be
	// adopted only once; the server's ids are globally unique so they are a
	// safe fallback when adoption would collide.
	const assignedIds = new Set<string>();

	const stabilized = incoming.map((msg, i) => {
		let existing = msg.id ? existingById.get(msg.id) : undefined;

		if (!existing) {
			const positional = prior[i];
			if (positional && positional.role === msg.role && positional.type === msg.type) {
				let positionMatchValid = true;
				if (msg.type === 'tool_use') {
					positionMatchValid = msg.toolName === positional.toolName;
				} else if (msg.type === 'text' && msg.content && positional.content) {
					const shorter = Math.min(msg.content.length, positional.content.length, 50);
					positionMatchValid = shorter === 0 ||
						msg.content.slice(0, shorter) === positional.content.slice(0, shorter);
				}
				if (positionMatchValid) {
					existing = positional;
				}
			}
		}

		// Adopting `existing.id` is only allowed if no earlier message in this
		// pass already claimed it — otherwise the distinct server blocks would
		// collapse onto one id (the bug above).
		const canAdopt = (id: string | undefined): boolean =>
			!!id && !assignedIds.has(id);

		let out: ChatMessage;
		if (!existing) {
			out = msg;
		} else if (msg.type === 'subagent') {
			const existChildren = existing.agentChildren;
			const incomChildren = msg.agentChildren;
			const sameLen = (existChildren?.length ?? 0) === (incomChildren?.length ?? 0);
			const existLast = existChildren?.[(existChildren?.length ?? 0) - 1];
			const incomLast = incomChildren?.[(incomChildren?.length ?? 0) - 1];
			const sameTail =
				existLast?.content === incomLast?.content &&
				existLast?.toolResult === incomLast?.toolResult &&
				existLast?.toolStatus === incomLast?.toolStatus &&
				existLast?.streaming === incomLast?.streaming;
			if (
				existing.agentStatus === msg.agentStatus &&
				existing.streaming === msg.streaming &&
				existing.content === msg.content &&
				sameAgentUsage(existing.agentUsage, msg.agentUsage) &&
				sameLen &&
				sameTail
			) {
				if (existing !== msg) needsStabilization = true;
				out = existing;
			} else if (existing.id !== msg.id && canAdopt(existing.id)) {
				needsStabilization = true;
				out = { ...msg, id: existing.id };
			} else {
				out = msg;
			}
		} else if (
			existing.content === msg.content &&
			existing.streaming === msg.streaming &&
			existing.toolResult === msg.toolResult &&
			existing.toolStatus === msg.toolStatus
		) {
			if (existing !== msg) needsStabilization = true;
			out = existing;
		} else if (existing.id !== msg.id && canAdopt(existing.id)) {
			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] phase3 id-swap from=${msg.id} to=${existing.id} role=${msg.role} type=${msg.type} "${(msg.content || '').slice(0, 30)}"`);
			needsStabilization = true;
			out = { ...msg, id: existing.id };
		} else {
			out = msg;
		}

		// Carry server-authoritative identity onto an adopted prior object. When
		// `out === existing` we kept the optimistic message for reference
		// stability — but a just-sent user message is optimistic and carries no
		// `jsonlUuid` until the post-turn poll delivers it on `msg`. Dropping it
		// here is the "rewind blocked until reload" bug: MessageActions sees
		// `messageUuid == null` and refuses to rewind ("missing its checkpoint
		// id"). Only fill when missing so a settled message stays a stable
		// reference (no churn) on subsequent polls.
		if (out === existing && existing && msg.jsonlUuid && !existing.jsonlUuid) {
			out = { ...existing, jsonlUuid: msg.jsonlUuid };
			needsStabilization = true;
		}

		// Same one-way fill for the creation timestamp. Mid-turn polls serve
		// live-store messages that carry no persisted metadata.timestamp, so an
		// incoming message replacing a client-stamped prior one must keep the
		// prior send time; conversely a kept optimistic message gains the
		// persisted time once the post-turn poll delivers it. Either way the
		// timestamp never regresses to undefined — which the UI renders as "now".
		if (!out.timestamp && (msg.timestamp || existing?.timestamp)) {
			out = { ...out, timestamp: msg.timestamp || existing?.timestamp };
			needsStabilization = true;
		}

		// Final safety net: never emit two messages with the same id. If the
		// chosen id is already taken (a stale duplicate arrived in `incoming`,
		// or the match picked an id another message already claimed), fall back
		// to a position-stable unique id so the id-dedup downstream can't drop a
		// real message. `i` is stable across polls for a given turn structure,
		// so this introduces no churn.
		if (out.id && assignedIds.has(out.id)) {
			needsStabilization = true;
			out = { ...out, id: `${out.id}#${i}` };
		}
		if (out.id) assignedIds.add(out.id);
		return out;
	});

	return { messages: needsStabilization ? stabilized : incoming, changed: needsStabilization };
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createSessionInit(deps: SessionInitDeps) {
	const {
		update, getState, notifySubscribers, defaultState, setState,
		internal, sessionPoller,
		wsConn, switcher, appData,
		disconnect: disconnectFn, persistSession, persistTimer,
	} = deps;

	// Sessions for which we've already surfaced a "polling keeps failing" toast.
	// The poller fires onPollError on EVERY poll at/after the consecutive-error
	// threshold, so without this guard a wedged session would spam a toast every
	// tick. Cleared on the next successful poll-apply so a later failure episode
	// re-notifies once.
	const _pollErrorNotified = new Set<string>();

	// ─── Poll callback ───────────────────────────────────────────────
	// Direct callback from the poller → activeSession, replacing the old bridge
	// subscription pattern. One _update() call per poll cycle — no double-update
	// risk that corrupted Svelte 5's effect linked-list.

	function setupPollCallback() {
		// Tell the poller when the active session is mid-`interrupt_for_query` so
		// its stop/park lifecycle leaves the timer alive across the drain window
		// (the old query reports is_streaming=false before the replacement query
		// starts streaming). `interruptPending` is the single source of truth —
		// set in message-sender, cleared by the `start` handler / first fresh poll.
		sessionPoller.setInterruptGuard?.(
			(sessionId) => internal.interruptPending && getState().sessionId === sessionId
		);

		sessionPoller.onPollApplied((sessionId, data) => {
			// A poll succeeded — reset the poll-error notify guard so a future error
			// episode surfaces one fresh toast (see setupOnPollError). Must run
			// BEFORE the active-session gate: a background session recovering
			// while another is focused would otherwise keep its stale guard entry
			// and silently swallow the toast for its next failure episode.
			_pollErrorNotified.delete(sessionId);

			const _state = getState();
			if (sessionId !== _state.sessionId) {
				console.debug(`[PollCB] skipped: poll for ${sessionId} but active is ${_state.sessionId}`);
				return;
			}

			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] poll-apply start sid=${sessionId.slice(0,8)} stateLen=${_state.messages.length} stateLastId=${_state.messages[_state.messages.length - 1]?.id} stateLastRole=${_state.messages[_state.messages.length - 1]?.role} dataLen=${data.messages.length} dataLastId=${data.messages[data.messages.length - 1]?.id} dataLastRole=${data.messages[data.messages.length - 1]?.role} streaming=${data.isStreaming}`);

			// During interrupt_for_query, STALE polls from the old query can
			// return with `isStreaming=false` and would clobber the optimistic
			// `isStreaming=true` set by the interrupt path. But fresh polls
			// where the server already sees the NEW query as streaming are
			// safe — and vital, because they carry the stream buffer merge
			// that renders `wsStreamContent` into the UI. Previously we
			// blanket-skipped all polls while `interruptPending` was true,
			// which meant `stream_delta` events piled into `wsStreamContent`
			// but never rendered until the late `start` event cleared the
			// gate — UI lag of 1-3s after send-now on a paragraph-sized
			// response. Now: skip only stale polls; accept the first fresh
			// poll and clear the gate so buffer merge resumes immediately.
			if (internal.interruptPending) {
				if (!data.isStreaming) {
					console.debug(`[PollCB] skipped: interruptPending stale poll for ${sessionId}`);
					return;
				}
				console.debug(`[PollCB] interruptPending cleared by fresh poll (isStreaming=true) for ${sessionId}`);
				internal.interruptPending = false;
			}

			// Build usage delta — don't overwrite a valid contextUsed with 0.
			const usageDelta = usageStoreUpdate(data.usage);
			if (usageDelta.contextUsed === 0 && _state.contextUsed && _state.contextUsed > 0) {
				usageDelta.contextUsed = _state.contextUsed;
			}
			const usageHasUpdate = usageDelta.contextUsed !== _state.contextUsed;
			const streamingChanged = _state.isStreaming !== data.isStreaming;

			// Active background tasks: fresh on every poll, independent of message
			// deltas. Compare cheaply by length + per-index identity-ish fields so a
			// poll that only changed message content doesn't churn the tasks array
			// (and vice-versa). `data.activeTasks` is always present (poller defaults
			// it to []), but guard for older mocked responses.
			const incomingTasks = data.activeTasks ?? [];
			const tasksChanged = !sameTasks(_state.activeTasks, incomingTasks);

			// Build worktree metadata delta from server (corrects stale client state)
			const metaDelta: Partial<ActiveSessionState> = {};
			if (data.sessionMeta) {
				const meta = data.sessionMeta;
				if (meta.title && meta.title !== _state.title) {
					metaDelta.title = meta.title;
				}
				if (meta.worktree_deleted !== undefined && meta.worktree_deleted !== _state.worktreeDeleted) {
					metaDelta.worktreeDeleted = meta.worktree_deleted;
				}
				if (meta.worktree_id !== undefined && meta.worktree_id !== _state.worktreeId) {
					metaDelta.worktreeId = meta.worktree_id;
				}
				if (meta.worktree_branch !== undefined && meta.worktree_branch !== null && meta.worktree_branch !== _state.worktreeBranch) {
					metaDelta.worktreeBranch = meta.worktree_branch;
				}
				if (meta.execution_mode !== undefined && meta.execution_mode !== _state.executionMode) {
					metaDelta.executionMode = meta.execution_mode as 'local' | 'worktree' | 'cloud';
				}
				// No-worktree cloud sessions carry their checked-out branch here (the
				// worker persisted it at intake). Surface it into `cloudBranch` so the
				// branch pill shows it after a reload, when ExecutionModeBar (which
				// otherwise preselects it) isn't mounted. cloud+worktree uses
				// `worktree_branch` above instead.
				if (meta.cloud_branch !== undefined && meta.cloud_branch !== null && meta.cloud_branch !== _state.cloudBranch) {
					metaDelta.cloudBranch = meta.cloud_branch;
				}
				// Cross-install control reachability: a remote session whose owner is
				// offline is served from shared-DB history (view-only). `controllable`
				// is absent for local sessions → default to true so they stay sendable.
				const controllable = meta.controllable !== false;
				if (controllable !== _state.remoteControllable) {
					metaDelta.remoteControllable = controllable;
				}
				// Reason the owner couldn't be reached (registry-online but the /live
				// relay failed). `relay_error` isn't in the SessionMeta type (owned by
				// the poller module) but survives on the raw response — read it via a
				// narrow cast. Clear it whenever the session is controllable again so a
				// recovered device drops the stale "could not reach" banner.
				const relayError = controllable
					? undefined
					: (meta as { relay_error?: string }).relay_error;
				if (relayError !== _state.relayError) {
					metaDelta.relayError = relayError;
				}
			}
			const metaHasUpdate = Object.keys(metaDelta).length > 0;

			// Skip if nothing actually changed
			const msgsMatch = _state.messages === data.messages;
			if (msgsMatch && !usageHasUpdate && !streamingChanged && !metaHasUpdate && !tasksChanged) {
				console.debug(`[PollCB] skipped: no changes for ${sessionId}`);
				return;
			}

			if (metaHasUpdate) {
				console.debug(`[PollCB] applying worktree correction:`, metaDelta);
			}
			console.debug(`[PollCB] applying: ${data.messages.length} msgs, streaming=${data.isStreaming}, msgsChanged=${!msgsMatch}, streamingChanged=${streamingChanged}`);

			// ── Diagnostic: trace streaming→ended transition ────────────
			if (STREAM_DEBUG && streamingChanged && !data.isStreaming) {
				const assistantTexts = data.messages.filter(
					(m: ChatMessage) => m.role === 'assistant' && m.type === 'text'
				);
				const activeAssistantTexts = _state.messages.filter(
					(m: ChatMessage) => m.role === 'assistant' && m.type === 'text'
				);
				console.warn(
					`[PollCB:StreamEnd] streaming→false transition`,
					`\n  server assistant texts: ${assistantTexts.length} [${assistantTexts.map((m: ChatMessage) => `id=${m.id} len=${(m.content||'').length} streaming=${m.streaming}`).join(', ')}]`,
					`\n  active assistant texts: ${activeAssistantTexts.length} [${activeAssistantTexts.map((m: ChatMessage) => `id=${m.id} len=${(m.content||'').length} streaming=${m.streaming}`).join(', ')}]`,
					`\n  wsStreamContent len: ${_state.wsStreamContent.length}`,
					`\n  total server msgs: ${data.messages.length}, total active msgs: ${_state.messages.length}`
				);
			}

			// Single authoritative update — poller is the sole writer of session state.
			// Three-phase merge:
			// 1. Preserve client-side-only messages (hooks, status notifications)
			// 2. Merge WS stream buffer when ahead of poller content
			// 3. Stabilize message identity to prevent DOM recreation flicker
			update(s => {
				let finalMessages = data.messages;
				let finalWsContent = s.wsStreamContent;

				// ── Phase 1: Preserve client-side-only messages ─────────────
				const serverIdSet = new Set(finalMessages.map(m => m.id));
				const clientOnlyEntries: Array<{msg: ChatMessage; anchorId: string | null; srcIdx: number}> = [];
				for (let i = 0; i < s.messages.length; i++) {
					const m = s.messages[i];
					if (m.type === 'system' && !serverIdSet.has(m.id)) {
						let anchorId: string | null = null;
						for (let j = i - 1; j >= 0; j--) {
							if (serverIdSet.has(s.messages[j].id)) {
								anchorId = s.messages[j].id;
								break;
							}
						}
						clientOnlyEntries.push({ msg: m, anchorId, srcIdx: i });
					}
				}

				// Reconcile ALL optimistic user messages in state with server
				// payload. Optimistic msgs use id `user-${Date.now()}`; server
				// persists them under a different DB id (`msg-N`), so id-based
				// matching alone leaves both in the merged list (duplicate).
				//
				// Algorithm: for every optimistic `user-*` msg anywhere in
				// s.messages (not just trailing — a prior merge may have placed
				// it mid-list when no anchor existed), find the first unclaimed
				// server `role:user` msg of identical content. Match → drop
				// optimistic (server absorbed it). No match → preserve optimistic
				// (server race — still persisting). Preserving prevents DOM
				// shrink, scrollTop clamp, false scroll-up detection, and
				// auto-scroll disable.
				//
				// We do NOT compare profileId — server payload uses snake_case
				// (`profile_id`) while optimistic uses camelCase (`profileId`);
				// they wouldn't match. Content+role is sufficient because the
				// full text prompt is unique enough and claim-once-per-server-msg
				// prevents cross-pairing when the session has repeats.
				// Scan server msgs from newest→oldest: the optimistic is always
				// the most-recently-typed prompt, so it must pair with the LAST
				// matching server user msg. Forward-scan would incorrectly claim
				// an older identical-content msg (e.g., user sends same text
				// twice), leaving the new server msg unclaimed → both render.
				const claimedServerIdx = new Set<number>();
				const findMatchingServerUserIdx = (opt: ChatMessage): number => {
					for (let k = finalMessages.length - 1; k >= 0; k--) {
						if (claimedServerIdx.has(k)) continue;
						const sm = finalMessages[k];
						if (sm.role !== 'user') continue;
						if ((sm.content || '') !== (opt.content || '')) continue;
						return k;
					}
					return -1;
				};
				for (let i = 0; i < s.messages.length; i++) {
					const m = s.messages[i];
					if (serverIdSet.has(m.id)) continue;
					if (m.role !== 'user' || !m.id?.startsWith('user-')) continue;
					const matchIdx = findMatchingServerUserIdx(m);
					if (matchIdx >= 0) {
						// Server persisted this msg under its own id → drop optimistic.
						// eslint-disable-next-line no-console
						if (STREAM_DEBUG) console.log(`[AS] phase1 drop-optimistic optId=${m.id} serverId=${finalMessages[matchIdx]?.id} serverIdx=${matchIdx} "${(m.content || '').slice(0, 30)}"`);
						claimedServerIdx.add(matchIdx);
						continue;
					}
					// No server match — preserve optimistic. Anchor to the most
					// recent server-known msg before this position. If none
					// exists (new session, first msg), anchor to the last server
					// msg in finalMessages so the optimistic appends at the tail
					// (where the user actually sent it) rather than prepending
					// to the head of the chat.
					let anchorId: string | null = null;
					for (let j = i - 1; j >= 0; j--) {
						if (serverIdSet.has(s.messages[j].id)) {
							anchorId = s.messages[j].id;
							break;
						}
					}
					if (!anchorId && finalMessages.length > 0) {
						anchorId = finalMessages[finalMessages.length - 1].id;
					}
					clientOnlyEntries.push({ msg: m, anchorId, srcIdx: i });
				}
				if (clientOnlyEntries.length > 0) {
					finalMessages = [...finalMessages];
					const idToIndex = new Map<string, number>();
					for (let i = 0; i < finalMessages.length; i++) {
						if (finalMessages[i].id) idToIndex.set(finalMessages[i].id, i);
					}
					// The single-offset splice below assumes entries arrive in
					// ascending insert position. They were collected in TWO passes
					// (system msgs, then optimistic users), so a late-anchored
					// system msg can precede an early-anchored user msg and the
					// offset math would shove the user msg past it. Restore the
					// original s.messages ordering before splicing.
					clientOnlyEntries.sort((a, b) => a.srcIdx - b.srcIdx);
					let offset = 0;
					for (const { msg, anchorId } of clientOnlyEntries) {
						let insertIdx: number;
						if (anchorId && idToIndex.has(anchorId)) {
							insertIdx = idToIndex.get(anchorId)! + 1 + offset;
						} else {
							insertIdx = 0 + offset;
						}
						insertIdx = Math.min(insertIdx, finalMessages.length);
						finalMessages.splice(insertIdx, 0, msg);
						offset++;
					}
				}

				// ── Phase 2: WS stream buffer merge ────────────────────────
				// Reconcile the live WS partial-text buffer with the poller's
				// authoritative messages. Pure logic lives in mergeWsStreamBuffer
				// (exported + unit-tested) — it also drops a STALE buffer so a
				// post-tool think-gap synthetic stays a transparent empty phantom
				// (the "dead air after tools" bug).
				{
					const merged = mergeWsStreamBuffer(finalMessages, finalWsContent, s.messages, data.isStreaming);
					finalMessages = merged.messages;
					finalWsContent = merged.wsStreamContent;
				}

				// ── Phase 3: Stabilize message identity ────────────────────
				// Pure logic in stabilizeMessageIdentity (exported + unit-tested).
				// It adopts existing client ids to avoid render churn while
				// GUARANTEEING unique output ids — otherwise a turn's text blocks
				// collapse onto the single per-turn synthetic id and displayItems'
				// id-dedup silently drops all but the first (messages vanish
				// mid-stream, reappear on refresh).
				{
					const stab = stabilizeMessageIdentity(finalMessages, s.messages);
					finalMessages = stab.messages;
				}

				// ── No-op detection ────────────────────────────────────────
				const msgsUnchanged = finalMessages === s.messages ||
					(finalMessages.length === s.messages.length &&
					 finalMessages.every((m, i) => m === s.messages[i]));
				if (
					msgsUnchanged &&
					finalWsContent === s.wsStreamContent &&
					data.isStreaming === s.isStreaming &&
					!usageHasUpdate && !streamingChanged && !metaHasUpdate && !tasksChanged
				) {
					return s;
				}

				return {
					...s,
					messages: msgsUnchanged ? s.messages : finalMessages,
					isStreaming: data.isStreaming,
					streamStartTime: data.isStreaming ? s.streamStartTime : null,
					wsStreamContent: finalWsContent,
					// Only swap the array reference when tasks actually changed so a
					// pure message/usage poll doesn't re-render the ActiveTasks strip.
					activeTasks: tasksChanged ? incomingTasks : s.activeTasks,
					...usageDelta,
					...metaDelta,
				};
			});

			// ── Diagnostic: verify post-update state ────────────────
			if (STREAM_DEBUG && streamingChanged && !data.isStreaming) {
				const postState = getState();
				const postAssistantTexts = postState.messages.filter(
					(m: ChatMessage) => m.role === 'assistant' && m.type === 'text'
				);
				console.warn(
					`[PollCB:StreamEnd:POST] after update`,
					`\n  isStreaming: ${postState.isStreaming}`,
					`\n  total msgs: ${postState.messages.length}`,
					`\n  assistant texts: ${postAssistantTexts.length} [${postAssistantTexts.map((m: ChatMessage) => `id=${m.id} len=${(m.content||'').length} streaming=${m.streaming}`).join(', ')}]`,
					`\n  wsStreamContent len: ${postState.wsStreamContent.length}`
				);
			}

			// Persist corrected worktree state so the fix survives page refresh.
			if (metaHasUpdate) {
				persistSession();
			}
		});
	}

	// ─── onStreamEnd callback ────────────────────────────────────────
	function setupOnStreamEnd() {
		sessionPoller.onStreamEnd((sessionId, _usage) => {
			const _state = getState();
			if (internal.interruptPending && _state.sessionId === sessionId) return;

			if (_state.isStreaming && _state.sessionId === sessionId && sessionPoller.isPolling(sessionId)) {
				return;
			}

			appData.loadSessions();
		});
	}

	// ─── onPollError callback ────────────────────────────────────────
	function setupOnPollError() {
		sessionPoller.onPollError((sessionId, consecutiveErrors) => {
			// Only surface errors for the session the user is actually looking at —
			// a background session's poll failing shouldn't toast.
			if (sessionId !== getState().sessionId) return;
			// Non-spammy: one toast per failure episode (reset on the next good poll).
			if (_pollErrorNotified.has(sessionId)) return;
			_pollErrorNotified.add(sessionId);

			console.warn(`[ActiveSession] poll errors for ${sessionId} (${consecutiveErrors} consecutive)`);
			notify.error(
				"Lost connection to this chat — it may be out of date. It'll refresh automatically when the connection recovers.",
				{ category: 'session-poll-error', session_id: sessionId }
			);
		});
	}

	// ─── init ────────────────────────────────────────────────────────

	async function init() {
		const initGen = bumpInitGeneration();
		setupOnStreamEnd();
		setupOnPollError();
		setupPollCallback();

		const skipRestore = getSkipRestore();
		console.debug(`[ActiveSession] init(): skipRestore=${skipRestore}, initGen=${initGen}, currentSessionId=${getState().sessionId}, messages=${getState().messages.length}`);

		try {
			const backendSelection = await switcher.loadSelectionFromBackend();

			if (initGen !== getInitGeneration()) {
				console.debug(`[ActiveSession] init(): stale (gen ${initGen} < ${getInitGeneration()}), bailing`);
				return;
			}

			const defaultProfile = backendSelection?.selectedProfile || switcher.getPersistedProfile();
			const defaultProject = backendSelection?.selectedProject || switcher.getPersistedProject();

			if (skipRestore) {
				console.debug('[ActiveSession] init(): skipRestore=true, keeping cleared state');
				update(s => ({
					...s,
					profile: s.profile || defaultProfile,
					project: s.project || defaultProject,
				}));
			} else {
				const persisted = await switcher.loadPersistedSession();

				if (initGen !== getInitGeneration()) {
					console.debug(`[ActiveSession] init(): stale after loadPersistedSession (gen ${initGen} < ${getInitGeneration()}), bailing`);
					return;
				}

				console.debug(`[ActiveSession] init(): persisted sessionId=${persisted?.sessionId ?? 'null'}`);

				if (persisted) {
					update(s => ({
						...s,
						sessionId: persisted.sessionId ?? null,
						// Adopting a sessionId whose history hasn't rendered yet opens
						// the restore-race window: the UI shows "New Chat" while sends
						// would land in the old session. Mark it until onConnected's
						// loadSession settles. !! (not `!== null`): a persisted entry
						// missing the key yields undefined, which must not strand the
						// flag as true with nothing to restore.
						isRestoringSession: !!persisted.sessionId,
						title: persisted.title || 'New Chat',
						profile: persisted.profile || defaultProfile,
						project: persisted.project || defaultProject,
						modelOverride: persisted.modelOverride ?? null,
						permissionModeOverride: persisted.permissionModeOverride ?? null,
						effortLevelOverride: persisted.effortLevelOverride ?? null,
						executionMode: persisted.executionMode || 'local',
						worktreeBranch: persisted.worktreeBranch || '',
						worktreeBaseBranch: persisted.worktreeBaseBranch || 'main',
						worktreeId: persisted.worktreeId ?? null,
						worktreeMode: persisted.worktreeMode || 'new',
						selectedWorktreeBranch: persisted.selectedWorktreeBranch ?? null,
						worktreeDeleted: persisted.worktreeDeleted ?? false,
					}));
				} else {
					update(s => ({ ...s, profile: defaultProfile, project: defaultProject }));
				}
			}

			wsConn.connect();
		} catch (error) {
			console.error('[ActiveSession] Failed to initialize:', error);
			wsConn.connect();
		}
	}

	// ─── destroy ─────────────────────────────────────────────────────

	function destroy() {
		disconnectFn();
		sessionPoller.stopAll();
		wsConn.destroy();
		switcher.destroy();
		persistTimer.clear();
		setState(defaultState());
		notifySubscribers();
	}

	return { init, destroy };
}

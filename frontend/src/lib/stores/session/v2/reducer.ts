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
	/** Context breakdown for the most recent model call, as carried by
	 *  `turn.usage`. Same shape as the `/context-usage` fetch, so the bar can
	 *  refresh per model call instead of polling. Null until a snapshot
	 *  arrives; a `turn.usage` without one leaves the previous value in place. */
	contextUsage: Record<string, unknown> | null;
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
	/** Profile (agent) id the live turn is running as, from `turn.started`.
	 *  Messages created on the live SSE path (streaming deltas, and a
	 *  `message.user` whose event carries no profile) are stamped with it at
	 *  CREATION time, so their name header stays pinned to the agent that
	 *  actually produced them instead of following the current selection.
	 *  Cleared on turn end — safe because stamping happens at creation. */
	liveTurnProfileId: string | null;
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
	 *  with `convertServerMessage`.
	 *
	 *  A `Map`, and the ONE structure in this state the reducer mutates in place
	 *  (see "Copy-on-publish ownership" below): it is pure bookkeeping, never
	 *  rendered, and copying it per appended message is what made a large
	 *  backfill quadratic. */
	messageOriginSeq: Map<string, number>;
	/** Same, for `activeTaskSummaries` entries (keyed by task id). Stays a plain
	 *  record — tasks are a handful per session, not one per event. */
	taskOriginSeq: Record<string, number>;
	/** Whether ANY `provider_retry` card exists in `messages`.
	 *
	 *  Pure derived bookkeeping, kept because the retry-card sweeps below run on
	 *  hot events (every tool.use, every user message) and each one scanned the
	 *  entire transcript to discover that the overwhelmingly common answer is
	 *  "there are no retry cards" — O(N) per event, O(N²) over a replay. Set on
	 *  append, recomputed on rewind. */
	hasRetryCards: boolean;
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
		contextUsage: null,
		lastSeq: 0,
		openTextId: null,
		openThinkingId: null,
		liveTurnId: null,
		liveTurnProfileId: null,
		pendingPumpAck: null,
		pumpAckBuffer: '',
		liveModel: null,
		liveRequestedModel: null,
		liveProvider: null,
		messageOriginSeq: own(new Map<string, number>()),
		taskOriginSeq: {},
		hasRetryCards: false,
		speculativelySettledCardIds: [],
		speculativelySettledTaskIds: [],
	};
}

// ─── Small immutable helpers ──────────────────────────────────────────────────

/**
 * Patch one message by id.
 *
 * Same value semantics as before (returns the array to store on the next
 * state, unchanged when the id is absent), but it no longer maps the whole
 * transcript into a fresh array per event: it patches the one slot, copying the
 * array only when this lineage does not already own it (see "Copy-on-publish
 * ownership"). Searches from the END because every hot caller — a tool result,
 * a subagent update, closing the open text block — targets something recent.
 */
function replaceById(messages: ChatMessage[], id: string, patch: Partial<ChatMessage>): ChatMessage[] {
	let idx = -1;
	for (let i = messages.length - 1; i >= 0; i--) {
		if (messages[i].id === id) {
			idx = i;
			break;
		}
	}
	if (idx === -1) return messages;
	const next = ownedMessages.has(messages) ? messages : own(messages.slice());
	next[idx] = { ...next[idx], ...patch };
	return next;
}

/**
 * Card lookups. Both card kinds have a DETERMINISTIC id (`tooluse-<id>` /
 * `subagent-<id>`) and are only ever created through `appendMessage`, so
 * `messageOriginSeq` — passed as `known` — answers "does this card exist?" in
 * O(1). Without it, the miss case (every fresh tool call during a backfill)
 * scanned the whole transcript, which is O(N²) over a replay. Optimistic
 * messages are absent from that map, but they are only ever user text bubbles,
 * never cards.
 */
function findByToolId(
	messages: ChatMessage[],
	toolId: string,
	known?: Map<string, number>
): ChatMessage | undefined {
	if (known && !known.has(`tooluse-${toolId}`)) return undefined;
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (m.type === 'tool_use' && m.toolId === toolId) return m;
	}
	return undefined;
}

function findSubagentByTask(
	messages: ChatMessage[],
	taskId: string,
	known?: Map<string, number>
): ChatMessage | undefined {
	// Subagent cards are correlated across started/progress/completed by task_id
	// (agent_id only appears on `subagent.started`). See shape note in reduce().
	if (known && !known.has(`subagent-${taskId}`)) return undefined;
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (m.type === 'subagent' && m.toolId === taskId) return m;
	}
	return undefined;
}

/**
 * Append a thinking delta to the text already accumulated on the open block.
 *
 * Consecutive thinking BLOCKS (the CLI's per-step summaries, each of the form
 * `**Title**\n...`) land as separate deltas on the same open message, and bare
 * concatenation glued them into `**A****B**` — markdown renders the middle `**`
 * literally, which is exactly the "Implementing X****Refactoring Y" the
 * transcript showed. A boundary where the accumulated text ENDS with a bold
 * close and the incoming chunk OPENS one can only ever be two adjacent spans, so
 * it gets a paragraph break. Every other boundary (a coalescer splitting one
 * block mid-sentence) is untouched.
 */
export function joinThinking(previous: string, chunk: string): string {
	if (!previous) return chunk;
	if (previous.trimEnd().endsWith('**') && chunk.trimStart().startsWith('**')) {
		return `${previous.trimEnd()}\n\n${chunk.trimStart()}`;
	}
	return previous + chunk;
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
 * The one exception is the background-SHELL lane, which the park gate skips: a
 * `kind === 'shell'` summary outliving turn.completed is live, not stale. See
 * the guard in the summaries pass below.
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
		// Background SHELLS are exempt. A `Bash(run_in_background)` shell
		// legitimately outlives a COMPLETED turn — the park gate deliberately
		// excludes the monitor-shell lane, so the turn completes while the shell
		// keeps running and wakes the session later via the completion watcher.
		// Shells always receive their terminal task.updated (registry watcher +
		// boot reconcile), so this heal — built for AGENT cards whose
		// subagent.completed frame can be lost — has nothing to fix here.
		// Settling them hid live background shells from the tasks strip.
		if (t.kind === 'shell') return t;
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

/**
 * Close out open retry cards when the turn reaches a terminal state.
 *
 * A `provider_retry` card renders as live work — a spinner counting attempts —
 * because the engine is mid-retry when the note arrives. Nothing else ever
 * finishes it: the engine stops emitting notes on success, so without this the
 * card spins forever after the turn is over. Stamping `settled` lets
 * SystemMessage render it as a finished "Retried N times" row.
 *
 * An `exhausted` card is left alone: it records a failure and owns the Retry
 * button, which the settled rendering does not offer. Already-settled cards are
 * skipped so the array identity survives a replay folding the same log twice.
 */
function settleRetryCards(messages: ChatMessage[], present = true): ChatMessage[] {
	if (!present) return messages;
	let touched = false;
	const out = messages.map((m) => {
		if (m.role !== 'system' || m.systemSubtype !== 'provider_retry') return m;
		const data = m.systemData as Record<string, unknown> | undefined;
		if (data?.exhausted || data?.settled) return m;
		touched = true;
		return { ...m, systemData: { ...(data ?? {}), settled: true } };
	});
	return touched ? out : messages;
}

/** systemData of a `provider_retry` card, or undefined for any other message. */
function retryCardData(m: ChatMessage): Record<string, unknown> | undefined {
	if (m.role !== 'system' || m.systemSubtype !== 'provider_retry') return undefined;
	return m.systemData as Record<string, unknown> | undefined;
}

/**
 * The resume launched from an exhausted card produced a real reply.
 *
 * The provider can sit silent for a minute or two before the first token, so
 * `resuming` alone never proves anything came back. Recovery is the first sign
 * of an actual answer — text, thinking, a tool call, or the turn finishing —
 * and only then does the card give up its Retry button for good.
 *
 * Returns the SAME array when no card is resuming, so the hot delta paths pay
 * one `some()` and a replay folding the same log twice lands on equal state.
 */
function markRetryRecovered(messages: ChatMessage[], present = true): ChatMessage[] {
	if (!present) return messages;
	if (!messages.some((m) => retryCardData(m)?.resuming)) return messages;
	return messages.map((m) => {
		const d = retryCardData(m);
		if (!d?.resuming) return m;
		return { ...m, systemData: { ...d, resuming: false, recovered: true } };
	});
}

/** markRetryRecovered lifted to the state, keeping identity when nothing moved. */
function withRetryRecovered(state: V2SessionState): V2SessionState {
	const messages = markRetryRecovered(state.messages, state.hasRetryCards);
	return messages === state.messages ? state : { ...state, messages };
}

/**
 * A resume turn ended without a reply — re-arm the Retry button.
 *
 * Every terminal abort lands here (interrupt, error, anything else): the card
 * stops reading as in-flight and offers the way back again, because nothing
 * else will. Cards that already recovered are untouched — they hold no
 * `resuming` flag by then.
 */
function clearRetryResuming(messages: ChatMessage[], present = true): ChatMessage[] {
	if (!present) return messages;
	if (!messages.some((m) => retryCardData(m)?.resuming)) return messages;
	return messages.map((m) => {
		const d = retryCardData(m);
		if (!d?.resuming) return m;
		return { ...m, systemData: { ...d, resuming: false } };
	});
}

/**
 * The user typed something instead of retrying — retire the spent cards.
 *
 * A reply to a NEW prompt is not this card's recovery, so the card must not
 * claim it. It reads as superseded: no button, no green check.
 */
function supersedeRetryCards(messages: ChatMessage[], present = true): ChatMessage[] {
	if (!present) return messages;
	const stale = (d: Record<string, unknown> | undefined) =>
		!!d?.exhausted && !d.recovered && !d.superseded;
	if (!messages.some((m) => stale(retryCardData(m)))) return messages;
	return messages.map((m) => {
		const d = retryCardData(m);
		if (!stale(d)) return m;
		return { ...m, systemData: { ...d, superseded: true, resuming: false } };
	});
}

function str(v: unknown): string {
	return typeof v === 'string' ? v : '';
}

/**
 * Serve URL for a screenshot content-addressed into the artifact store
 * (`tool.result` `artifact_hashes`, stamped by the recorder's Q5 ingest).
 * Returns undefined unless the first hash is a well-formed sha256 hex digest —
 * the only shape the artifact route serves.
 */
function artifactScreenshotUrl(v: unknown): string | undefined {
	if (!Array.isArray(v)) return undefined;
	const hash = v.find((h) => typeof h === 'string' && /^[0-9a-f]{64}$/.test(h));
	return hash ? `/api/v1/artifacts/${hash}` : undefined;
}

// ─── Copy-on-publish ownership ────────────────────────────────────────────────
//
// `reduce()` is value-semantics from the outside: every call returns a NEW state
// object and never mutates a state anyone else can observe. Naively that also
// meant copying the whole `messages` array and the whole `messageOriginSeq`
// record on every appended event, which makes replaying an N-event backfill
// O(N²) in both time and allocation (the reported cost of a full switch-in
// replay on a long session).
//
// The fix is an ownership boundary. A container the reducer BUILT since the last
// time state left this module is "owned" — nobody outside holds a reference, so
// appending to it in place is unobservable. The moment a state is published to
// the UI (`releaseMessages`) or stored for later (`sealState`), its containers
// are disowned and the next append copies once. Anything the reducer did not
// create (a caller-supplied state, a restored cache entry) is unowned by
// default, so the model fails closed: an unknown array is always copied.
//
// Tracked in WeakSets rather than on the state object so the V2SessionState
// shape — and its byte-parity with `convertServerMessage` output — is unchanged.
const ownedMessages = new WeakSet<ChatMessage[]>();
const ownedOrigins = new WeakSet<Map<string, number>>();

function own<T extends ChatMessage[] | Map<string, number>>(container: T): T {
	if (Array.isArray(container)) ownedMessages.add(container);
	else ownedOrigins.add(container as Map<string, number>);
	return container;
}

/**
 * Hand this state's message array to the outside world (a fold into
 * `activeSession`, a snapshot handed to a consumer). Disowns the array so the
 * next append copies instead of mutating what the consumer now holds.
 */
export function releaseMessages(state: V2SessionState): ChatMessage[] {
	ownedMessages.delete(state.messages);
	return state.messages;
}

/**
 * Freeze a state for storage (e.g. the feed controller's transcript cache).
 * Disowns every mutable container so a later lineage that resumes FROM this
 * snapshot copies on its first write and can never mutate the stored value.
 */
export function sealState(state: V2SessionState): V2SessionState {
	ownedMessages.delete(state.messages);
	ownedOrigins.delete(state.messageOriginSeq);
	return state;
}

/** Append a message and record its origin seq (for `transcript.rewound`). */
function appendMessage(state: V2SessionState, msg: ChatMessage, seq: number): V2SessionState {
	// O(1) when this lineage owns the containers; one O(N) copy otherwise.
	const messages = ownedMessages.has(state.messages)
		? state.messages
		: own(state.messages.slice());
	messages.push(msg);
	const messageOriginSeq = ownedOrigins.has(state.messageOriginSeq)
		? state.messageOriginSeq
		: own(new Map(state.messageOriginSeq));
	messageOriginSeq.set(msg.id, seq);
	const next = { ...state, messages, messageOriginSeq };
	if (!next.hasRetryCards && msg.systemSubtype === 'provider_retry') {
		next.hasRetryCards = true;
	}
	return next;
}

/** Record an origin seq once — the earliest (creation) seq wins on replay. */
function withOrigin(map: Record<string, number>, id: string, seq: number): Record<string, number> {
	return map[id] !== undefined ? map : { ...map, [id]: seq };
}

/** `withOrigin` for the Map-backed message origins (same "earliest wins" rule). */
function withMessageOrigin(
	state: V2SessionState,
	id: string,
	seq: number
): Map<string, number> {
	if (state.messageOriginSeq.has(id)) return state.messageOriginSeq;
	const next = ownedOrigins.has(state.messageOriginSeq)
		? state.messageOriginSeq
		: own(new Map(state.messageOriginSeq));
	next.set(id, seq);
	return next;
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
		const screenshotUrl = artifactScreenshotUrl(payload.artifact_hashes);
		const out = [...children];
		if (targetToolId) {
			for (let i = out.length - 1; i >= 0; i--) {
				if (out[i].type === 'tool_use' && out[i].toolId === targetToolId) {
					out[i] = {
						...out[i],
						toolResult: resultText,
						toolStatus: payload.is_error ? 'error' : 'complete',
						...(screenshotUrl ? { toolScreenshot: screenshotUrl } : {}),
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
			...(screenshotUrl ? { toolScreenshot: screenshotUrl } : {}),
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

/**
 * Fold one wire event into the session state.
 *
 * Ownership contract (see "Copy-on-publish ownership"): `reduce` CONSUMES
 * `state` — the returned state supersedes it, and the input must not be reduced
 * down a second, divergent branch. Snapshot with `sealState(state)` first if you
 * need to keep a state you will branch from (that is exactly what the feed
 * controller's transcript cache does), and `releaseMessages(state)` before
 * handing `state.messages` to a consumer that keeps the reference.
 */
export function reduce(state: V2SessionState, event: WireEvent): V2SessionState {
	const createdAt = str(event.created_at);
	// lastSeq only ever advances (replay is monotonic).
	const seq = typeof event.seq === 'number' ? event.seq : state.lastSeq;
	const withSeq = seq > state.lastSeq ? { ...state, lastSeq: seq } : state;
	state = withSeq;

	switch (event.type) {
		// ── Turn lifecycle ──────────────────────────────────────────────
		case 'turn.started': {
			// Track the live turn id (#585) so a Stop can target THIS turn.
			// Also reset liveModel/liveRequestedModel: the new turn's model is
			// unknown until its own model_info note arrives; without this, a
			// previous turn's reroute banner lingers even after the user aligns
			// their selection. Replay order preserves correctness: a backfill
			// replays turn.started then that turn's model_info.
			//
			// A new turn on top of an exhausted retry card is that card's resume
			// in flight: the button goes quiet ("Retrying…") but does NOT retire.
			// It only retires once a reply actually lands (markRetryRecovered) or
			// the user moves on (supersedeRetryCards) — the provider can take a
			// minute or two to answer, and a resume that fails again must leave
			// the way back on screen. Pure state, so a from_seq=0 replay agrees.
			const arming = (d: Record<string, unknown> | undefined) =>
				!!d?.exhausted && !d.recovered && !d.superseded && !d.resuming;
			let messages = state.messages;
			if (messages.some((m) => arming(retryCardData(m)))) {
				messages = messages.map((m) => {
					const d = retryCardData(m);
					return arming(d) ? { ...m, systemData: { ...d, resuming: true } } : m;
				});
			}
			return {
				...state,
				messages,
				isStreaming: true,
				parked: false,
				liveTurnId: typeof event.turn_id === 'string' ? event.turn_id : state.liveTurnId,
				liveTurnProfileId:
					typeof event.profile_id === 'string' && event.profile_id ? event.profile_id : null,
				pendingPumpAck: null,
				pumpAckBuffer: '',
				liveModel: null,
				liveRequestedModel: null,
				liveProvider: null,
			};
		}

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

		case 'turn.completed': {
			// A resume that reached the end of a turn got its answer.
			state = withRetryRecovered(state);
			// turn.completed fires unconditionally when the pump drains — background
			// work may still remain, in which case the park gate emits turn.parked
			// right after. Speculatively settle any still-'running' subagent card /
			// task summary that missed its terminal event (#609 self-heal); a
			// following turn.parked reverts that settle. See settleStaleBackgroundWork.
			const drained = settleStaleBackgroundWork(
				closeOpen({
					...state,
					isStreaming: false,
					parked: false,
					liveTurnId: null,
					liveTurnProfileId: null,
					pendingPumpAck: null,
					pumpAckBuffer: '',
					usage: obj(event.usage) ?? state.usage,
				})
			);
			// A retry card left mid-count is finished business now — the turn
			// succeeded despite the empty responses. See settleRetryCards.
			const retrySettled = settleRetryCards(drained.messages);
			const completed =
				retrySettled === drained.messages ? drained : { ...drained, messages: retrySettled };
			// SDK 0.2.126: a turn that stopped because it hit the configured
			// max-turns ceiling carries terminal_reason='max_turns'. Surface a
			// neutral (non-error) informational system card so the user knows the
			// response was truncated by the turn limit rather than finishing
			// naturally. Mirrors the turn.aborted error branch's appendMessage
			// mechanism, but with the gray 'info' subtype; systemData.content
			// feeds SystemMessage's default (neutral) renderer. The duplicate
			// guard defends against the same event folding twice on replay.
			if (str(event.terminal_reason) === 'max_turns') {
				const text =
					'Turn limit reached — the response stopped after hitting the ' +
					'configured maximum turns. Send a message to continue.';
				const last = completed.messages[completed.messages.length - 1];
				const duplicate =
					last?.role === 'system' &&
					last.systemSubtype === 'info' &&
					last.content === text;
				if (!duplicate) {
					return appendMessage(completed, {
						id: event.id,
						role: 'system',
						type: 'system',
						content: text,
						systemSubtype: 'info',
						systemData: { content: text },
						timestamp: createdAt || undefined,
					}, seq);
				}
			}
			if (str(event.terminal_reason) === 'safety_limit') {
				// Legacy replay compatibility for turns stored before automatic
				// safety termination was removed. New turns never emit this reason.
				const text =
					'Safety limit reached — this turn was halted before further model or tool ' +
					'spend. Review the warning above, then send a new message to continue.';
				const last = completed.messages[completed.messages.length - 1];
				if (!(last?.role === 'system' && last.content === text)) {
					return appendMessage(completed, {
						id: event.id,
						role: 'system',
						type: 'system',
						content: text,
						systemSubtype: 'warning',
						systemData: { content: text },
						timestamp: createdAt || undefined,
					}, seq);
				}
			}
			return completed;
		}

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
					// A retry card that burned its last attempt IS this failure —
					// the abort is the cap being reached. Fold the error into the
					// card (which then offers Retry) rather than stacking a second
					// error card underneath saying the same thing.
					const retryData =
						last?.role === 'system' && last.systemSubtype === 'provider_retry'
							? (last.systemData as Record<string, unknown> | undefined)
							: undefined;
					const exhausted =
						!!retryData &&
						typeof retryData.attempt === 'number' &&
						typeof retryData.max_attempts === 'number' &&
						retryData.attempt >= retryData.max_attempts;
					if (exhausted) {
						// The freshly spent card owns Retry from here, so it starts
						// clean: whatever resume brought it about is over. Older
						// exhausted cards step aside rather than offering a second,
						// competing button for the same head.
						const folded: Record<string, unknown> = {
							...retryData,
							exhausted: true,
							error: message,
						};
						delete folded.resuming;
						const lastIndex = next.messages.length - 1;
						const messages = next.messages.map((m, i) => {
							if (i === lastIndex) return { ...last, systemData: folded };
							const d = retryCardData(m);
							if (!d?.exhausted || d.recovered || d.superseded) return m;
							return { ...m, systemData: { ...d, superseded: true, resuming: false } };
						});
						return {
							...next,
							messages,
							isStreaming: false,
							parked: false,
							liveTurnId: null,
							liveTurnProfileId: null,
							pendingPumpAck: null,
							pumpAckBuffer: '',
						};
					}
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
			// Every abort that did NOT fold into an exhausted card lands here —
			// an interrupt, or an error that arrived without a retry card at the
			// cap. Any retry note still counting is over either way, so settle it
			// rather than leaving a spinner behind. See settleRetryCards. A resume
			// that died here answered nothing, so its card re-arms Retry.
			return {
				...next,
				messages: clearRetryResuming(
					settleRetryCards(next.messages, next.hasRetryCards),
					next.hasRetryCards
				),
				isStreaming: false,
				parked: false,
				liveTurnId: null,
				liveTurnProfileId: null,
				pendingPumpAck: null,
				pumpAckBuffer: '',
			};
		}

		case 'turn.usage':
			return {
				...state,
				usage: obj(event.usage) ?? state.usage,
				// Only events that actually carry a snapshot move this — the
				// key is omitted from the payload when the engine had none, and
				// a later usage record must not blank the last known breakdown.
				contextUsage: obj(event.context_usage) ?? state.contextUsage,
			};

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

			// A real typed message (wake-turn notifications returned above): the
			// user chose to move on rather than retry, so any spent retry card
			// retires. Whatever this new prompt gets back is not its recovery.
			const superseded = supersedeRetryCards(state.messages, state.hasRetryCards);
			if (superseded !== state.messages) state = { ...state, messages: superseded };

			const commandId = typeof event.command_id === 'string' ? event.command_id : null;
			// Whether a reconcile is even possible, both answered in O(1):
			//   - an OPTIMISTIC message is one the feed controller seeded outside
			//     the reducer, so it has no origin seq — which is exactly when the
			//     message count and the origin-map size disagree.
			//   - a DUPLICATE delivery of this same event (the case the `m.id ===
			//     event.id` branch below keeps idempotent) is one map lookup.
			// Without these, every backfilled user message scanned the whole
			// transcript twice — O(N²) over a replay, for nothing.
			const reconcilable =
				state.messages.length !== state.messageOriginSeq.size ||
				state.messageOriginSeq.has(event.id);
			// 1. Reconcile against an optimistic message with a matching command_id.
			let idx = -1;
			if (commandId && reconcilable) {
				idx = state.messages.findIndex((m) => m.role === 'user' && m.commandId === commandId);
			}
			// 2. Fallback: same content, appended ≤30s before this event.
			if (idx === -1 && reconcilable) {
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
			// Pin the sending agent at creation time: the event's own profile wins,
			// then the optimistic message's (the reconcile branch already knows who
			// sent it), then the live turn's.
			const senderProfileId =
				str(event.profile_id) ||
				(idx >= 0 ? state.messages[idx].profileId : '') ||
				state.liveTurnProfileId ||
				'';
			// The owning turn, straight off the envelope. A message steered into a
			// running turn (turn_recorder::record_steered_user_message) carries THAT
			// turn's id rather than opening one of its own, which is how the
			// transcript tells a mid-turn prompt from a turn-initiating one.
			const turnId = str(event.turn_id);
			const userMsg = (id: string): ChatMessage => ({
				id,
				role: 'user',
				type: 'text',
				content,
				timestamp: stamp || undefined,
				...(turnId ? { turnId } : {}),
				...(commandId ? { commandId } : {}),
				...(senderProfileId ? { profileId: senderProfileId } : {}),
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
					messageOriginSeq: withMessageOrigin(state, keepId, seq),
					pendingPumpAck: null,
					pumpAckBuffer: '',
				};
			}
			return { ...appendMessage(state, userMsg(event.id), seq), pendingPumpAck: null, pumpAckBuffer: '' };
		}

		// ── Assistant text / thinking deltas ────────────────────────────
		case 'assistant.text_delta': {
			// First real token back — a resume that was in flight has recovered.
			state = withRetryRecovered(state);
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
					...(next.liveTurnProfileId ? { profileId: next.liveTurnProfileId } : {}),
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
				...(next.liveTurnProfileId ? { profileId: next.liveTurnProfileId } : {}),
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
			// Thinking counts too: the provider is answering.
			state = withRetryRecovered(state);
			const text = str(event.text);
			let next = closeOpenText(state);
			if (next.openThinkingId) {
				const open = next.messages.find((m) => m.id === next.openThinkingId);
				return {
					...next,
					messages: replaceById(next.messages, next.openThinkingId, {
						content: joinThinking(open?.content ?? '', text),
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
				...(next.liveTurnProfileId ? { profileId: next.liveTurnProfileId } : {}),
			};
			return { ...appendMessage(next, msg, seq), openThinkingId: id };
		}

		// ── Tool cards ──────────────────────────────────────────────────
		case 'tool.use': {
			const toolUseId = str(event.tool_use_id);
			if (!toolUseId) return state;
			// The model acted, so a resume in flight has its answer.
			state = withRetryRecovered(state);
			// A tool use means the model is acting → stop watching for an "ok" ack.
			let next = { ...closeOpen(state), pendingPumpAck: null, pumpAckBuffer: '' };
			const input = obj(event.input);
			const screenshot = str(event.screenshot);
			const existing = findByToolId(next.messages, toolUseId, next.messageOriginSeq);
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
			const deltaToolName = str(event.name);
			if (!toolUseId) return state;
			const existing = findByToolId(state.messages, toolUseId, state.messageOriginSeq);
			if (existing) {
				return {
					...state,
					messages: replaceById(state.messages, existing.id, {
						partialToolInput: (existing.partialToolInput ?? '') + partial,
						// A placeholder opened by an earlier nameless delta gets
						// its label as soon as one arrives. Never overwrites a
						// name already set — tool.use is authoritative.
						...(deltaToolName && !existing.toolName
							? { toolName: deltaToolName }
							: {}),
					}),
				};
			}
			// A background subagent launch emits input_delta for the launch
			// tool_use block but never tool.use/tool.result. Once the subagent
			// card exists for that id (launch tool_use_id == task_id), the card
			// is the rendering surface — do not recreate the orphan placeholder.
			if (findSubagentByTask(state.messages, toolUseId, state.messageOriginSeq)) return state;
			// input_delta before the tool.use envelope: open a placeholder card.
			const next = closeOpen(state);
			const msg: ChatMessage = {
				id: `tooluse-${toolUseId}`,
				role: 'assistant',
				type: 'tool_use',
				content: '',
				// Named from the first fragment when the producer knows it, so
				// the card reads "Write" while the file content streams in
				// rather than sitting anonymous until the call completes.
				toolName: deltaToolName,
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
			const existing = findByToolId(state.messages, toolUseId, state.messageOriginSeq);
			if (!existing) {
				// A Task launch that fails before spawning a worker may surface ONLY
				// as an error/rejection tool.result joined to the card by toolId —
				// without this belt the card spins forever.
				const card = findSubagentByTask(state.messages, toolUseId, state.messageOriginSeq);
				if (!card || card.agentStatus !== 'running') return state;
				const preview = str(event.content_preview);
				let errorText: string | null = event.is_error ? preview : null;
				const trimmed = preview.trim();
				if (trimmed.startsWith('{')) {
					try {
						const parsed = JSON.parse(trimmed);
						if (parsed && typeof parsed === 'object' && parsed.ok === false) {
							errorText = parsed.error != null ? String(parsed.error) : preview;
						}
					} catch {
						/* not JSON — fall back to the is_error verdict above */
					}
				}
				if (errorText === null) return state; // non-failure: settles via its own events
				const children = (card.agentChildren ?? []).map((c) =>
					c.streaming ? { ...c, streaming: false } : c
				);
				const speculativelySettledCardIds = state.speculativelySettledCardIds.includes(card.id)
					? state.speculativelySettledCardIds.filter((c) => c !== card.id)
					: state.speculativelySettledCardIds;
				return {
					...state,
					speculativelySettledCardIds,
					messages: replaceById(state.messages, card.id, {
						agentStatus: 'error',
						agentChildren: children,
						content: errorText,
					}),
				};
			}
			const rawResult = str(event.content_preview);
			// On an API rejection (#612), swap the raw provider error text for a
			// friendly message when we confidently recognize it; otherwise keep the
			// raw preview (conservative — a miss degrades to today's passthrough).
			const toolResult = event.is_error
				? classifyToolRejection(rawResult) ?? rawResult
				: rawResult;
			// A screenshot result (browser/computer-use image block) arrives as a
			// content-addressed artifact hash — surface it on the tool card so the
			// image renders below the call. Keep an existing toolScreenshot (e.g.
			// a backfilled data URI stamped on tool.use) if one is already set.
			const screenshotUrl = existing.toolScreenshot
				? undefined
				: artifactScreenshotUrl(event.artifact_hashes);
			return {
				...state,
				messages: replaceById(state.messages, existing.id, {
					toolResult,
					toolStatus: event.is_error ? 'error' : 'complete',
					streaming: false,
					...(screenshotUrl ? { toolScreenshot: screenshotUrl } : {}),
				}),
			};
		}

		// ── Subagent card (correlated by task_id) ───────────────────────
		case 'subagent.started': {
			const taskId = str(event.task_id);
			if (!taskId) return state;
			let next = closeOpen(state);
			if (findSubagentByTask(next.messages, taskId, next.messageOriginSeq)) return next; // idempotent
			// Reverse ordering of the same fluid dispatch: the worker-keyed card
			// already landed and absorbed THIS launch id, so a late launch-keyed
			// `subagent.started` would duplicate it. The surviving card records the
			// absorption on agentLaunchToolUseId — treat it as the idempotency key.
			if (next.messages.some((m) => m.type === 'subagent' && m.agentLaunchToolUseId === taskId))
				return next;
			// The launch tool_use block emitted only tool.input_delta (never
			// tool.use/tool.result), opening a `tooluse-${taskId}` placeholder
			// that would spin forever. The subagent card is the designed surface
			// for the launch — drop the orphan placeholder now the card exists.
			// Fluid-leg Task workers key task_id on the WORKER session id, so the
			// model's actual launch tool_use_id rides separately as
			// launch_tool_use_id — absorb that block too, or the transcript shows
			// both a Task tool call and the worker card for one dispatch. That id
			// may also already carry a full subagent CARD (the in-stream
			// `subagent.started` keyed on the model's Task tool_use_id fired first
			// on the native leg): absorb it as well — drop it and inherit its
			// children below — so one dispatch renders exactly one card.
			const launchToolUseId = str(event.launch_tool_use_id);
			const absorbedCard = launchToolUseId
				? next.messages.find((m) => m.type === 'subagent' && m.toolId === launchToolUseId)
				: undefined;
			next = {
				...next,
				messages: next.messages.filter(
					(m) =>
						!(
							m.type === 'tool_use' &&
							(m.toolId === taskId || (launchToolUseId && m.toolId === launchToolUseId))
						) && !(absorbedCard && m.id === absorbedCard.id)
				),
			};
			// The absorbed card no longer exists, so a following turn.parked could
			// never revive it — drop its id from the speculative-settle record.
			if (absorbedCard && next.speculativelySettledCardIds.includes(absorbedCard.id)) {
				next = {
					...next,
					speculativelySettledCardIds: next.speculativelySettledCardIds.filter(
						(c) => c !== absorbedCard.id
					),
				};
			}
			const msg: ChatMessage = {
				id: `subagent-${taskId}`,
				role: 'assistant',
				type: 'subagent',
				content: '',
				toolId: taskId,
				agentId: typeof event.agent_id === 'string' ? event.agent_id : undefined,
				agentType: typeof event.agent_type === 'string' ? event.agent_type : undefined,
				// The dispatched agent's display NAME — the card's identity, with
				// agentType (the model slug) demoted to a secondary chip.
				agentName:
					typeof event.agent_name === 'string' && event.agent_name
						? event.agent_name
						: undefined,
				agentDescription: typeof event.description === 'string' ? event.description : undefined,
				agentPrompt: typeof event.prompt_preview === 'string' ? event.prompt_preview : undefined,
				agentStatus: 'running',
				agentChildren: absorbedCard?.agentChildren ?? [],
				agentLaunchToolUseId: launchToolUseId || undefined,
				timestamp: createdAt || undefined,
			};
			return appendMessage(next, msg, seq);
		}

		case 'subagent.progress': {
			const taskId = str(event.task_id);
			const card = findSubagentByTask(state.messages, taskId, state.messageOriginSeq);
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
				const screenshotUrl = artifactScreenshotUrl(payload.artifact_hashes);
				// Merge into the matching tool_use child when identifiable.
				let merged = false;
				if (targetToolId) {
					for (let i = children.length - 1; i >= 0; i--) {
						if (children[i].type === 'tool_use' && children[i].toolId === targetToolId) {
							children[i] = {
								...children[i],
								toolResult: resultText,
								toolStatus: payload.is_error ? 'error' : 'complete',
								...(screenshotUrl ? { toolScreenshot: screenshotUrl } : {}),
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
						...(screenshotUrl ? { toolScreenshot: screenshotUrl } : {}),
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
			const card = findSubagentByTask(state.messages, taskId, state.messageOriginSeq);
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
			// The worker's authoritative token counts, when the feed forwarded
			// them — ProviderAgent worker cards rendered no usage row at all.
			const usage = obj(event.usage);
			return {
				...state,
				speculativelySettledCardIds,
				messages: replaceById(state.messages, card.id, {
					agentStatus,
					agentChildren: children,
					content: str(event.result_preview),
					...(usage ? { agentUsage: usage as ChatMessage['agentUsage'] } : {}),
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
			// The backend stamps the registry kind ("agent" | "shell") onto
			// task.updated frames; fall back to the existing summary's kind, then
			// 'agent' (older logs carry no kind at all).
			const kind: Task['kind'] =
				event.kind === 'shell' || event.kind === 'agent'
					? event.kind
					: (existing?.kind ?? 'agent');
			const summary: Task = {
				id: taskId,
				subject: typeof event.description === 'string' ? event.description : existing?.subject ?? '',
				status,
				kind,
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
					? findSubagentByTask(messages, summary.toolUseId, state.messageOriginSeq)
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
			// `provider_retry` arrives once per attempt while the engine
			// re-sends an empty completion — up to ten of them in a row. They
			// must read as ONE card counting 1 → 2 → 3, not ten stacked
			// cards, so when the newest message is already a retry note the
			// new attempt replaces it in place. The id is reused so
			// messageOriginSeq and transcript rewind keep lining up, and a
			// retry that follows anything else still appends a fresh card.
			// An EXHAUSTED card is finished business — it records a turn that
			// already failed, and its Retry button is the only way back — so a
			// retry note from the NEXT turn appends a fresh card beside it
			// rather than overwriting the failure. A SETTLED card is finished
			// the same way: it records a turn that already ended, so the next
			// turn's retries append beside it instead of rewriting history.
			if (str(event.subtype) === 'provider_retry') {
				const last = state.messages[state.messages.length - 1];
				const lastData = last?.systemData as Record<string, unknown> | undefined;
				const lastFinished = !!(lastData?.exhausted || lastData?.settled);
				if (
					last &&
					last.type === 'system' &&
					last.systemSubtype === 'provider_retry' &&
					!lastFinished
				) {
					const messages = state.messages.slice();
					messages[messages.length - 1] = {
						...last,
						content: str(data?.content ?? data?.text),
						...(data ? { systemData: data } : {}),
						timestamp: stampTs(event, createdAt) || undefined,
					};
					return { ...state, messages };
				}
			}
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
			const keptMessages = own(
				state.messages.filter((m) => {
					const origin = state.messageOriginSeq.get(m.id);
					return origin === undefined || origin < fromSeq;
				})
			);
			const messageOriginSeq = own(new Map<string, number>());
			for (const [id, origin] of state.messageOriginSeq) {
				if (origin < fromSeq) messageOriginSeq.set(id, origin);
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
				hasRetryCards:
					state.hasRetryCards &&
					keptMessages.some((m) => m.systemSubtype === 'provider_retry'),
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
	// Sealed: replay helpers hand the result around freely (tests branch off it),
	// so the returned state must not keep in-place-append ownership.
	return sealState(events.reduce(reduce, from));
}

// Re-export for callers that seed optimistic messages.
export type { ChatMessage, SubagentChildMessage };

/**
 * WebSocket Message Handlers — Notification + control dispatch for incoming WS messages.
 *
 * Architecture (SSE-fed):
 * - The SSE-fed reducer (feedController) owns the session transcript and its
 *   streaming state — it is the sole writer of `messages` and isStreaming for
 *   the active session. These handlers NEVER render stream content.
 * - WS handlers DO manage non-transcript state: permissions, questions, the
 *   cross-session streaming map, git state, session adoption (session_created/
 *   start → start the feed), and fire side-effect callbacks (completion
 *   notifications, error banners, etc).
 * - Session history loads via the feed's from_seq=0 replay, not WS.
 */

import type { PermissionRequest as PermissionRequestType, UserQuestionRequest } from '$lib/api/client';
import type { ActiveSessionState, StreamingSessionInfo } from '../active-session';
import { notify, type NotificationAction, type NotificationSeverity, type NotificationSurface } from '../notifications';
import { rateLimit } from '../rate-limit';
import { reportClientError } from '$lib/services/error-reporter';
import { startFixChat } from '$lib/services/fix-with-ai';
// The SSE-fed reducer store — the transcript authority for a session.
import { feedController, OPTIMISTIC_STREAMING_ON } from './v2/feed-controller';
// Arbiter of which pending ("not seen yet") bubbles survive.
import { pendingQueue, reconcilePendingPeerMessages } from './pending-queue';
import { composerSuggestion } from './composer-suggestion';
import { swarms } from '../swarms';

// ─── Interfaces ──────────────────────────────────────────────────────────────

/**
 * Mutable internal state shared between active-session.ts and the handlers.
 * Simplified: removed guards that were only needed for the dual-authority pattern.
 */
export interface InternalMutableState {
	pendingTools: Set<string>;
	/** True when sendMessage() sent a query for a NEW session (no sessionId yet).
	 *  Used by session_created/start to know whether to accept the sessionId. */
	awaitingNewSession: boolean;
	/** Client-generated id attached to the outgoing new-session `query` frame while
	 *  awaitingNewSession. handle_query echoes it in the `session_created` and first
	 *  `start` frames, and the awaiting-branch adoption predicates accept ONLY when
	 *  the incoming frame's request_id matches. This authenticates adoption so a
	 *  server-initiated turn for ANOTHER session (unpark follow-up / queue drain) —
	 *  which carries NO request_id — can never be adopted during the awaiting
	 *  window on the shared device socket. null when not awaiting a new session. */
	pendingRequestId: string | null;
	/** True when the user clicked Stop on a brand-new session BEFORE the backend
	 *  minted its sessionId (stopGeneration had no id to send, so the stop would
	 *  otherwise be dropped — "Stop fired before the session started"). Consumed
	 *  by the session_created/start handlers: once the id is known they send the
	 *  deferred stop so the backend can discard the orphan (no-output) turn. */
	stopRequestedBeforeSession: boolean;
	/** Monotonic counter incremented on every session switch / new-chat.
	 *  autoCreateWorktree captures the value at start and bails if it changes
	 *  during an await, preventing the async callback from clobbering the
	 *  active session after the user navigated away. */
	worktreeCreationGeneration: number;
	/** AbortController for the currently in-flight autoCreateWorktree request,
	 *  if any. session-switcher aborts this on every session switch / new-chat
	 *  so we don't leak orphan worktrees on the backend when the user navigates
	 *  away mid-creation (e.g. phone lock). */
	worktreeCreateAbort: AbortController | null;
}


/** Per-session outstanding-prompt state behind the sidebar attention indicator. */
export interface SessionAttention {
	/** Outstanding prompts (permissions + questions) for the session. */
	count: number;
	/** Which kind arrived most recently — drives the sidebar card's label. */
	kind: 'permission' | 'question';
}

/**
 * Dependencies injected from the active-session store factory.
 */
export interface MessageHandlerDeps {
	// Store access
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	getState: () => ActiveSessionState;

	// WebSocket
	wsSend: (data: Record<string, unknown>) => boolean;

	// External store access
	streamingSessions: {
		update: (fn: (m: Map<string, StreamingSessionInfo>) => Map<string, StreamingSessionInfo>) => void;
		set: (m: Map<string, StreamingSessionInfo>) => void;
	};
	backgroundPermissions: {
		update: (fn: (m: Map<string, SessionAttention>) => Map<string, SessionAttention>) => void;
	};

	// Callbacks for git
	gitRefreshStatus: () => void;
	gitRefreshDiffStats: () => void;
	/** Optional: refresh session-scoped PR state (merges the git store's throttle). */
	gitRefreshPrState?: () => void;
	gitGetProjectId: () => string | null;
	gitGetLoading: () => boolean;
	// Callbacks for app-level side effects
	appLoadSessions: () => void;
	appLoadApiUserSessions: () => void;
	/** Merge a server-pushed field patch into the sidebar session list without a
	 *  refetch (e.g. is_active from user_session_updated). Returns true when the
	 *  row exists locally and was updated. */
	mergeSessionPatch: (sessionId: string, patch: Record<string, unknown>) => boolean;

	// Internal mutable state (shared reference)
	internal: InternalMutableState;

	// Callbacks for complex side effects that remain in active-session.ts
	onNewChat: () => void;
	onPersistSession: () => void;
	onNotifySessionComplete: (info: {
		sessionId: string;
		sessionTitle: string | null;
		durationSeconds?: number;
		totalCostUsd?: number;
		tokensIn?: number;
		tokensOut?: number;
		model?: string;
	}) => void;
	onNotifyWaitingForResponse: (type: 'permission' | 'question', details?: { toolName?: string; questionCount?: number }) => void;

	// WebSocket connection helpers
	wsIsConnected: () => boolean;

	// Stop timeout management (owned by active-session.ts)
	clearStopTimeout: () => void;
}

// ─── Session-list refresh policy ─────────────────────────────────────────────

/**
 * Trailing-edge debounce for full session-list reloads.
 *
 * Every `done` / `session_updated` / `user_session_*` frame used to trigger an
 * uncapped re-page of the whole session list — ⌈N/100⌉ sequential HTTP
 * round-trips and a full sidebar re-derive per completion event, from every
 * device and every background session, with no coalescing
 * (PERFORMANCE_REVIEW_2026-08 W1). Row-level changes are now merged into the
 * sidebar row directly; a full reload is reserved for changes that reshape the
 * LIST (a session appearing/disappearing, a tag/hidden flip the server filters
 * on) and is coalesced over this window.
 */
export const SESSION_LIST_RELOAD_DEBOUNCE_MS = 700;

/**
 * Fields of a `session_updated` / `user_session_updated` payload that can be
 * merged straight into the local sidebar row. Everything here is displayed
 * from the row itself, so patching is equivalent to refetching it.
 */
const ROW_PATCHABLE_FIELDS: ReadonlySet<string> = new Set([
	'title',
	'is_active',
	'hidden',
	'status',
	'updated_at',
	'branch',
	'worktree_branch',
	'worktree_id',
	'worktree_deleted',
	'degraded',
	'device_name',
	'instance_id',
	'is_remote',
	'turn_count',
	'total_cost_usd',
	'pr_number',
	'pr_url',
	'pr_state',
	'parked_task_kind',
	'has_running_tasks',
	'work_ms',
	'work_started_at',
]);

/**
 * Fields whose change also reshapes the list SERVER-side (membership of the
 * paginated/tag-filtered query), so the row patch is only half the story and an
 * authoritative reload still has to follow — debounced.
 */
const LIST_SHAPE_FIELDS: ReadonlySet<string> = new Set(['hidden', 'tags', 'project_id']);

// ─── Factory ─────────────────────────────────────────────────────────────────

/** A rate_limit frame without a window type is logged once per page load. */
let warnedUntypedRateLimit = false;

export function createMessageHandlers(deps: MessageHandlerDeps): {
	handleMessage: (data: Record<string, unknown>) => void;
} {
	const {
		update, getState, wsSend,
		streamingSessions, backgroundPermissions,
		gitRefreshStatus, gitRefreshDiffStats, gitRefreshPrState, gitGetProjectId, gitGetLoading,
		appLoadSessions, appLoadApiUserSessions, mergeSessionPatch,
		internal,
		onNewChat, onPersistSession,
		onNotifySessionComplete, onNotifyWaitingForResponse,
		clearStopTimeout,
	} = deps;

	// ── Debounced list reloads ───────────────────────────────────────
	// Trailing edge: a burst of lifecycle frames (several sessions finishing,
	// a reconnect backfill) collapses into ONE reload.
	let reloadTimer: ReturnType<typeof setTimeout> | null = null;
	let reloadApiUsers = false;

	function scheduleSessionsReload(alsoApiUsers = false): void {
		if (alsoApiUsers) reloadApiUsers = true;
		if (reloadTimer !== null) return;
		reloadTimer = setTimeout(() => {
			reloadTimer = null;
			const withApiUsers = reloadApiUsers;
			reloadApiUsers = false;
			appLoadSessions();
			if (withApiUsers) appLoadApiUserSessions();
		}, SESSION_LIST_RELOAD_DEBOUNCE_MS);
	}

	/**
	 * Merge a metadata-update payload into the sidebar row instead of refetching
	 * the world (W1). Schedules a debounced reload only when the row is unknown
	 * locally or the change reshapes the list server-side.
	 */
	function applyRowUpdates(sessionId: string | undefined, updates: Record<string, unknown>): void {
		const keys = Object.keys(updates);
		const patch: Record<string, unknown> = {};
		let needsReload = false;
		for (const key of keys) {
			if (ROW_PATCHABLE_FIELDS.has(key)) patch[key] = updates[key];
			else needsReload = true;
			if (LIST_SHAPE_FIELDS.has(key)) needsReload = true;
		}
		const patched =
			!!sessionId && Object.keys(patch).length > 0 && mergeSessionPatch(sessionId, patch);
		// Nothing patchable, or the row isn't loaded here (paged out / brand new):
		// only then is the list itself out of date.
		if (!patched || needsReload) scheduleSessionsReload();
	}

	function mapPermissionRequests(
		requests: PermissionRequestType[],
		sessionId?: string
	): PermissionRequestType[] {
		return requests.map(req => ({
			...req,
			session_id: req.session_id ?? sessionId,
		}));
	}

	function mapUserQuestions(
		questions: UserQuestionRequest[],
		sessionId?: string
	): UserQuestionRequest[] {
		return questions.map(question => ({
			...question,
			session_id: question.session_id ?? sessionId,
		}));
	}

	function setNeedsAttention(
		sessionId: string | null | undefined,
		count: number,
		kind: SessionAttention['kind'] = 'permission'
	): void {
		if (!sessionId) return;
		backgroundPermissions.update(map => {
			const next = new Map(map);
			if (count > 0) next.set(sessionId, { count, kind });
			else next.delete(sessionId);
			return next;
		});
	}

	/** Clear a finished turn's permission dot but keep a question's: a posted
	 *  question stays pending after the turn that asked it ends, and the server
	 *  sends its durable count whenever that changes. */
	function clearTurnAttention(sessionId: string | null | undefined): void {
		if (!sessionId) return;
		backgroundPermissions.update(map => {
			const prev = map.get(sessionId);
			if (!prev || prev.kind === 'question') return map;
			const next = new Map(map);
			next.delete(sessionId);
			return next;
		});
	}

	function bumpNeedsAttention(sessionId: string | undefined, kind: SessionAttention['kind']): void {
		if (!sessionId) return;
		backgroundPermissions.update(map => {
			const next = new Map(map);
			const prev = next.get(sessionId);
			next.set(sessionId, { count: (prev?.count ?? 0) + 1, kind });
			return next;
		});
	}

	// ── Handler: done ────────────────────────────────────────────────

	function handleDone(data: Record<string, unknown>): void {
		const metadata = data.metadata as Record<string, unknown>;
		const sessionId = data.session_id as string;
		// done event — the SSE feed folds the streaming→ended transition into
		// the transcript; this handler only fires the completion notification
		// and cleans cross-session bookkeeping.

		const currentState = getState();
		const isActiveSession = !sessionId || sessionId === currentState.sessionId;
		const doneSessionId = sessionId || currentState.sessionId;

		if (isActiveSession) {
			clearStopTimeout();
			internal.pendingTools.clear();

			// Fire completion notification with metadata from the WS event.
			// State (isStreaming, messages) is owned by the SSE feed.
			const durationSeconds = currentState.streamStartTime
				? (Date.now() - currentState.streamStartTime) / 1000
				: undefined;

			// Runtime done frames carry the outcome after the background-work
			// park gate. Legacy frames without an outcome remain compatible.
			if (data.outcome == null || data.outcome === 'completed') {
				onNotifySessionComplete({
					sessionId: sessionId || '',
					sessionTitle: currentState.title ?? null,
					durationSeconds,
					totalCostUsd: metadata?.total_cost_usd as number | undefined,
					tokensIn: metadata?.tokens_in as number | undefined,
					tokensOut: metadata?.tokens_out as number | undefined,
					model: metadata?.model as string | undefined,
				});
			}

			onPersistSession();
		}

		// A terminal frame describes one turn, not the session's current run.
		// It can arrive after a successor's user_streaming_changed(true) on the
		// other socket. Only the durable run-status channel may clear that map.

		// A finished turn can't still be waiting on a permission. Clear that dot
		// here too, in case the backend's count=0 broadcast was missed (dead
		// socket); otherwise it sticks until the user opens the session. A
		// question dot stays: a posted question outlives the turn that asked it.
		clearTurnAttention(doneSessionId);

		// A completed turn only moves the row's last-activity time (and with it
		// its place in the sidebar order) — patch that in rather than re-paging
		// the entire session list on every `done`, from every device and every
		// background session (W1). A `done` for a session we don't hold locally
		// (paged out, or created elsewhere) does change the list, so that case
		// falls back to a debounced reload.
		if (doneSessionId) {
			const patched = mergeSessionPatch(doneSessionId, { updated_at: new Date().toISOString() });
			if (!patched) scheduleSessionsReload();
		}
	}

	// ── Handler: stopped / interrupted ───────────────────────────────

	function handleStoppedOrInterrupted(data: Record<string, unknown>): void {
		// Route by the EVENT's session_id, not the active session — mirror
		// handleDone. A stopped/interrupted event for a BACKGROUND session must
		// not clobber the ACTIVE session's pendingTools. Pre-fix this handler
		// ignored data.session_id entirely and always mutated whatever session
		// was in view.
		const sessionId = data.session_id as string | undefined;
		const currentState = getState();
		const isActiveSession = !sessionId || sessionId === currentState.sessionId;
		const stoppedSessionId = sessionId || currentState.sessionId;

		if (isActiveSession) {
			clearStopTimeout();
			internal.pendingTools.clear();
		}

		// As with done, a late stop/interrupt acknowledgement must not clear a
		// newer run's user-wide streaming state.

		// A stopped/interrupted turn can't be waiting on a permission — clear a
		// stale permission dot (mirrors handleDone; guards against a missed
		// count=0). A posted question is still pending, so its dot stays.
		clearTurnAttention(stoppedSessionId);
	}

	// ── Main dispatch ────────────────────────────────────────────────

	function handleMessage(data: Record<string, unknown>): void {
		const msgType = data.type as string;
		const eventType = data.event_type as string;

		// Lifecycle events logged only on error path (see 'error' case below)

		const state = getState();
		const eventSessionId = data.session_id as string | undefined;
		if (msgType === 'swarm.updated' || eventType === 'swarm.updated') {
			swarms.receive(data.swarm ?? data.payload, eventSessionId);
			return;
		}
		if (msgType === 'composer_suggestion') {
			composerSuggestion.receive(data);
			return;
		}
		if (['stopped', 'interrupted', 'error'].includes(msgType) &&
			(!eventSessionId || eventSessionId === state.sessionId)) composerSuggestion.invalidate();

		// Handle sync events from other devices / server-originated broadcasts
		if (eventType) {
			// stream_frame: the per-session sync fan-out envelope
			// (session_runtime.py::_default_broadcast / SyncEvent) sent to every
			// OTHER device watching this session over the shared /ws/chat socket
			// (registered via watch_session). It wraps the SAME legacy WS frame the
			// owning device gets un-enveloped on its origin socket (tool_use,
			// permission_request, done, error, ...) under `data`. Unwrap it and feed
			// it back through this same dispatcher so those devices get the same
			// tool-tracking / permission-modal / completion-notification handling
			// instead of the frame being silently dropped. The inner frame carries
			// its own `session_id` (stamped server-side), so every case's existing
			// cross-session routing applies unchanged.
			if (eventType === 'stream_frame') {
				const inner = data.data as Record<string, unknown> | undefined;
				if (inner) handleMessage(inner);
				return;
			}

			// queue_updated is handled here directly since queue state
			// is not part of the poll response — it's server-side only.
			// Scope to the active session: a late/cross-session event (e.g. a
			// stale queue_updated for the session we just switched away from, or
			// the watch_session restore racing the switch) must not clobber the
			// current session's queue.
			if (eventType === 'queue_updated' &&
				(!eventSessionId || eventSessionId === state.sessionId)) {
				const syncData = data.data as Record<string, unknown> | undefined;
				const messages = (syncData?.queued_messages ?? []) as Array<{ id: string; content: string; created_at: string }>;
				const rows = messages.map(m => ({ id: m.id, content: m.content, queuedAt: new Date(m.created_at).getTime() }));
				// The snapshot is authoritative about what EXISTS, but it is not
				// ordered against the other producer of this frame (the turn pump's
				// clearing broadcast), so a pre-absorption snapshot can land last and
				// resurrect an answered bubble. pendingQueue is what remembers.
				const sid = state.sessionId;
				pendingQueue.noteAuthoritative(sid, rows);
				const reconciled = pendingQueue.reconcile(sid, rows);
				// An × on an optimistic id had no server row to name; now that the
				// real one is here, aim the removal at it.
				for (const removeId of pendingQueue.drainRemovalRequests()) {
					wsSend({ type: 'remove_queued_message', message_id: removeId, session_id: sid });
				}
				const peers = (syncData?.pending_peer_messages ?? []) as Array<{
					id: string; content: string; sender_label: string; sender_session_id?: string; created_at: string;
				}>;
				const peerRows = peers.map(m => ({
					id: m.id, content: m.content, senderLabel: m.sender_label,
					senderSessionId: m.sender_session_id || undefined,
					queuedAt: new Date(m.created_at).getTime()
				}));
				update(s => ({
					...s, queuedMessages: reconciled,
					pendingPeerMessages: reconcilePendingPeerMessages(peerRows, s.messages)
				}));
			}

			// session_updated carries metadata changes (e.g. a backfilled title
			// for sessions created without one, like via auto-worktree, or a
			// settled session re-activated by a new send). Apply the title to
			// the active session immediately and refresh the sidebar list so
			// the change is visible everywhere.
			if (eventType === 'session_updated') {
				const syncData = data.data as Record<string, unknown> | undefined;
				const updates = (syncData?.updates ?? {}) as Record<string, unknown>;
				const updatedTitle = typeof updates.title === 'string' ? updates.title : undefined;
				const updatedSessionId = data.session_id as string | undefined;
				if (updatedTitle) {
					const currentState = getState();
					if (updatedSessionId && updatedSessionId === currentState.sessionId) {
						update(s => ({ ...s, title: updatedTitle }));
						onPersistSession();
					}
				}
				// Row-level metadata (title backfill, is_active flip, …) is merged
				// straight into the sidebar row — no refetch, so no chance of a
				// stale paginated response re-flipping the card the user just
				// settled/unsettled, and no reload storm per turn (W1). Only an
				// unknown row or a list-reshaping field falls back to a reload.
				applyRowUpdates(updatedSessionId, updates);
			}

			// message_added from server-side queue processing (_process_next_queued)
			// needs no handling here: the SSE feed delivers the queued user message
			// to the transcript.

			return;
		}

		// ── User-level messages (not per-session) ─────────────────────

		if (msgType === 'tabs_sync') return;

		// Per-install Claude usage snapshot from the backend poller (not tied to
		// the active session). Each install runs a different account, so the
		// payload carries a `snapshots` array (one bucket per install) plus
		// `self_instance_id`; the legacy top-level fields describe the LOCAL
		// install and are folded into a self bucket when `snapshots` is absent.
		// Feeds the usage-limits store that drives the header pill + popover.
		if (msgType === 'usage_snapshot') {
			rateLimit.applySnapshot({
				windows: (data.windows as { type: string; utilization: number; resets_at?: string | null }[]) ?? [],
				fetched_at: (data.fetched_at as number | null) ?? null,
				available: (data.available as boolean) ?? false,
				generation: (data.generation as number | null) ?? undefined,
				snapshots: (data.snapshots as import('$lib/stores/rate-limit').SnapshotInstance[] | undefined) ?? undefined,
				self_instance_id: (data.self_instance_id as string | null | undefined) ?? undefined,
				self_device_id: (data.self_device_id as string | null | undefined) ?? undefined,
			});
			return;
		}

		if (msgType === 'user_session_created') {
			// A genuine list-shape change (a row appeared) — reload, debounced so
			// a burst of creations costs one round-trip, not one each.
			scheduleSessionsReload(true);
			return;
		}

		// A session's list-facing metadata changed without any device watching
		// it — e.g. the voice attach backfill hiding a wake-word "command"
		// session (the list queries exclude hidden rows, so a refetch drops the
		// card that flashed during the create→attach window).
		if (msgType === 'user_session_updated') {
			const updates = (data.updates ?? {}) as Record<string, unknown>;
			const sessionId = data.session_id as string | undefined;
			// Same policy as session_updated: patch the row, and only reload when
			// the row is unknown here or the change reshapes the list server-side
			// (hidden / tags / project move).
			applyRowUpdates(sessionId, updates);
			return;
		}

		// A session's conversation was wiped IN PLACE (POST /sessions/{id}/clear
		// — another device/tab, an API user, or this tab's own request echoed
		// back). The row survives, but its event log was rewritten out-of-band:
		// any cached transcript is now for a log that no longer exists, and a
		// feed resuming at from_seq=lastSeq would match nothing and leave the
		// deleted conversation on screen.
		if (msgType === 'user_session_cleared') {
			const sessionId = data.session_id as string | undefined;
			if (!sessionId) return;
			// R3: drop the cached transcript so a later switch-in replays from 0.
			feedController.invalidate(sessionId);
			// If the cleared session is on screen, re-hydrate from seq 0: the
			// emptied log folds to zero messages and the view collapses to the
			// "Conversation cleared" hero instead of keeping the stale transcript.
			if (feedController.isFeeding(sessionId)) {
				feedController.restart(sessionId);
			}
			// A cleared conversation has nothing left awaiting the user's input.
			setNeedsAttention(sessionId, 0);
			// Sidebar row: the server zeroed the counters as part of the clear —
			// mirror that on the row (all patchable fields) instead of re-paging.
			applyRowUpdates(sessionId, {
				turn_count: 0,
				total_cost_usd: 0,
				updated_at: new Date().toISOString(),
			});
			return;
		}

		// A session row was deleted (counterpart of user_session_created). Drop
		// everything this tab holds for it: the cached transcript (R3), any
		// attention/streaming dot, the sidebar row (via reload — the list's
		// shape changed), and the open view if this tab is sitting on the
		// deleted session (mirrors the session_not_found case).
		if (msgType === 'user_session_deleted') {
			const sessionId = data.session_id as string | undefined;
			if (!sessionId) return;
			feedController.invalidate(sessionId);
			setNeedsAttention(sessionId, 0);
			streamingSessions.update(map => {
				if (!map.has(sessionId)) return map;
				const next = new Map(map);
				next.delete(sessionId);
				return next;
			});
			scheduleSessionsReload();
			if (getState().sessionId === sessionId) {
				onNewChat();
			}
			return;
		}

		// Unified notification funnel: server emitted a user-facing message via
		// app/services/notification_service.emit_notification. Already persisted
		// server-side — feed it to the store for live render + the bell. Actions
		// can't cross the wire as closures, so we rebuild interactive ones
		// (e.g. "Fix with AI") here, keyed on `category`.
		if (msgType === 'notification') {
			const n = data.notification as
				| {
						id: string;
						severity: NotificationSeverity;
						surface?: NotificationSurface;
						category?: string;
						title?: string;
						message: string;
						session_id?: string;
						read?: boolean;
						created_at?: number;
						action?: { kind?: string; payload?: Record<string, unknown> } | null;
				  }
				| undefined;
			if (!n) return;

			let action: NotificationAction | undefined;
			let duration_ms: number | undefined;
			if (n.category === 'api_error') {
				// Mirror the legacy api_error UX: 429 is transient (5s, no fix),
				// other errors get 10s + a one-click "Fix with AI".
				duration_ms = n.severity === 'warning' ? 5000 : 10000;
				if (n.severity !== 'warning') {
					const payload = n.action?.payload ?? {};
					action = {
						label: 'Fix with AI',
						onClick: () =>
							startFixChat({
								source: 'ws',
								message: n.message,
								status_code:
									typeof payload.status_code === 'number' ? payload.status_code : undefined,
								session_id: n.session_id ?? (payload.session_id as string | undefined),
							}),
					};
				}
			}

			// Drop the server-side `{kind, payload}` action descriptor — it can't be
			// rendered directly; the rebuilt closure (if any) is passed separately.
			const { action: _serverAction, ...payload } = n;
			notify.ingest({ ...payload, duration_ms }, action);
			return;
		}

		if (msgType === 'user_streaming_changed') {
			if (!data.session_id) return;
			const ssId = data.session_id as string;
			const isStreamingFlag = data.is_streaming as boolean;
			const projectId = (data.project_id as string | null) ?? null;
			const worktreeId = (data.worktree_id as string | null) ?? null;
			const worktreeBranch = ((data.worktree_branch as string | null) ?? null) || null;
			streamingSessions.update(map => {
				const newMap = new Map(map);
				if (isStreamingFlag) {
					newMap.set(ssId, { projectId, worktreeId, worktreeBranch });
				} else {
					newMap.delete(ssId);
				}
				return newMap;
			});
			return;
		}

		// Durable pending prompts, sent on every chat WS connect. It REPLACES the
		// map: live session_attention frames missed while disconnected would
		// otherwise leave stale dots, and a fresh page load has no other source.
		if (msgType === 'session_attention_snapshot') {
			const sessions = data.sessions as Array<{ session_id?: string; count?: number; kind?: string }> | undefined;
			const next = new Map<string, SessionAttention>();
			for (const s of sessions ?? []) {
				if (!s.session_id || !s.count || s.count <= 0) continue;
				next.set(s.session_id, { count: s.count, kind: s.kind === 'question' ? 'question' : 'permission' });
			}
			backgroundPermissions.update(() => next);
			return;
		}

		if (msgType === 'user_streaming_sessions') {
			const sessions = data.sessions as Array<{ session_id: string; project_id: string | null; worktree_id?: string | null; worktree_branch?: string | null }> | undefined;
			const sessionIds = data.session_ids as string[] | undefined;
			const newMap = new Map<string, StreamingSessionInfo>();
			if (sessions) {
				for (const s of sessions) {
					newMap.set(s.session_id, {
						projectId: s.project_id ?? null,
						worktreeId: s.worktree_id ?? null,
						worktreeBranch: s.worktree_branch ?? null
					});
				}
			} else if (sessionIds) {
				for (const sid of sessionIds) {
					newMap.set(sid, { projectId: null, worktreeId: null });
				}
			}
			streamingSessions.set(newMap);
			return;
		}

		if (msgType === 'user_branch_changed') {
			const projectId = data.project_id as string;
			const gitProjectId = gitGetProjectId();
			if (gitProjectId === projectId && !gitGetLoading()) {
				gitRefreshStatus();
				gitRefreshDiffStats();
				// Also fired after PR create/merge (backend reuses this channel),
				// so re-query PR lifecycle to keep Finish/Create-PR fresh everywhere.
				gitRefreshPrState?.();
			}
			return;
		}

		// ── Per-session message switch ────────────────────────────────

		switch (msgType) {
			// ── Session lifecycle ─────────────────────────────────────
			// These handlers accept the sessionId and start the SSE feed,
			// which owns the transcript and streaming state from there on.

			case 'session_created': {
				if (!data.session_id) break;
				const sessionId = data.session_id as string;
				const title = (data.title as string) || undefined;
				const currentState = getState();
				// Authenticate new-session adoption: while awaiting, accept ONLY
				// when the frame echoes our client-generated request_id. This blocks
				// the session-adoption hijack — a server-initiated turn for another
				// session (unpark follow-up / queue drain) unparking on the shared
				// device socket during the awaiting window carries no request_id, so
				// it can never be adopted here. See `pendingRequestId`.
				const createdRequestId = data.request_id as string | undefined;
				const createdAwaitingMatch = !currentState.sessionId && internal.awaitingNewSession &&
					!!internal.pendingRequestId && createdRequestId === internal.pendingRequestId;
				const acceptCreated = currentState.sessionId === sessionId || createdAwaitingMatch;
				if (acceptCreated) {
					internal.awaitingNewSession = false;
					internal.pendingRequestId = null;
					// Set the sessionId and title so the header shows the correct
					// session title immediately (not stale 'New Chat'). Reset
					// activeTasks to [] on session commit (mirrors PR #448) so a
					// prior/foreign session's running-task strip can't flash on the
					// freshly-adopted session before the feed settles.
					update(s => ({ ...s, sessionId, activeTasks: [], ...(title ? { title } : {}) }));
					// Deliver a Stop the user clicked before the id existed (see
					// stopRequestedBeforeSession). Now that we have the id, the
					// backend can interrupt and discard the no-output orphan turn.
					if (internal.stopRequestedBeforeSession) {
						internal.stopRequestedBeforeSession = false;
						wsSend({ type: 'stop', session_id: sessionId, ...(getState().liveTurnId ? { turn_id: getState().liveTurnId } : {}) });
					}
					// Adopt a freshly-minted session by starting the SSE feed
					// (idempotent — start() no-ops if already feeding this id).
					// The feed's replay renders the turn.
					if (!feedController.isFeeding(sessionId)) {
						feedController.start(sessionId);
					}
					onPersistSession();
				}
				// A new row exists server-side — a real list-shape change, but a
				// debounced one (the create is usually followed by title/start/done
				// frames for the same session).
				scheduleSessionsReload();
				break;
			}

			case 'start': {
				if (!data.session_id) break;
				const sessionId = data.session_id as string;
				const currentState = getState();
				// Authenticate new-session adoption (see session_created): while
				// awaiting, accept the `start` frame ONLY when it echoes our
				// client-generated request_id. A server-initiated turn for a foreign
				// session unparking on the shared device socket carries no request_id
				// and so can never hijack the awaiting new chat. See `pendingRequestId`.
				const startRequestId = data.request_id as string | undefined;
				const startAwaitingMatch = !currentState.sessionId && internal.awaitingNewSession &&
					!!internal.pendingRequestId && startRequestId === internal.pendingRequestId;
				const acceptStart = currentState.sessionId === sessionId || startAwaitingMatch;
				// activeTasks is reset only when this frame actually adopts a NEW
				// sessionId (mirrors PR #448) — a `start` for the already-open session
				// (e.g. a pending message becoming the next turn) must not wipe its
				// live task strip.
				const startAdoptsNewSession = currentState.sessionId !== sessionId;
				if (acceptStart) {
					internal.awaitingNewSession = false;
					internal.pendingRequestId = null;
					// Deliver a Stop deferred before the id existed (see
					// stopRequestedBeforeSession) in case 'start' beat
					// 'session_created' here. The flag self-clears so we never
					// double-send.
					if (internal.stopRequestedBeforeSession) {
						internal.stopRequestedBeforeSession = false;
						wsSend({ type: 'stop', session_id: sessionId, ...(getState().liveTurnId ? { turn_id: getState().liveTurnId } : {}) });
					}
					// The SSE feed owns the transcript and streaming state. Adopt the
					// sessionId and start the feed; the feed's replay renders the turn.
					update(s => ({
						...s,
						sessionId,
						...OPTIMISTIC_STREAMING_ON,
						streamStartTime: Date.now(),
						...(startAdoptsNewSession ? { activeTasks: [] } : {}),
					}));
					if (!feedController.isFeeding(sessionId)) {
						feedController.start(sessionId);
					}
					onPersistSession();
				}
				break;
			}

			// hook_event: the ungated WS append was removed — the SSE feed replays
			// hook events from the session's event log, so appending here would
			// double-render. Folded into the no-op group below.
			case 'hook_event':
			case 'subagent_start':
			case 'subagent_chunk':
			case 'subagent_done':
			case 'plugin_message':
				break;

			case 'stream_message_delta':
				break;

			case 'tool_use': {
				const tuSessionId = data.session_id as string | undefined;
				if (!tuSessionId || tuSessionId !== getState().sessionId) break;
				const toolId = data.id as string;
				if (toolId) internal.pendingTools.add(toolId);
				break;
			}

			case 'tool_result': {
				const trSessionId = data.session_id as string | undefined;
				if (!trSessionId || trSessionId !== getState().sessionId) break;
				const toolUseId = data.tool_use_id as string;
				if (toolUseId) internal.pendingTools.delete(toolUseId);
				break;
			}

			case 'subagent_tool_use': {
				const satuSessionId = data.session_id as string | undefined;
				if (!satuSessionId || satuSessionId !== getState().sessionId) break;
				const toolId = data.id as string;
				if (toolId) internal.pendingTools.add(toolId);
				break;
			}

			case 'subagent_tool_result': {
				const satrSessionId = data.session_id as string | undefined;
				if (!satrSessionId || satrSessionId !== getState().sessionId) break;
				const toolUseId = data.tool_use_id as string;
				if (toolUseId) internal.pendingTools.delete(toolUseId);
				break;
			}

			case 'done':
				handleDone(data);
				break;

			// Acks of the retired two-phase interrupt-and-query handshake. A send
			// during a live turn is a plain `query` now, so nothing waits on them.
			case 'interrupt_confirmed':
			case 'interrupt_failed':
				break;

			case 'stopped':
			case 'interrupted':
				handleStoppedOrInterrupted(data);
				break;

			case 'user_message_discarded': {
				// The user pressed Stop before any assistant output. The backend
				// removed the orphan user message from the live store (so the
				// turn isn't shown as a saved-but-empty exchange) and sent its
				// text back so we can restore it to the input box for editing or
				// resending. Set pendingRestorePrompt (ChatInput watches it and
				// repopulates the editor); the SSE feed drops the now-removed user
				// message from the transcript.
				const discardedSessionId = data.session_id as string | undefined;
				const prompt = typeof data.prompt === 'string' ? data.prompt : '';
				const discardState = getState();
				if (discardedSessionId && discardedSessionId === discardState.sessionId) {
					update(s => ({ ...s, pendingRestorePrompt: prompt }));
				}
				break;
			}

			case 'error': {
				// Route by the EVENT's session_id, not the active session — mirror
				// handleDone / handleStoppedOrInterrupted. A BACKGROUND session's
				// error must never reset the ACTIVE session's isStreaming. The
				// backend now stamps session_id
				// on error events (interactive + headless paths); fall back to the
				// active session only when the id is absent (e.g. a synchronous
				// pre-stream command error like an empty prompt).
				const errSessionId = data.session_id as string | undefined;
				const errState = getState();
				const isActiveError = !errSessionId || errSessionId === errState.sessionId;
				const erroredSessionId = errSessionId || errState.sessionId;
				const errMessage = data.message as string;

				// Error frames (including rejected commands) do not establish that
				// the session has no live run. The run-status channel owns the map.

				// Always log for observability (keyed to the session that errored).
				reportClientError({
					source: 'ws',
					message: errMessage || 'Stream error',
					detail: typeof data.detail === 'string' ? data.detail : undefined,
					session_id: erroredSessionId ?? undefined,
				});

				// Only the in-view session surfaces the banner and resets its
				// streaming flag (so the user can send again).
				if (isActiveError) {
					// `offer_fork`: the worker that owns this session is unreachable —
					// the chat renders a "Fork here" action for POST /network/sessions/{id}/fork-here.
					const forkOffer = data.offer_fork === true && erroredSessionId ? erroredSessionId : null;
					update(s => ({
						...s,
						error: errMessage,
						forkOfferSessionId: forkOffer,
						isStreaming: false,
						optimisticStreaming: false,
						streamStartTime: null,
					}));
				}
				break;
			}

			case 'session_not_found': {
				if (!data.session_id) break;
				const missingId = data.session_id as string;
				console.warn(`[ActiveSession] Session not found: ${missingId}`);
				// The row is gone server-side: a list-shape change, debounced.
				scheduleSessionsReload();
				const currentId = getState().sessionId;
				if (currentId === missingId) {
					update(s => ({ ...s, error: 'This session no longer exists.' }));
					onNewChat();
				}
				break;
			}

			case 'query_queued':
				// Ack that a send landed in the queue instead of starting a turn —
				// the ordinary outcome of sending while a turn is live. The live turn
				// is untouched, so this frame must NOT clear isStreaming or prune the
				// streaming map (`turn.*` sync events own that). The pending bubble
				// itself is driven by the separate `queue_updated` broadcast.
				break;

			case 'message_queued':
			case 'queued_message_processing':
				// Legacy — replaced by queue_updated
				break;

			case 'session_locked': {
				const lockedBy = data.locked_by as string;
				console.warn(`[ActiveSession] Session locked by device ${lockedBy}`);
				update(s => ({
					...s,
					error: 'This session is already open on another device. Click "Take Over" to use it here.',
					sessionLocked: true,
					sessionLockedBy: lockedBy,
				}));
				break;
			}

			case 'session_takeover': {
				const newDevice = data.new_device as string;
				console.warn(`[ActiveSession] Session taken over by device ${newDevice}`);
				update(s => ({
					...s,
					error: 'This session was taken over by another device.',
					sessionLocked: false,
					sessionLockedBy: undefined,
				}));
				break;
			}

			case 'ping': {
				wsSend({ type: 'pong' });
				break;
			}

			case 'api_error': {
				// API-level errors (e.g. Anthropic 429/5xx) are now surfaced through
				// the unified notification funnel: the backend emits a `notification`
				// event (persisted + the bell + every device) which the handler above
				// renders — including the "Fix with AI" action. We no longer toast
				// here (that would double up). We DO still log to the durable
				// error_log for observability (GetAppErrors), keyed off `status`.
				const status = Number(data.status ?? 0);
				const customMessage = typeof data.message === 'string' ? data.message : '';
				let message: string;
				if (status === 429) {
					message = customMessage || 'Rate limited — slow down or wait for the next window.';
				} else if (status >= 500 && status < 600) {
					message = customMessage || `Service error: HTTP ${status}`;
				} else if (status > 0) {
					message = customMessage || `API error: HTTP ${status}`;
				} else {
					message = customMessage || 'An API error occurred.';
				}
				const apiErrSessionId = getState().sessionId ?? undefined;
				reportClientError({
					source: 'ws',
					message,
					status_code: status > 0 ? status : undefined,
					session_id: apiErrSessionId,
				});
				break;
			}

			case 'rate_limit': {
				// Live SDK rate-limit event — refines a single usage window of the
				// EMITTING install's bucket. `instance_id` tags which install it came
				// from (relayed remote-session events carry the worker's id); the
				// store routes by it so an offloaded session's usage lands in the
				// right bucket. ContextUsageBar reads the store reactively.
				// The backend keys every window on the CLI's `rate_limit_type`; a
				// frame without one names no window, so it is dropped (never filed
				// under a made-up "unknown" row).
				const rateLimitType = data.rate_limit_type;
				if (typeof rateLimitType !== 'string' || !rateLimitType) {
					if (!warnedUntypedRateLimit) {
						warnedUntypedRateLimit = true;
						console.warn('[ws] dropping rate_limit frame without a window type', data);
					}
					break;
				}
				rateLimit.applyLiveEvent({
					status: typeof data.status === 'string' ? data.status : undefined,
					rate_limit_type: rateLimitType,
					resets_at: data.resets_at as string | undefined,
					utilization: data.utilization as number | undefined,
					session_id: data.session_id as string | undefined,
					instance_id: (data.instance_id as string | null | undefined) ?? undefined,
				});
				break;
			}

			case 'heartbeat': {
				const pendingPermCount = data.pending_permissions as number | undefined;
				const pendingQCount = data.pending_questions as number | undefined;
				if (pendingPermCount && pendingPermCount > 0) {
					const cs = getState();
					if (!cs.pendingPermissions || cs.pendingPermissions.length === 0) {
						wsSend({ type: 'get_pending_permissions' });
					}
				}
				if (pendingQCount && pendingQCount > 0) {
					const cs = getState();
					if (!cs.pendingQuestions || cs.pendingQuestions.length === 0) {
						wsSend({ type: 'get_pending_questions' });
					}
				}
				break;
			}

			case 'permission_request': {
				const requestSessionId = data.session_id as string | undefined;
				const currentSessionId = getState().sessionId;
				if (requestSessionId && requestSessionId !== currentSessionId) {
					bumpNeedsAttention(requestSessionId, 'permission');
					break;
				}
				const request = {
					session_id: requestSessionId ?? currentSessionId ?? undefined,
					request_id: data.request_id as string,
					tool_name: data.tool_name as string,
					tool_input: data.tool_input as Record<string, unknown>,
					queue_position: data.queue_position as number | undefined,
					queue_total: data.queue_total as number | undefined,
					// Slice D enrichment (SDK 0.1.81+) — forwarded only when present.
					title: data.title as string | undefined,
					display_name: data.display_name as string | undefined,
					description: data.description as string | undefined,
					decision_reason: data.decision_reason as string | undefined,
					blocked_path: data.blocked_path as string | undefined,
				} as PermissionRequestType;

				// The same prompt now arrives twice by design: once on the origin
				// socket that owns the query and once on the user-wide device
				// fan-out (both legs are best-effort, so neither can be dropped).
				// Ignore the echo — re-appending would double the modal queue and
				// fire a second "waiting for response" notification.
				if (getState().pendingPermissions.some(p => p.request_id === request.request_id)) {
					break;
				}

				setNeedsAttention(request.session_id, 0);
				onNotifyWaitingForResponse('permission', { toolName: request.tool_name });
				update(s => ({
					...s,
					pendingPermissions: [...s.pendingPermissions, request],
					// Slice B: if this request was previously deferred and is now
					// being re-broadcast (after `requeue_deferred_permissions`),
					// drop it from the deferred queue to avoid duplicates.
					deferredPermissions: s.deferredPermissions.filter(
						p => p.request_id !== request.request_id
					),
				}));
				break;
			}

			case 'permission_queue_update': {
				const queueSessionId = data.session_id as string | undefined;
				if (queueSessionId && queueSessionId !== getState().sessionId) {
					const remainingCount = data.remaining_count as number | undefined;
					if (remainingCount !== undefined) setNeedsAttention(queueSessionId, remainingCount);
					break;
				}
				// The requeue reply names `requeued_ids` and carries no `resolved_ids`;
				// the requeued requests arrive again as permission_request frames.
				const resolvedIds = Array.isArray(data.resolved_ids) ? data.resolved_ids as string[] : [];
				if (resolvedIds.length === 0) break;
				update(s => ({
					...s,
					pendingPermissions: s.pendingPermissions.filter(p => !resolvedIds.includes(p.request_id)),
				}));
				break;
			}

			case 'permission_response_ack':
			case 'permission_timeout': {
				const responseSessionId = data.session_id as string | undefined;
				if (responseSessionId && responseSessionId !== getState().sessionId) break;
				const requestId = data.request_id as string;
				update(s => ({
					...s,
					pendingPermissions: s.pendingPermissions.filter(p => p.request_id !== requestId),
				}));
				break;
			}

			case 'pending_permissions': {
				// `session_id: null` is the server's empty reply for a socket with no
				// session selected (no load_session / watch_session yet). It says
				// nothing about the active session, so it must not clear its modal.
				if (data.session_id === null) break;
				const pendingSessionId = data.session_id as string | undefined;
				// A cross-install relay-read failure replies with `stale: true` and a
				// non-authoritative (local, empty) list. Replacing pendingPermissions /
				// deferredPermissions with it would wipe an open permission modal on
				// every reconnect/switch resync. Leave state untouched; a later
				// authoritative reply (no marker) reconciles. A successful relay, even
				// with zero items, is authoritative and omits the marker so it flows
				// through below.
				if (data.stale === true) {
					console.debug('[ws] ignoring stale pending_permissions (relay to owner failed)');
					break;
				}
				// Some backends spell the active-queue field `requests`, others
				// (per SDK 0.1.81 contract) ship it under `pending`. Accept both.
				const activeRaw =
					(data.requests as PermissionRequestType[] | undefined) ??
					(data.pending as PermissionRequestType[] | undefined) ??
					[];
				const requests = mapPermissionRequests(activeRaw, pendingSessionId);
				// Slice B: a fresh-server (SDK 0.1.81+) also reports deferred items.
				const deferredRaw = (data.deferred as PermissionRequestType[] | undefined) ?? [];
				const deferred = mapPermissionRequests(deferredRaw, pendingSessionId);

				if (pendingSessionId && pendingSessionId !== getState().sessionId) {
					setNeedsAttention(pendingSessionId, requests.length, 'permission');
					break;
				}
				setNeedsAttention(pendingSessionId, 0);
				update(s => ({
					...s,
					pendingPermissions: requests,
					deferredPermissions: deferred,
				}));
				break;
			}

			case 'user_question': {
				const questionSessionId = data.session_id as string | undefined;
				const currentSessionId = getState().sessionId;
				if (questionSessionId && questionSessionId !== currentSessionId) {
					bumpNeedsAttention(questionSessionId, 'question');
					break;
				}
				const rawQuestions = data.questions as Array<Record<string, unknown>>;
				const normalizedQuestions = rawQuestions.map((q, idx) => ({
					question: q.question as string || '',
					header: q.header as string || `Question ${idx + 1}`,
					options: Array.isArray(q.options) ? q.options.map((opt: unknown) => ({
						label: typeof opt === 'object' && opt !== null ? (opt as Record<string, unknown>).label as string || '' : String(opt),
						description: typeof opt === 'object' && opt !== null ? (opt as Record<string, unknown>).description as string || '' : '',
					})) : [],
					multiSelect: Boolean(q.multiSelect),
				}));

				const request = {
					session_id: questionSessionId ?? currentSessionId ?? undefined,
					request_id: data.request_id as string,
					tool_use_id: data.tool_use_id as string,
					questions: normalizedQuestions,
					nonblocking: data.nonblocking === true,
				} as UserQuestionRequest;

				setNeedsAttention(request.session_id, 0);
				onNotifyWaitingForResponse('question', { questionCount: request.questions.length });
				// Dedup by request_id: a reconnect resync (get_pending_questions ->
				// pending_questions) can race with this live event, and without this
				// guard the same question would render twice.
				update(s => s.pendingQuestions.some(q => q.request_id === request.request_id)
					? s
					: { ...s, pendingQuestions: [...s.pendingQuestions, request] });
				break;
			}

			case 'user_question_response_ack':
			case 'user_question_timeout': {
				const responseSessionId = data.session_id as string | undefined;
				if (responseSessionId && responseSessionId !== getState().sessionId) break;
				const requestId = data.request_id as string;
				// A failed ack carries WHY it failed. Only a retryable delivery
				// failure (`reason: 'relay_offline'` — owner install unreachable)
				// keeps the modal up so the user's in-progress answer isn't lost.
				// Every other failure (`not_pending`, or a legacy ack without a
				// reason) means the owner authoritatively has no such pending
				// question — the modal is stale and must be dismissed, matching
				// pre-relay behavior. The shared `user_question_timeout` type
				// carries NO success field, so it also removes unconditionally.
				if (
					data.type === 'user_question_response_ack' &&
					data.success === false &&
					data.reason === 'relay_offline'
				) {
					notify.error(
						"Couldn't deliver your answer — the owning device is unreachable. Try again.",
						{ category: 'user-question-relay-error', session_id: responseSessionId }
					);
					break;
				}
				update(s => ({
					...s,
					pendingQuestions: s.pendingQuestions.filter(q => q.request_id !== requestId),
				}));
				break;
			}

			case 'pending_questions': {
				// See pending_permissions: a null session_id is the no-session reply.
				if (data.session_id === null) break;
				const pendingSessionId = data.session_id as string | undefined;
				// A cross-install relay-read failure replies with `stale: true` and a
				// non-authoritative (local, empty) list. Replacing pendingQuestions with
				// it would wipe a locally-open question modal (losing in-progress
				// answers) on every reconnect/switch resync. Leave state untouched; a
				// later authoritative reply (no marker) reconciles. A successful relay,
				// even with zero questions, is authoritative and still clears stale
				// modals — that omits the marker so it flows through below.
				if (data.stale === true) {
					console.debug('[ws] ignoring stale pending_questions (relay to owner failed)');
					break;
				}
				const questions = mapUserQuestions(
					(data.questions as UserQuestionRequest[] | undefined) ?? [],
					pendingSessionId
				);
				if (pendingSessionId && pendingSessionId !== getState().sessionId) {
					setNeedsAttention(pendingSessionId, questions.length, 'question');
					break;
				}
				setNeedsAttention(pendingSessionId, 0);
				update(s => {
					const existingIds = new Set(s.pendingQuestions.map(q => q.request_id));
					const newIds = new Set(questions.map(q => q.request_id));
					const same = existingIds.size === newIds.size && [...existingIds].every(id => newIds.has(id));
					if (same) return s;
					return { ...s, pendingQuestions: questions };
				});
				break;
			}

			case 'session_attention': {
				// User-level broadcast: a (possibly background) session is awaiting
				// the user's input. Reaches every one of the user's devices, so the
				// sessions board lights up regardless of which tab owns the query.
				const attnSessionId = data.session_id as string | undefined;
				const count = (data.count as number | undefined) ?? 0;
				const kindRaw = data.kind as string | undefined;
				const kind: SessionAttention['kind'] = kindRaw === 'question' ? 'question' : 'permission';
				// The active session surfaces the prompt through its own modal, not a
				// background dot. But session_attention is the ONE signal that always
				// reaches us: the per-socket user_question/permission_request event is
				// lost whenever its origin socket died (reconnect mid-turn rebinds
				// broadcast_func nowhere), the turn is headless, or it originated on
				// another device/install. So rather than blindly ignore it, use it as a
				// resync trigger — ask the server to replay the pending queues for the
				// active session. The pending_permissions/pending_questions replies
				// REPLACE by request_id, so re-requesting never double-shows a modal.
				if (count > 0 && attnSessionId === getState().sessionId) {
					const cs = getState();
					if (!cs.pendingPermissions || cs.pendingPermissions.length === 0) {
						wsSend({ type: 'get_pending_permissions' });
					}
					if (!cs.pendingQuestions || cs.pendingQuestions.length === 0) {
						wsSend({ type: 'get_pending_questions' });
					}
					break;
				}
				setNeedsAttention(attnSessionId, count, kind);
				break;
			}

			default:
				break;
		}
	}

	return { handleMessage };
}

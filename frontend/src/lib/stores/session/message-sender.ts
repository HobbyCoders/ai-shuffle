/**
 * Message Sender — Send message flow, worktree creation, stop generation, and
 * removal of messages the model hasn't seen yet.
 *
 * Plain .ts file — no $state/$derived/$effect runes (see KNOWN_ISSUES.md Root Cause 10).
 */

import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import type { ExecutionMode } from '../types';
import { api } from '$lib/api/client';
import { isCallActiveFor, sendVoiceText } from '$lib/services/voiceCall';
import { persistVoiceContext, voiceContextFor } from '$lib/services/voiceExecutionContext';
import { STREAM_DEBUG } from '$lib/utils/debug-log';
import { generateBranchName, generateBranchNameLocal } from '$lib/api/git';
import { ATTACHMENTS_NEED_LOCAL_CHECKOUT } from '$lib/components/chat/attach-disable';
// V2 Phase 1: SSE-fed reducer — the transcript authority for a session.
import { feedController, OPTIMISTIC_STREAMING_ON } from './v2/feed-controller';
// Arbiter of which pending ("not seen yet") bubbles survive.
import { pendingQueue, newLocalPendingId } from './pending-queue';
import { contextWindowForChoice } from '$lib/constants/model-context';
import { getDefaultModel } from '$lib/constants/models';

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatWorktreeError(rawError: string, branchName: string): string {
	const lower = rawError.toLowerCase();

	// Git-specific errors
	if (lower.includes('already checked out') || lower.includes('is already used by worktree')) {
		return `Branch '${branchName}' already has an active worktree. Select it from Active Worktrees instead.`;
	}
	if (lower.includes('not a valid branch') || lower.includes('branch not found') ||
		lower.includes('unknown revision') || (lower.includes('pathspec') && lower.includes('did not match'))) {
		return `Branch '${branchName}' doesn't exist. Fetch from remote first or create a new branch.`;
	}
	if (lower.includes('already exists') && (lower.includes('path') || lower.includes('folder') || lower.includes('directory'))) {
		return `A folder already exists at the worktree path for '${branchName}'. Try a different branch name.`;
	}
	if (lower.includes('.lock') || lower.includes('unable to lock') || lower.includes('index.lock')) {
		return 'A git operation is in progress. Wait a moment and try again.';
	}
	if (lower.includes('permission denied') || lower.includes('access denied')) {
		return 'Permission denied creating worktree. Check folder permissions on the server.';
	}
	if (lower.includes('not a git repository')) {
		return 'The project directory is not a git repository. Initialize git first.';
	}
	if (lower.includes('dirty') || lower.includes('uncommitted') || lower.includes('unstaged')) {
		return 'Uncommitted changes detected. Commit or stash your changes before creating a worktree.';
	}

	// Network / HTTP errors
	if (lower.includes('could not resolve') || lower.includes('connection refused') ||
		lower.includes('failed to fetch') || lower.includes('networkerror')) {
		return 'Network error — cannot reach the server. Check your connection.';
	}
	if (lower.includes('timeout') || lower.includes('aborted')) {
		return 'Request timed out. The server may be overloaded — try again.';
	}

	// HTTP status-based errors from the API client
	if (lower.includes('bad request')) {
		return `Invalid worktree request for '${branchName}'. Check the branch name and try again.`;
	}
	if (lower.includes('authentication required') || lower.includes('permission denied')) {
		return 'Authentication error. Try refreshing the page and logging in again.';
	}
	if (lower.includes('not found') && lower.includes('resource')) {
		return 'Project not found. It may have been deleted or moved.';
	}
	if (lower.includes('internal server error') || lower.includes('server error')) {
		return `Server error creating worktree for '${branchName}'. Check the backend logs.`;
	}

	// Fallback with the raw error for debugging
	return `Failed to create worktree for '${branchName}': ${rawError}`;
}

// ─── Interfaces ──────────────────────────────────────────────────────────────

export interface MessageSenderDeps {
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	getState: () => ActiveSessionState;
	internal: InternalMutableState;

	// WebSocket
	wsConn: {
		isConnected: () => boolean;
		connect: () => void;
		send: (data: Record<string, unknown>) => boolean;
		resetLastMessageTime: () => void;
		resetReconnectAttempts: () => void;
	};

	// External modules
	sessionEphemeralCache: {
		remove: (key: string) => void;
	};
	appData: {
		loadSessions: () => void;
		loadApiUserSessions: () => void;
	};

	persistSession: () => void;

	// Stop timeout management
	stopTimeout: {
		get: () => ReturnType<typeof setTimeout> | null;
		set: (t: ReturnType<typeof setTimeout> | null) => void;
	};

	/** Reference to the activeSession object for recursive calls
	 *  (sendMessage calls itself after worktree creation). */
	getActiveSession: () => {
		sendMessage: (prompt: string, options?: SendMessageOptions) => Promise<boolean>;
		createWorktree: (prompt?: string, pendingUploadPaths?: string[]) => Promise<void>;
		autoCreateWorktree: (prompt: string, pendingUploadPaths?: string[]) => Promise<void>;
	};
}

interface SendMessageOptions {
	skipWorktreeSetup?: boolean;
	skipMessageAppend?: boolean;
	pendingUploadPaths?: string[];
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createMessageSender(deps: MessageSenderDeps) {
	const {
		update, getState, internal, wsConn,
		sessionEphemeralCache,
		appData, persistSession, stopTimeout, getActiveSession,
	} = deps;

	// ─── Internal helper for worktree existing branch sessions ──────

	async function startSessionOnExistingWorktree(prompt: string, pendingUploadPaths?: string[]) {
		const _state = getState();
		if (!_state.profile) { update(s => ({ ...s, error: 'Please select a profile' })); return; }
		if (!_state.project) { update(s => ({ ...s, error: 'Please select a project' })); return; }
		if (!_state.worktreeId) { update(s => ({ ...s, error: 'Please select an existing worktree' })); return; }

		// The plain WS query path carries both the device pick and the upload
		// paths, so an existing-worktree session composes with a device choice
		// exactly like any other send.
		await getActiveSession().sendMessage(prompt, {
			skipWorktreeSetup: true,
			pendingUploadPaths,
		});
	}

	// ─── Build overrides from state ─────────────────────────────────

	/**
	 * The picker renders the registry default when no explicit model binding has
	 * been saved yet. Context is selected against that visible model, so resolve
	 * the same effective model here; otherwise choosing 200k on a fresh compose
	 * would silently produce no override.
	 */
	function contextWindowOverride(state: ActiveSessionState): number | null {
		return contextWindowForChoice(
			state.modelOverride || getDefaultModel(),
			state.contextWindowChoice
		);
	}

	function buildOverrides(): Record<string, string | string[] | number | boolean> | undefined {
		const _state = getState();
		const overrides: Record<string, string | string[] | number | boolean> = {};
		if (_state.mode !== 'chat') overrides.workspace_project_ids = [...(_state.workspaceProjectIds ?? [])];
		if (_state.modelOverride) overrides.model = _state.modelOverride;
		if (_state.permissionModeOverride) overrides.permission_mode = _state.permissionModeOverride;
		if (_state.effortLevelOverride) overrides.effort = _state.effortLevelOverride;
		// Always an explicit boolean. Omitting the key when fast mode is OFF made
		// the backend fall back to the session row, which still held the earlier
		// `true` — so turning fast mode off never reached the CLI. `false` is a
		// real pick and has to travel.
		overrides.fast = _state.fastMode;
		const contextWindow = contextWindowOverride(_state);
		if (contextWindow != null) overrides.context_window = contextWindow;
		return Object.keys(overrides).length > 0 ? overrides : undefined;
	}

	// ─── Wait for WS connection ─────────────────────────────────────

	function waitForConnection(timeoutMs = 3000): Promise<boolean> {
		return new Promise<boolean>((resolve) => {
			if (wsConn.isConnected()) { resolve(true); return; }
			const timeout = setTimeout(() => { clearInterval(check); resolve(false); }, timeoutMs);
			const check = setInterval(() => {
				if (wsConn.isConnected()) { clearTimeout(timeout); clearInterval(check); resolve(true); }
			}, 100);
		});
	}

	async function waitForConnectionVoid(timeoutMs = 3000): Promise<void> {
		await waitForConnection(timeoutMs);
	}

	// ─── Wait for a persisted-session restore to settle ─────────────

	function waitForRestore(timeoutMs = 5000): Promise<boolean> {
		return new Promise<boolean>((resolve) => {
			if (!getState().isRestoringSession) { resolve(true); return; }
			const timeout = setTimeout(() => { clearInterval(check); resolve(false); }, timeoutMs);
			const check = setInterval(() => {
				if (!getState().isRestoringSession) { clearTimeout(timeout); clearInterval(check); resolve(true); }
			}, 100);
		});
	}

	return {
		async sendMessage(prompt: string, options?: SendMessageOptions): Promise<boolean> {
			prompt = prompt.trim();

			// When the send began. An optimistic pending bubble is only worth
			// appending while the server has said nothing about the queue since this
			// moment — see the guard on `optimisticQueuedId` below.
			const dispatchedAt = Date.now();

			// Restore-race guard: right after a page load, init() may have adopted
			// a persisted sessionId whose history hasn't rendered yet — the UI
			// looks like "New Chat" but a send here would silently continue the
			// old session. Wait for the restore to settle (history fetched, or
			// navigation cancelled it) BEFORE snapshotting state below, so the
			// send targets what the user actually sees.
			if (getState().isRestoringSession) {
				const settled = await waitForRestore();
				if (!settled) {
					update(s => ({ ...s, error: 'Still loading your previous session — please try again in a moment.' }));
					return false;
				}
			}

			const _state = getState();
			if (_state.sessionId && isCallActiveFor(_state.sessionId)) {
				try {
					await persistVoiceContext(_state.sessionId, voiceContextFor(_state));
				} catch {
					update(s => s.sessionId === _state.sessionId ? { ...s, error: 'Could not save voice execution settings. Your message has been kept.' } : s);
					return false;
				}
				const paths = options?.pendingUploadPaths;
				if (paths?.length) {
					prompt += '\n\nAttached file references (ask a worker to inspect these files):\n' + paths.map(path => JSON.stringify(path)).join('\n');
				}
				const sent = sendVoiceText(_state.sessionId, prompt);
				if (!sent) update(s => s.sessionId === _state.sessionId ? { ...s, error: 'Voice call is not ready. Keep your message and try again when connected.' } : s);
				return sent;
			}

			// A first send from an unbound compose spends the new-chat compose
			// state. Invalidate the shared '__new_chat__' ephemeral entry here —
			// BEFORE the worktree-setup branch below — so every creation path
			// (plain query, createWorktree, autoCreateWorktree) clears it. The
			// worktree branches used to leave the entry behind, and the next
			// New Chat restored the previous chat's prompt into the composer.
			if (!_state.sessionId) {
				sessionEphemeralCache.remove('__new_chat__');
			}

			// Worktree sessions never block on send: the backend resolves/validates
			// the cwd on the query path (worktreeDeleted is a display-only badge).

			// Worktree setup on first message
			if (!options?.skipWorktreeSetup && _state.worktreeIntent === 'worktree' && !_state.sessionId && _state.messages.length === 0) {
				if (_state.worktreeMode === 'existing' && _state.worktreeId) {
					await startSessionOnExistingWorktree(prompt, options?.pendingUploadPaths);
					return true;
				} else if (_state.worktreeBranch) {
					await getActiveSession().createWorktree(prompt, options?.pendingUploadPaths);
					return true;
				} else {
					await getActiveSession().autoCreateWorktree(prompt, options?.pendingUploadPaths);
					return true;
				}
			}

			// Backstop: a worktree-intended NEW session must never fall through to
			// the plain WS query path below — that path carries no worktree_id, so
			// the backend would create the session in the project's MAIN checkout.
			// We only reach here (instead of the worktree-setup block above) when
			// worktreeIntent is 'worktree' with no sessionId yet but a message is
			// already present — i.e. autoCreateWorktree/createWorktree is still in
			// flight (e.g. a second send slipped in during the multi-second
			// auto-name step). Refuse rather than silently run in main; the
			// in-flight creation owns the first message.
			if (!options?.skipWorktreeSetup && _state.worktreeIntent === 'worktree' && !_state.sessionId) {
				console.warn('[ActiveSession] Suppressed send to main: worktree creation still in flight');
				update(s => ({ ...s, worktreeError: 'Still setting up the worktree — please wait a moment and try again.' }));
				return false;
			}

			if (!wsConn.isConnected()) {
				wsConn.resetReconnectAttempts();
				wsConn.connect();
				const connected = await waitForConnection();
				if (!connected) {
					update(s => ({ ...s, error: 'Unable to connect. Please check your connection and try again.' }));
					return false;
				}
			}

			const userMsgId = `user-${Date.now()}`;
			const isFirstMessage = _state.title === 'New Chat';
			const newTitle = isFirstMessage
				? prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '')
				: _state.title;
			const queryProfileId = _state.profile;

			// Sending during a live turn is a plain `query`: the turn keeps running
			// and the backend decides when the model sees the new prompt (mid-turn
			// steer, or the start of the next turn). Until it lands, the message
			// shows as a pending bubble fed by `queuedMessages` — so the optimistic
			// append below goes there instead of onto the transcript.
			const sendWhileStreaming = !!(_state.isStreaming && _state.sessionId);

			// Guard against the "stray new session" bug: a chat that was already
			// OPENED (history/messages present) but whose sessionId went missing —
			// e.g. a WS reconnect race nulled it after a remote session loaded —
			// must NOT fall through to new-session creation below. Doing so silently
			// spawns a duplicate local session from the user's prompt (the reported
			// stray "hello" session). awaitingNewSession is the legitimate
			// mid-creation signal for a brand-new chat; if it's false yet we have
			// messages and no id, the id was lost — refuse and ask the user to
			// reopen. A genuinely empty new chat (messages.length === 0) is
			// unaffected and still mints a session normally.
			if (!_state.sessionId && !internal.awaitingNewSession
				&& !options?.skipMessageAppend && _state.messages.length > 0) {
				console.error('[ActiveSession] Refusing to send: session id missing but messages present (open-session id race)');
				update(s => ({ ...s, error: 'Lost the connection to this session — please reopen it from the sidebar and try again.' }));
				return false;
			}

			// awaitingNewSession means "we sent a prompt with no session id yet and
			// are waiting for the backend to mint one" (consumed as
			// `!state.sessionId && awaitingNewSession` in ws-message-handlers and
			// session_created/start acceptance). It must only be set when there is
			// NO session id — setting it on the 2nd+ send of an EXISTING session
			// left a stale flag that forced shouldNotify=true for every event and
			// muddied session-routing. Gate it on the absence of a session id.
			// Client-generated adoption token for the outgoing new-session query.
			// Only the awaiting (no-sessionId) path needs it: the backend echoes it
			// in session_created + the first start frame, and the frontend adopts
			// ONLY on a match — so a foreign session's server-initiated turn
			// (unpark / queue drain, which carries no request_id) can't be adopted
			// during the awaiting window on the shared device socket. null otherwise.
			let requestId: string | null = null;
			if (!_state.sessionId) {
				internal.awaitingNewSession = true;
				requestId = `req-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
				internal.pendingRequestId = requestId;
			}
			console.debug(`[ActiveSession] sendMessage: sessionId=${_state.sessionId}, awaitingNewSession=${internal.awaitingNewSession}, wsConnected=${wsConn.isConnected()}, sendWhileStreaming=${sendWhileStreaming}`);

			// Optimistic echo, in whichever place the message actually belongs.
			// While a turn is live the prompt is NOT part of the transcript yet, so
			// it goes onto `queuedMessages` and renders as a pending bubble; the
			// server's `queue_updated` replaces this local row, and pendingQueue
			// retires it once the transcript answers it.
			//
			// Skipped when an authoritative `queue_updated` for this session already
			// landed since the send began: the server has described the queue for
			// this window, so a local row added on top of it is a second copy that
			// no later frame is guaranteed to replace.
			const optimisticQueuedId = sendWhileStreaming && !options?.skipMessageAppend
				&& !pendingQueue.authoritySpokeSince(_state.sessionId, dispatchedAt)
				? newLocalPendingId()
				: null;

			// Single atomic update — avoids intermediate render where streaming
			// is cleared but the new user message hasn't been appended yet.
			if (options?.skipMessageAppend) {
				update(s => ({
					...s,
					// S6: the feed reducer owns isStreaming; this is the declared
					// optimistic flip, which the fold ORs in and clears on
					// `turn.started` instead of fighting with it.
					...OPTIMISTIC_STREAMING_ON,
					error: null,
					title: newTitle,
					currentQueryProfileId: queryProfileId,
				}));
			} else if (optimisticQueuedId) {
				pendingQueue.noteOptimistic(_state.sessionId, optimisticQueuedId);
				update(s => ({
					...s,
					queuedMessages: [...s.queuedMessages, {
						id: optimisticQueuedId,
						content: prompt,
						queuedAt: Date.now(),
					}],
					error: null,
				}));
			} else {
				update(s => {
					const baseMessages = s.messages;
					// eslint-disable-next-line no-console
					if (STREAM_DEBUG) console.log(`[AS] optimistic append A id=${userMsgId} prevLen=${baseMessages.length} "${prompt.slice(0, 30)}"`);
					return {
						...s,
						messages: [...baseMessages, {
							id: userMsgId,
							role: 'user' as const,
							content: prompt,
							profileId: queryProfileId,
							// V2 Phase 1: stamp the client-generated adoption token as the
							// commandId so the SSE reducer can reconcile the eventual
							// `message.user` event onto THIS optimistic row (in place, no
							// remount) instead of appending a duplicate. Only set for a
							// new-session query (requestId is null for existing sessions —
							// the reducer then falls back to content+recency matching).
							commandId: requestId || undefined,
							timestamp: new Date().toISOString(),
						}],
						// S6: declared optimistic flip (see the skipMessageAppend
						// branch above) — not a competing write of isStreaming.
						...OPTIMISTIC_STREAMING_ON,
						error: null,
						title: newTitle,
						currentQueryProfileId: queryProfileId,
					};
				});
			}

			wsConn.resetLastMessageTime();
			const remoteOnly = !_state.project && !!_state.projectV2Id;
			const querySent = wsConn.send({
				type: 'query',
				prompt,
				session_id: _state.sessionId,
				profile: _state.profile,
				project: _state.project || undefined,
				project_v2_id: remoteOnly ? _state.projectV2Id : undefined,
				worktree_id: _state.worktreeId || undefined,
				// Composer device pick. Creation-time only: the backend ignores it
				// for an existing session.
				target_instance_id: _state.deviceIntent?.id || undefined,
				// Sidepanel mode for a NEW session. Creation-time only — the backend
				// ignores it once the session row exists.
				mode: _state.sessionId ? undefined : _state.mode,
				// Staged upload paths this prompt references (D8). Remote-only
				// compose has no local checkout — never send the list.
				upload_paths: !remoteOnly && options?.pendingUploadPaths?.length
					? options.pendingUploadPaths
					: undefined,
				// Adoption token — present only for a new-session query (see requestId).
				// The backend echoes it in session_created + the first start frame.
				request_id: requestId || undefined,
				overrides: buildOverrides(),
			});
			if (!querySent) {
				// WS dropped before the frame went out. Roll the optimistic echo back
				// and surface an error so the caller can restore the input.
				//
				// S6: withdrawing the flip restores the AUTHORITATIVE value — what
				// the feed reducer currently says — rather than forcing false. A
				// turn that is genuinely live (this send was a steer into it) keeps
				// its indicator; nothing has to guess.
				update(s => ({
					...s,
					...(optimisticQueuedId
						? { queuedMessages: s.queuedMessages.filter(m => m.id !== optimisticQueuedId) }
						: {
								optimisticStreaming: false,
								isStreaming: feedController.isReducerStreaming(s.sessionId),
							}),
					error: 'Connection lost before the message was sent. Please try again.',
				}));
				feedController.clearOptimisticSend(_state.sessionId);
				return false;
			}
			// The pick is spent: the session now exists and its row carries the
			// ownership, so follow-up sends must not re-assert a target. (Left
			// intact on a failed send so a retry still goes to the chosen device.)
			if (_state.deviceIntent) update(s => ({ ...s, deviceIntent: null }));
			return true;
		},

		stopGeneration() {
			const _state = getState();
			if (_state.sessionId) {
				// The interrupt is always a full stop (the query-scoped SDK interrupt
				// tears down in-flight background sub-agents too).
				// #585: carry the live turn id so the backend binds the stop to that
				// turn — a stop that lands after its turn settled then no-ops instead
				// of interrupting the successor. Omitted when the FE knows no live turn
				// (legacy session-scoped stop server-side).
				wsConn.send({
					type: 'stop',
					session_id: _state.sessionId,
					...(_state.liveTurnId ? { turn_id: _state.liveTurnId } : {}),
				});
			} else if (internal.awaitingNewSession) {
				// Brand-new session whose id the backend hasn't minted yet, so we
				// have nothing to address the stop to. Defer it: the session_created
				// / start handlers send the stop once the id arrives, so the backend
				// can discard the orphan turn instead of silently dropping the click.
				internal.stopRequestedBeforeSession = true;
			}

			const current = stopTimeout.get();
			if (current) clearTimeout(current);
			stopTimeout.set(setTimeout(() => {
				if (getState().isStreaming) {
					console.warn('[ActiveSession] Stop confirmation timeout - forcing UI recovery');
					// Deliberate override, not a competing writer: the stop was never
					// confirmed, so this is the UI's last-resort recovery. Withdraw
					// the optimistic flip FIRST — otherwise the next fold would
					// re-raise isStreaming straight back to true.
					feedController.clearOptimisticSend(getState().sessionId);
					update(s => {
						let messages = s.messages.map(m => {
							if (m.type === 'tool_use' && m.toolStatus === 'running') return { ...m, streaming: false, toolStatus: 'error' as const };
							return m.streaming ? { ...m, streaming: false } : m;
						});
						messages = messages.filter(m => !(m.type === 'text' && m.role === 'assistant' && !m.content));
						return { ...s, messages, isStreaming: false, optimisticStreaming: false };
					});
				}
				stopTimeout.set(null);
			}, 10000));
		},

		async createWorktree(prompt?: string, pendingUploadPaths?: string[]) {
			const _state = getState();
			if (!_state.profile) { update(s => ({ ...s, worktreeError: 'Please select a profile' })); return; }
			if (!_state.project && !_state.projectV2Id) { update(s => ({ ...s, worktreeError: 'Please select a project' })); return; }
			if (!_state.worktreeBranch) { update(s => ({ ...s, worktreeError: 'Please select a branch' })); return; }
			if (!_state.project && pendingUploadPaths?.length) {
				update(s => ({ ...s, worktreeError: ATTACHMENTS_NEED_LOCAL_CHECKOUT }));
				return;
			}

			const creationGen = internal.worktreeCreationGeneration;
			const projectId = _state.project;
			const projectV2Id = _state.projectV2Id;
			const queryProfileId = _state.profile;
			const branchName = _state.worktreeBranch;
			const remoteOnly = !projectId && !!projectV2Id;
			const overridesSnapshot = buildOverrides();

			update(s => ({ ...s, isCreatingWorktree: true, worktreeError: null }));

			try {
				const response = await api.post<{
					session_id: string; branch_name: string;
					base_branch: string; worktree_path: string; status: string;
					upload_migration_errors?: string[] | null;
				}>('/sessions/with-worktree', {
					...(remoteOnly
						? { project_v2_id: projectV2Id }
						: { project_id: projectId }),
					branch_name: branchName,
					base_branch: _state.worktreeBaseBranch || 'main',
					profile_id: queryProfileId,
					create_new_branch: _state.worktreeCreateNewBranch,
					pending_upload_paths: !remoteOnly && pendingUploadPaths?.length
						? pendingUploadPaths
						: undefined,
					// Composer device pick composes with the worktree checkbox: the
					// target install materializes the checkout itself (durable git-ops
					// queue) and the uploads travel as DB blobs.
					target_instance_id: _state.deviceIntent?.id || undefined,
				});

				// The pick is spent once the session row carries the ownership.
				if (_state.deviceIntent) update(s => ({ ...s, deviceIntent: null }));

				appData.loadSessions();
				appData.loadApiUserSessions();

				if (response.upload_migration_errors?.length) {
					console.warn('[Worktree] Upload migration had errors:', response.upload_migration_errors);
				}

				if (creationGen !== internal.worktreeCreationGeneration) {
					console.debug('[Worktree] User navigated away during createWorktree, sending query in background');
					if (prompt && wsConn.isConnected()) {
						wsConn.send({
							type: 'query',
							prompt,
							session_id: response.session_id,
							profile: queryProfileId,
							project: projectId,
							overrides: overridesSnapshot,
							upload_paths: pendingUploadPaths?.length ? pendingUploadPaths : undefined,
						});
					}
					return;
				}

				update(s => ({
					...s,
					sessionId: response.session_id,
					isCreatingWorktree: false,
					// Reset on session commit (PR #448): this fresh worktree session has
					// no running tasks yet — clear any strip inherited from the previous
					// view so nothing flashes before the first poll settles.
					activeTasks: [],
				}));

				persistSession();

				if (prompt) {
					if (!wsConn.isConnected()) {
						wsConn.connect();
						await waitForConnectionVoid();
					}

					if (creationGen !== internal.worktreeCreationGeneration) {
						console.debug('[Worktree] User navigated away during WS connect wait');
						wsConn.send({
							type: 'query',
							prompt,
							session_id: response.session_id,
							profile: queryProfileId,
							project: projectId,
							upload_paths: pendingUploadPaths?.length ? pendingUploadPaths : undefined,
						});
						return;
					}

					// The POST created the session but carried no prompt, so it had no
					// turn to attach uploads to — this follow-up query is that turn.
					// Locally the files are already in the checkout and the backend
					// ignores the list; on a remote target it lifts them into blobs.
					getActiveSession().sendMessage(prompt, {
						skipWorktreeSetup: true,
						pendingUploadPaths,
					});
				}
			} catch (error) {
				if (creationGen !== internal.worktreeCreationGeneration) return;

				const err = error as { detail?: string; message?: string; status?: number };
				const rawError = err.detail || err.message || '';
				const formatted = rawError
					? formatWorktreeError(rawError, branchName)
					: `Failed to create worktree session for '${branchName}'. Check the backend logs for details.`;
				update(s => ({
					...s,
					isCreatingWorktree: false,
					worktreeError: formatted,
				}));
			}
		},

		async autoCreateWorktree(prompt: string, pendingUploadPaths?: string[]) {
			prompt = prompt.trim();
			const _state = getState();
			if (!_state.profile) { update(s => ({ ...s, worktreeError: 'Please select a profile' })); return; }
			if (!_state.project && !_state.projectV2Id) { update(s => ({ ...s, worktreeError: 'Please select a project' })); return; }
			if (!_state.project && pendingUploadPaths?.length) {
				update(s => ({ ...s, worktreeError: ATTACHMENTS_NEED_LOCAL_CHECKOUT }));
				return;
			}

			const queryProfileId = _state.profile;
			const projectId = _state.project;
			const projectV2Id = _state.projectV2Id;
			const remoteOnly = !projectId && !!projectV2Id;
			const baseBranch = _state.worktreeBaseBranch || 'main';
			const modelOverrideSnap = _state.modelOverride;
			const permissionModeOverrideSnap = _state.permissionModeOverride;
			const effortLevelOverrideSnap = _state.effortLevelOverride;
			const fastModeSnap = _state.fastMode;
			const contextWindowSnap = contextWindowOverride(_state);
			const deviceIntentSnap = _state.deviceIntent;
			const userMsgId = `user-${Date.now()}`;

			const creationGen = internal.worktreeCreationGeneration;

			// NOTE: intentionally no AbortController. The backend now creates the
			// worktree AND kicks off the first Claude query in a single request,
			// so we want the POST to finish even if the user navigates away,
			// locks their phone, or closes the tab. Using ``keepalive: true``
			// lets the browser complete the POST in the background.
			const overridesPayload: Record<string, string | string[] | number | boolean> = {};
			if (_state.mode !== 'chat') overridesPayload.workspace_project_ids = [...(_state.workspaceProjectIds ?? [])];
			if (modelOverrideSnap) overridesPayload.model = modelOverrideSnap;
			if (permissionModeOverrideSnap) overridesPayload.permission_mode = permissionModeOverrideSnap;
			if (effortLevelOverrideSnap) overridesPayload.effort = effortLevelOverrideSnap;
			// Explicit boolean, same reason as buildOverrides above.
			overridesPayload.fast = fastModeSnap;
			if (contextWindowSnap != null) overridesPayload.context_window = contextWindowSnap;

			update(s => {
				// eslint-disable-next-line no-console
				if (STREAM_DEBUG) console.log(`[AS] optimistic append B-worktree id=${userMsgId} prevLen=${s.messages.length} "${prompt.slice(0, 30)}"`);
				return {
					...s,
					isCreatingWorktree: true,
					worktreeError: null,
					messages: [...s.messages, {
						id: userMsgId,
						role: 'user' as const,
						content: prompt,
						profileId: queryProfileId,
						timestamp: new Date().toISOString(),
					}],
				};
			});

			try {
				let branchName: string;
				if (projectId) {
					try {
						const result = await generateBranchName(projectId, prompt);
						branchName = result.branch_name;
					} catch {
						branchName = generateBranchNameLocal(prompt);
					}
				} else {
					branchName = generateBranchNameLocal(prompt);
				}

				// Update the branch label even if the user has navigated away —
				// generation guard only matters for state that binds to the
				// current view. Continue the POST regardless of navigation so
				// the backend spawns the query.
				if (creationGen === internal.worktreeCreationGeneration) {
					update(s => ({
						...s,
						worktreeBranch: branchName,
						worktreeCreateNewBranch: true,
					}));
				}

				const response = await api.post<{
					session_id: string; branch_name: string;
					base_branch: string; worktree_path: string; status: string;
					query_started?: boolean;
					upload_migration_errors?: string[] | null;
				}>('/sessions/with-worktree', {
					...(remoteOnly
						? { project_v2_id: projectV2Id }
						: { project_id: projectId }),
					branch_name: branchName,
					base_branch: baseBranch,
					profile_id: queryProfileId,
					create_new_branch: true,
					prompt,
					overrides: Object.keys(overridesPayload).length > 0 ? overridesPayload : undefined,
					pending_upload_paths: !remoteOnly && pendingUploadPaths?.length
						? pendingUploadPaths
						: undefined,
					// Composer device pick composes with the worktree checkbox (see
					// createWorktree above).
					target_instance_id: deviceIntentSnap?.id || undefined,
				}, { keepalive: true });

				// The pick is spent once the session row carries the ownership.
				if (deviceIntentSnap) update(s => ({ ...s, deviceIntent: null }));

				appData.loadSessions();
				appData.loadApiUserSessions();

				if (response.upload_migration_errors?.length) {
					console.warn('[Worktree] Upload migration had errors:', response.upload_migration_errors);
				}

				// User navigated away during POST: backend still created the
				// worktree + session and (if query_started) kicked off the
				// query. Nothing left to do on the old view — do NOT mutate
				// the current (switched) session's state.
				if (creationGen !== internal.worktreeCreationGeneration) {
					console.debug('[Worktree] User navigated away — backend query already running in background');
					// Fallback only needed if backend didn't start the query
					// (e.g. spawn failed). In that case fire WS query if we can.
					if (!response.query_started && wsConn.isConnected()) {
						wsConn.send({
							type: 'query',
							prompt,
							session_id: response.session_id,
							profile: queryProfileId,
							project: projectId,
							overrides: Object.keys(overridesPayload).length > 0 ? overridesPayload : undefined,
						});
					}
					return;
				}

				// Atomic state commit: sessionId + streaming flags land in the
				// same update so reactive consumers (session-switcher, typing
				// indicator, streaming placeholder) never observe a new
				// sessionId with isStreaming=false. Previously these were
				// split across two update() calls with an `await` (WS connect)
				// between them, which produced a visible gap where the typing
				// indicator and streaming UI were missing on auto-worktree
				// sessions.
				// The title must be derived here too: the backend backfills the
				// row from this prompt, but its session_updated broadcast only
				// reaches devices watching the session — and a headless
				// worktree turn has no registered watcher, so this device
				// would otherwise show 'New Chat' until the session is
				// reopened from the sidebar.
				const derivedTitle = prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '');
				update(s => ({
					...s,
					sessionId: response.session_id,
					title: derivedTitle,
					selectedWorktreeBranch: response.branch_name,
					isCreatingWorktree: false,
					// Reset on session commit (PR #448): the freshly auto-created
					// worktree session starts with no tasks — clear any inherited strip
					// so a prior session's running tasks can't flash on it.
					activeTasks: [],
					...(response.query_started
						// S6: declared optimistic flip until the freshly started
						// headless turn reports `turn.started` on the feed opened
						// just below.
						? { ...OPTIMISTIC_STREAMING_ON, error: null }
						: {}),
				}));

				persistSession();

				if (response.query_started) {
					// Start the SSE feed FIRST — before any async WS work. The
					// headless query runner (sessions_worktree.py →
					// launch_headless_query) starts run_query WITHOUT a
					// WebSocket attached, so the freshly-created session_id has
					// no live socket. The live SSE event log is the transcript
					// authority: feedController.start() opens
					// GET /sessions/{id}/events?from_seq=0 and its reducer folds
					// every stream/message event into activeSession, so typing
					// indicators and content render immediately.
					feedController.start(response.session_id);

					// Ensure WS is connected for side-channel delivery of
					// future events — but the query itself keeps running
					// regardless of WS state, and the poller already owns
					// the streaming UI state.
					if (!wsConn.isConnected()) {
						wsConn.connect();
						await waitForConnectionVoid();
					}
					return;
				}

				// Fallback path: backend failed to spawn the query (rare). The
				// backend already migrated uploads server-side before the turn
				// submit, so nothing to do here but send the query via WS.
				if (!wsConn.isConnected()) {
					wsConn.connect();
					await waitForConnectionVoid();
				}

				if (creationGen !== internal.worktreeCreationGeneration) {
					wsConn.send({
						type: 'query',
						prompt,
						session_id: response.session_id,
						profile: queryProfileId,
						project: projectId,
					});
					return;
				}

				getActiveSession().sendMessage(prompt, { skipWorktreeSetup: true, skipMessageAppend: true });
			} catch (error) {
				// User navigation guard — don't surface errors in a session
				// the user already left.
				if (creationGen !== internal.worktreeCreationGeneration) return;

				const err = error as { detail?: string; message?: string; status?: number };
				const rawError = err.detail || err.message || '';
				const branchDisplay = getState().worktreeBranch || 'auto-generated branch';
				const formatted = rawError
					? formatWorktreeError(rawError, branchDisplay)
					: `Failed to auto-create worktree. Check the backend logs for details.`;
				update(s => ({
					...s,
					isCreatingWorktree: false,
					worktreeError: formatted,
					messages: s.messages.filter(m => m.id !== userMsgId),
				}));
			}
		},

		// ── Pending Messages (server-side queue) ────────────────────────

		/** Unsend a message that hasn't reached the model yet. */
		removeQueuedMessage(id: string) {
			const _state = getState();
			const entry = _state.queuedMessages.find(m => m.id === id);
			if (!entry) return;
			// An optimistic echo carries an id only this tab has ever seen, so the
			// server cannot act on it — naming it in a remove frame just triggers a
			// rebroadcast that puts the bubble back. Drop it here and let
			// pendingQueue re-aim the removal at the real row when it arrives.
			const serverId = pendingQueue.requestRemoval(_state.sessionId, entry);
			update(s => ({ ...s, queuedMessages: s.queuedMessages.filter(m => m.id !== id) }));
			if (!serverId) return;
			wsConn.send({
				type: 'remove_queued_message',
				message_id: serverId,
				session_id: _state.sessionId,
			});
		},
	};
}

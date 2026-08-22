/**
 * Message Sender — Send message flow, interrupt-and-query, worktree creation,
 * stop generation, and message queue management.
 *
 * Plain .ts file — no $state/$derived/$effect runes (see KNOWN_ISSUES.md Root Cause 10).
 */

import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import type { ExecutionMode } from '../types';
import { api } from '$lib/api/client';
import { STREAM_DEBUG } from '$lib/utils/debug-log';
import { generateBranchName, generateBranchNameLocal } from '$lib/api/git';
import { ATTACHMENTS_NEED_LOCAL_CHECKOUT } from '$lib/components/chat/attach-disable';
// V2 Phase 1: SSE-fed reducer — the transcript authority for a session.
import { feedController } from './v2/feed-controller';
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

	function buildOverrides(): Record<string, string | number | boolean> | undefined {
		const _state = getState();
		const overrides: Record<string, string | number | boolean> = {};
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

			// Interrupting a live stream: use the two-phase interrupt_for_query
			// path (backend interrupts, drains, sends 'interrupt_confirmed') so
			// the new query only starts after the old one has fully released the
			// SDK pipe. Gate poll updates via interruptPending so stale polls
			// from the old query don't flip isStreaming false between interrupt
			// and start, which caused a visible gap + user-msg flicker.
			const mustInterrupt = !!(_state.isStreaming && _state.sessionId);
			if (mustInterrupt) {
				internal.interruptPending = true;
				internal.pendingTools.clear();

				const sessionIdForInterrupt = _state.sessionId!;
				const confirmed = await new Promise<boolean>((resolve) => {
					internal.interruptConfirmResolve = resolve;
					const sent = wsConn.send({
						type: 'interrupt_for_query',
						session_id: sessionIdForInterrupt,
					});
					if (!sent) {
						internal.interruptConfirmResolve = null;
						resolve(false);
						return;
					}
					// Safety timeout covers the backend's worst case: SDK
					// interrupt timeout (5s) + drain budget (8s) + buffer.
					setTimeout(() => {
						if (internal.interruptConfirmResolve === resolve) {
							internal.interruptConfirmResolve = null;
							resolve(false);
						}
					}, 15000);
				});

				if (!confirmed) {
					// Interrupt failed — abort the send and let the user retry.
					// Clear interruptPending so subsequent polls work.
					internal.interruptPending = false;
					update(s => ({ ...s, error: 'Unable to interrupt the active generation. Please try again.' }));
					return false;
				}
			}

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
			console.debug(`[ActiveSession] sendMessage: sessionId=${_state.sessionId}, awaitingNewSession=${internal.awaitingNewSession}, wsConnected=${wsConn.isConnected()}, mustInterrupt=${mustInterrupt}`);

			// Single atomic update — avoids intermediate render where streaming
			// is cleared but the new user message hasn't been appended yet.
			// When mustInterrupt, also strip streaming artifacts from the old
			// query (running tool_use → error, streaming text finalized, empty
			// assistant placeholders dropped).
			if (options?.skipMessageAppend) {
				update(s => {
					const messages = mustInterrupt
						? s.messages
							.map(m => {
								if (m.type === 'tool_use' && m.toolStatus === 'running') {
									return { ...m, streaming: false, toolStatus: 'error' as const };
								}
								return m.streaming ? { ...m, streaming: false } : m;
							})
							.filter(m => !(m.type === 'text' && m.role === 'assistant' && !m.content))
						: s.messages;
					return {
						...s,
						messages,
						isStreaming: true,
						error: null,
						title: newTitle,
						currentQueryProfileId: queryProfileId,
					};
				});
			} else {
				update(s => {
					const baseMessages = mustInterrupt
						? s.messages
							.map(m => {
								if (m.type === 'tool_use' && m.toolStatus === 'running') {
									return { ...m, streaming: false, toolStatus: 'error' as const };
								}
								return m.streaming ? { ...m, streaming: false } : m;
							})
							.filter(m => !(m.type === 'text' && m.role === 'assistant' && !m.content))
						: s.messages;
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
						isStreaming: true,
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
				// Staged upload paths this prompt references (D8). Remote-only
				// compose has no local checkout — never send the list.
				upload_paths: !remoteOnly && options?.pendingUploadPaths?.length
					? options.pendingUploadPaths
					: undefined,
				// Adoption token — present only for a new-session query (see requestId).
				// The backend echoes it in session_created + the first start frame.
				request_id: requestId || undefined,
				overrides: buildOverrides(),
				// "Send now": flag the replacement query so a start-frame send that
				// fails on a socket dropped during the interrupt drain doesn't drop
				// the message server-side (it streams via sync_engine + polling).
				interrupt_followup: mustInterrupt || undefined,
			});
			// interruptPending cleared by 'start' handler (ws-message-handlers.ts:395)
			// once the new query's stream_start event arrives.
			if (!querySent) {
				// WS dropped between the interrupt confirm and the query send.
				// Surface an error so the caller can restore the input.
				internal.interruptPending = false;
				update(s => ({
					...s,
					isStreaming: false,
					error: 'Connection lost before the message was sent. Please try again.',
				}));
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
					update(s => {
						let messages = s.messages.map(m => {
							if (m.type === 'tool_use' && m.toolStatus === 'running') return { ...m, streaming: false, toolStatus: 'error' as const };
							return m.streaming ? { ...m, streaming: false } : m;
						});
						messages = messages.filter(m => !(m.type === 'text' && m.role === 'assistant' && !m.content));
						return { ...s, messages, isStreaming: false };
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
			const overridesPayload: Record<string, string | number | boolean> = {};
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
				update(s => ({
					...s,
					sessionId: response.session_id,
					selectedWorktreeBranch: response.branch_name,
					isCreatingWorktree: false,
					// Reset on session commit (PR #448): the freshly auto-created
					// worktree session starts with no tasks — clear any inherited strip
					// so a prior session's running tasks can't flash on it.
					activeTasks: [],
					...(response.query_started
						? { isStreaming: true, error: null }
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

		// ── Queued Messages (server-side queue) ─────────────────────────

		async queueMessage(content: string): Promise<boolean> {
			const _state = getState();
			if (!_state.sessionId) return false;

			const optimisticId = `queued-local-${Date.now()}`;
			update(s => ({
				...s,
				queuedMessages: [...s.queuedMessages, {
					id: optimisticId,
					content,
					queuedAt: Date.now(),
				}],
			}));

			// Mirror sendMessage's preamble: the socket may be closed (idle
			// tab, backgrounded device) when the user hits Send later, and a
			// bare send() on a dead socket silently drops the queue request.
			if (!wsConn.isConnected()) {
				wsConn.resetReconnectAttempts();
				wsConn.connect();
				const connected = await waitForConnection();
				if (!connected) {
					update(s => ({
						...s,
						queuedMessages: s.queuedMessages.filter(m => m.id !== optimisticId),
						error: 'Unable to connect. Please check your connection and try again.',
					}));
					return false;
				}
			}

			const sent = wsConn.send({
				type: 'queue_message',
				session_id: _state.sessionId,
				content,
				profile: _state.currentQueryProfileId || _state.profile,
				project: _state.project || undefined,
			});

			if (!sent) {
				update(s => ({
					...s,
					queuedMessages: s.queuedMessages.filter(m => m.id !== optimisticId),
					error: 'Failed to queue message — connection lost. Please try again.',
				}));
				return false;
			}
			return true;
		},

		removeQueuedMessage(id: string) {
			wsConn.send({
				type: 'remove_queued_message',
				message_id: id,
				session_id: getState().sessionId,
			});
		},

		clearQueuedMessages() {
			const _state = getState();
			if (!_state.sessionId) return;
			wsConn.send({
				type: 'clear_queued_messages',
				session_id: _state.sessionId,
			});
		},
	};
}

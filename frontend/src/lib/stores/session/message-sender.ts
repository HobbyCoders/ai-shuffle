/**
 * Message Sender — Send message flow, worktree creation, stop generation, and
 * removal of messages the model hasn't seen yet.
 *
 * Plain .ts file — no $state/$derived/$effect runes (rune-free so it can be imported before component init).
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
import { getDefaultModel, getServerDefaultModel } from '$lib/constants/models';
import { composerPreferences } from '../composer-preferences';
import { effortToSend } from '../assistant';

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
	if (lower.includes('already exists') && lower.includes('new branch')) {
		return `Branch '${branchName}' already exists. Select it as an existing branch, or choose a different name.`;
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

/**
 * The backend refuses a "create new branch" request whose branch already
 * exists (409) instead of binding the new session to that branch's existing
 * checkout — usually another session's working tree (#1528). The phrase is
 * pinned by the backend's `new_branch_exists_detail`.
 */
function isNewBranchAlreadyExists(error: unknown): boolean {
	const err = error as { status?: number; detail?: unknown } | null;
	return err?.status === 409 && typeof err.detail === 'string'
		&& /^Branch '.+' already exists/.test(err.detail);
}

/** A fresh variant of an auto-generated branch name that collided. */
function uniqueBranchName(branchName: string): string {
	return `${branchName}-${Math.random().toString(36).slice(2).padEnd(4, '0').slice(0, 4)}`;
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
		createWorktree: (prompt?: string, pendingUploadPaths?: string[]) => Promise<string | null>;
		autoCreateWorktree: (prompt: string, pendingUploadPaths?: string[]) => Promise<boolean>;
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

	async function startSessionOnExistingWorktree(prompt: string, pendingUploadPaths?: string[]): Promise<boolean> {
		const _state = getState();
		if (!_state.profile) { update(s => ({ ...s, worktreeError: 'Please select a profile' })); return false; }
		if (!_state.project) { update(s => ({ ...s, worktreeError: 'Please select a project' })); return false; }
		if (!_state.worktreeId) { update(s => ({ ...s, worktreeError: 'Please select an existing worktree' })); return false; }

		// The plain WS query path carries both the device pick and the upload
		// paths, so an existing-worktree session composes with a device choice
		// exactly like any other send.
		return getActiveSession().sendMessage(prompt, {
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
		else if (!_state.sessionId) {
			// A new chat shows the default of a provider that can run. When the
			// server's own default belongs to one that cannot (only OpenAI is
			// connected, the server still says Claude), send what the picker shows.
			const shown = getDefaultModel();
			if (shown && shown !== getServerDefaultModel()) overrides.model = shown;
		}
		if (_state.permissionModeOverride) overrides.permission_mode = _state.permissionModeOverride;
		const effort = effortToSend(_state.sessionId, _state.effortLevelOverride);
		if (effort) overrides.effort = effort;
		// Always an explicit boolean. Omitting the key when fast mode is OFF made
		// the backend fall back to the session row, which still held the earlier
		// `true` — so turning fast mode off never reached the CLI. `false` is a
		// real pick and has to travel.
		overrides.fast = _state.fastMode;
		const contextWindow = contextWindowOverride(_state);
		if (contextWindow != null) {
			overrides.context_window = contextWindow;
			// The pin this tab last saw acknowledged (0: none). If another tab or
			// device has picked since, the worker keeps that newer pin (#1895).
			if (_state.sessionId && _state.contextWindowPin !== undefined) {
				overrides.context_window_base = _state.contextWindowPin ?? 0;
			}
		}
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

	// Shared by every caller of startSessionWithoutPrompt (see there).
	let promptlessCreate: Promise<string | null> | null = null;

	async function createWithoutPrompt(): Promise<string | null> {
		if (getState().isRestoringSession && !(await waitForRestore())) {
			update(s => ({ ...s, error: 'Still loading your previous session — please try again in a moment.' }));
			return null;
		}
		const _state = getState();
		if (_state.sessionId) return _state.sessionId;
		if (internal.awaitingNewSession || _state.isCreatingWorktree) {
			update(s => ({ ...s, error: 'This session is still being created — try again in a moment.' }));
			return null;
		}
		if (_state.deviceIntent) {
			update(s => ({ ...s, error: `Calls run on this device. Switch the runner to Local to call, or send a message to start on ${_state.deviceIntent!.name}.` }));
			return null;
		}

		const creationGen = internal.worktreeCreationGeneration;
		const adopt = (sessionId: string, extra: Partial<ActiveSessionState> = {}): string | null => {
			if (creationGen !== internal.worktreeCreationGeneration) return null;
			update(s => ({ ...s, sessionId, activeTasks: [], error: null, ...extra }));
			sessionEphemeralCache.remove('__new_chat__');
			persistSession();
			// The WebSocket create registers this device on the session and
			// the first `start` frame starts the feed. No query ran here, so do
			// both: the call's transcript renders through this feed.
			wsConn.send({ type: 'watch_session', session_id: sessionId });
			if (!feedController.isFeeding(sessionId)) feedController.start(sessionId);
			appData.loadSessions();
			appData.loadApiUserSessions();
			return sessionId;
		};

		if (_state.worktreeIntent === 'worktree' && !(_state.worktreeMode === 'existing' && _state.worktreeId)) {
			if (_state.worktreeMode === 'existing') {
				update(s => ({ ...s, worktreeError: 'Please select an available existing worktree before calling.' }));
				return null;
			}
			// Auto names its branch from the first message. A call has none, so
			// stage a generated name for this create only.
			const auto = !_state.worktreeBranch;
			if (auto) {
				update(s => ({ ...s, worktreeBranch: generateBranchNameLocal('', 'call'), worktreeCreateNewBranch: true }));
			}
			const created = await getActiveSession().createWorktree();
			if (!created) {
				// Put the chip back to Auto; a navigation already reset it.
				if (auto && creationGen === internal.worktreeCreationGeneration) {
					update(s => ({ ...s, worktreeBranch: '', worktreeCreateNewBranch: _state.worktreeCreateNewBranch }));
				}
				return null;
			}
			return adopt(created, { selectedWorktreeBranch: getState().worktreeBranch || null });
		}

		const remoteOnly = !_state.project && !!_state.projectV2Id;
		try {
			const response = await api.post<{ session_id: string }>('/sessions', {
				...(remoteOnly
					? { project_v2_id: _state.projectV2Id }
					: { project_id: _state.project || undefined }),
				profile_id: _state.profile || undefined,
				mode: _state.mode,
				worktree_id: _state.worktreeIntent === 'worktree' ? _state.worktreeId || undefined : undefined,
				overrides: buildOverrides(),
			});
			return adopt(response.session_id);
		} catch (error) {
			if (creationGen !== internal.worktreeCreationGeneration) return null;
			const err = error as { detail?: string; message?: string };
			update(s => ({ ...s, error: err.detail || err.message || 'Could not create the session.' }));
			return null;
		}
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
			// The call button is creating this compose's session right now. A
			// send here would mint a second one over the WebSocket.
			if (!_state.sessionId && promptlessCreate) {
				update(s => ({ ...s, error: 'This session is still being created — try again in a moment.' }));
				return false;
			}
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
				try {
					// Streaming calls acknowledge admission before the composer clears
					// its draft; WebRTC retains its existing local-transport result.
					const sent = await sendVoiceText(_state.sessionId, prompt);
					if (!sent) update(s => s.sessionId === _state.sessionId ? { ...s, error: 'Voice call is not ready. Keep your message and try again when connected.' } : s);
					return sent;
				} catch (error) {
					update(s => s.sessionId === _state.sessionId ? { ...s, error: error instanceof Error ? error.message : 'Could not send to the voice call. Your draft has been kept.' } : s);
					return false;
				}
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
				if (_state.worktreeMode === 'existing' && !_state.worktreeId) {
					update(s => ({...s, worktreeError:'Please select an available existing worktree before sending.'}));
					return false;
				}
				// Report the creator's outcome (#1636). Answering true for a create
				// that was refused before any request went out left the composer
				// cleared with the message gone. A navigation during the create
				// counts as sent: the request is out, and its text must not
				// reappear in the view the user moved to.
				const creationGen = internal.worktreeCreationGeneration;
				let accepted: boolean;
				if (_state.worktreeMode === 'existing' && _state.worktreeId) {
					accepted = await startSessionOnExistingWorktree(prompt, options?.pendingUploadPaths);
				} else if (_state.worktreeBranch) {
					accepted = (await getActiveSession().createWorktree(prompt, options?.pendingUploadPaths)) !== null;
				} else {
					accepted = await getActiveSession().autoCreateWorktree(prompt, options?.pendingUploadPaths);
				}
				return accepted || creationGen !== internal.worktreeCreationGeneration;
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
			const queuedEcho = sendWhileStreaming && !options?.skipMessageAppend;
			const optimisticQueuedId = queuedEcho
				&& !pendingQueue.authoritySpokeSince(_state.sessionId, dispatchedAt)
				? newLocalPendingId()
				: null;

			// Single atomic update — avoids intermediate render where streaming
			// is cleared but the new user message hasn't been appended yet.
			if (queuedEcho && !optimisticQueuedId) {
				// The server already described the queue: no echo anywhere. Falling
				// through to the transcript append below put a send into a live turn
				// on the transcript, where the steer's own `message.user` was a
				// second copy of it (#2016).
				update(s => ({ ...s, error: null }));
			} else if (options?.skipMessageAppend) {
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
				// Existing remote-only sessions stage attachments per session;
				// only an uncreated remote draft has no upload destination.
				upload_paths: (!remoteOnly || !!_state.sessionId) && options?.pendingUploadPaths?.length
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

		/** Create the staged branch session. Resolves to the new session id once
		 *  this view adopted it, or null (the reason is in `worktreeError` or
		 *  `error`, or the user navigated away). With a prompt, null also means
		 *  the session was created but its first message was refused. */
		async createWorktree(prompt?: string, pendingUploadPaths?: string[]): Promise<string | null> {
			const _state = getState();
			if (!_state.profile) { update(s => ({ ...s, worktreeError: 'Please select a profile' })); return null; }
			if (!_state.project && !_state.projectV2Id) { update(s => ({ ...s, worktreeError: 'Please select a project' })); return null; }
			if (!_state.worktreeBranch) { update(s => ({ ...s, worktreeError: 'Please select a branch' })); return null; }
			if (!_state.project && pendingUploadPaths?.length) {
				update(s => ({ ...s, worktreeError: ATTACHMENTS_NEED_LOCAL_CHECKOUT }));
				return null;
			}

			const creationGen = internal.worktreeCreationGeneration;
			const projectId = _state.project;
			const projectV2Id = _state.projectV2Id;
			const queryProfileId = _state.profile;
			const branchName = _state.worktreeBranch;
			// Claimed before the POST so a New Chat opened mid-create does not stage
			// this one-time choice; consumed on success, released on failure.
			const branchClaim = composerPreferences.claimBranchChoice(
				{ projectId, projectV2Id }, branchName, _state.worktreeCreateNewBranch);
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
					// No prompt rides this request, so the backend stores these as
					// the session's run binding (model, permission mode, ...) right
					// away. A call started on the new session needs them before
					// any message is sent.
					overrides: overridesSnapshot,
					pending_upload_paths: !remoteOnly && pendingUploadPaths?.length
						? pendingUploadPaths
						: undefined,
					// Composer device pick composes with the worktree checkbox: the
					// target install materializes the checkout itself (durable git-ops
					// queue) and the uploads travel as DB blobs.
					target_instance_id: _state.deviceIntent?.id || undefined,
				});

				// A successful row consumes the one-time account default, even after
				// navigation, but never a newer choice or a different owner's pick.
				if (response.session_id) branchClaim.consume();

				// The pick is spent once the session row carries the ownership —
				// only on the compose that spent it (see autoCreateWorktree).
				if (_state.deviceIntent && creationGen === internal.worktreeCreationGeneration) {
					update(s => ({ ...s, deviceIntent: null }));
				}

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
					return null;
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
							overrides: overridesSnapshot,
							upload_paths: pendingUploadPaths?.length ? pendingUploadPaths : undefined,
						});
						return null;
					}

					// The POST created the session but carried no prompt, so it had no
					// turn to attach uploads to — this follow-up query is that turn.
					// Locally the files are already in the checkout and the backend
					// ignores the list; on a remote target it lifts them into blobs.
					// Its refusal is the send's refusal: the composer keeps the text.
					const sent = await getActiveSession().sendMessage(prompt, {
						skipWorktreeSetup: true,
						pendingUploadPaths,
					});
					if (!sent && creationGen === internal.worktreeCreationGeneration) return null;
				}
				return response.session_id;
			} catch (error) {
				if (creationGen !== internal.worktreeCreationGeneration) return null;

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
				return null;
			} finally {
				// No-op after consume; otherwise the choice stays for a retry.
				branchClaim.release();
			}
		},

		/**
		 * Create the composed session without a first message and open it in
		 * this view. The composer's call button uses this, then starts a call on
		 * the result.
		 *
		 * It goes through the same creators a first send does, with the same
		 * picks: a staged branch through `createWorktree` (so a one-time branch
		 * choice is spent the same way), Auto through the same request with a
		 * generated branch name, and everything else through
		 * `POST /sessions` with the project, agent, lane, legacy worktree and
		 * run overrides the WebSocket query would carry. The session is created
		 * on this install: a call can only attach to a conversation owned here,
		 * so a device pick is refused rather than stranding a session the call
		 * cannot reach.
		 *
		 * Resolves to the session id, or null when nothing was opened: the
		 * reason is in `error`/`worktreeError`, or the user navigated away.
		 */
		startSessionWithoutPrompt(): Promise<string | null> {
			// One create per compose: a second press (or a second mounted
			// composer) joins the request already in flight.
			promptlessCreate ??= createWithoutPrompt().finally(() => { promptlessCreate = null; });
			return promptlessCreate;
		},

		/** Resolves false when this view refused or failed the create (the reason
		 *  is in `worktreeError`), true once the request was accepted or the
		 *  user navigated away from it. */
		async autoCreateWorktree(prompt: string, pendingUploadPaths?: string[]): Promise<boolean> {
			prompt = prompt.trim();
			const _state = getState();
			if (!_state.profile) { update(s => ({ ...s, worktreeError: 'Please select a profile' })); return false; }
			if (!_state.project && !_state.projectV2Id) { update(s => ({ ...s, worktreeError: 'Please select a project' })); return false; }
			if (!_state.project && pendingUploadPaths?.length) {
				update(s => ({ ...s, worktreeError: ATTACHMENTS_NEED_LOCAL_CHECKOUT }));
				return false;
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

				const postCreate = (name: string) => api.post<{
					session_id: string; branch_name: string;
					base_branch: string; worktree_path: string; status: string;
					query_started?: boolean;
					upload_migration_errors?: string[] | null;
				}>('/sessions/with-worktree', {
					...(remoteOnly
						? { project_v2_id: projectV2Id }
						: { project_id: projectId }),
					branch_name: name,
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

				let response: Awaited<ReturnType<typeof postCreate>>;
				try {
					response = await postCreate(branchName);
				} catch (error) {
					// The generated name collided with a branch that already exists
					// (e.g. the same task sent twice). The backend refused rather
					// than sharing that branch's checkout; mint a distinct name once.
					if (!isNewBranchAlreadyExists(error)) throw error;
					const retryName = uniqueBranchName(branchName);
					branchName = retryName;
					if (creationGen === internal.worktreeCreationGeneration) {
						update(s => ({ ...s, worktreeBranch: retryName }));
					}
					response = await postCreate(retryName);
				}

				// The pick is spent once the session row carries the ownership —
				// but only on the compose that spent it. After navigation the
				// current view's device pick belongs to a different compose.
				if (deviceIntentSnap && creationGen === internal.worktreeCreationGeneration) {
					update(s => ({ ...s, deviceIntent: null }));
				}

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
					return true;
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
					return true;
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
						overrides: Object.keys(overridesPayload).length > 0 ? overridesPayload : undefined,
					});
					return true;
				}

				const sent = await getActiveSession().sendMessage(prompt, { skipWorktreeSetup: true, skipMessageAppend: true });
				// Refused: the composer takes the text back, so drop its echo.
				if (!sent) update(s => ({ ...s, messages: s.messages.filter(m => m.id !== userMsgId) }));
				return sent;
			} catch (error) {
				// User navigation guard — don't surface errors in a session
				// the user already left.
				if (creationGen !== internal.worktreeCreationGeneration) return true;

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
				return false;
			}
		},

		// ── Pending Messages (server-side queue) ────────────────────────

		/** Deliver the existing queue; unlike Stop, preserve messages and background work. */
		interruptForQueuedMessages() {
			const state = getState();
			if (!state.sessionId || !state.queuedMessages.length) return;
			wsConn.send({
				type: 'interrupt_for_query',
				session_id: state.sessionId,
				...(state.liveTurnId ? { turn_id: state.liveTurnId } : {}),
			});
		},

		/** Unsend a message that hasn't reached the model yet. Returns its text. */
		removeQueuedMessage(id: string): string | null {
			const _state = getState();
			const entry = _state.queuedMessages.find(m => m.id === id);
			if (!entry) return null;
			// An optimistic echo carries an id only this tab has ever seen, so the
			// server cannot act on it — naming it in a remove frame just triggers a
			// rebroadcast that puts the bubble back. Drop it here and let
			// pendingQueue re-aim the removal at the real row when it arrives.
			const serverId = pendingQueue.requestRemoval(_state.sessionId, entry);
			update(s => ({ ...s, queuedMessages: s.queuedMessages.filter(m => m.id !== id) }));
			if (serverId) {
				wsConn.send({
					type: 'remove_queued_message',
					message_id: serverId,
					session_id: _state.sessionId,
				});
			}
			return entry.content;
		},
	};
}

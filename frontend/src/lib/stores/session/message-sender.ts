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
import { generateBranchName, generateBranchNameLocal, migrateUploadsToWorktree, reopenWorktree } from '$lib/api/git';

// ─── Helpers ─────────────────────────────────────────────────────────────────

export function formatWorktreeError(rawError: string, branchName: string): string {
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
	sessionPoller: {
		stop: (sessionId: string) => void;
		startStreaming: (sessionId: string) => void;
	};
	sessionState: {
		removeSession: (sessionId: string) => void;
	};
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
		startCloudSession: (prompt: string, pendingUploadPaths?: string[]) => Promise<void>;
	};
}

export interface SendMessageOptions {
	skipWorktreeSetup?: boolean;
	skipMessageAppend?: boolean;
	pendingUploadPaths?: string[];
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createMessageSender(deps: MessageSenderDeps) {
	const {
		update, getState, internal, wsConn,
		sessionPoller, sessionState, sessionEphemeralCache,
		appData, persistSession, stopTimeout, getActiveSession,
	} = deps;

	// ─── Internal helper for worktree existing branch sessions ──────

	async function startSessionOnExistingWorktree(prompt: string) {
		const _state = getState();
		if (!_state.profile) { update(s => ({ ...s, error: 'Please select a profile' })); return; }
		if (!_state.project) { update(s => ({ ...s, error: 'Please select a project' })); return; }
		if (!_state.worktreeId) { update(s => ({ ...s, error: 'Please select an existing worktree' })); return; }

		await getActiveSession().sendMessage(prompt, { skipWorktreeSetup: true });
	}

	// ─── Build overrides from state ─────────────────────────────────

	function buildOverrides(): Record<string, string> | undefined {
		const _state = getState();
		const overrides: Record<string, string> = {};
		if (_state.modelOverride) overrides.model = _state.modelOverride;
		if (_state.permissionModeOverride) overrides.permission_mode = _state.permissionModeOverride;
		if (_state.effortLevelOverride) overrides.effort = _state.effortLevelOverride;
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

	return {
		async sendMessage(prompt: string, options?: SendMessageOptions): Promise<boolean> {
			prompt = prompt.trim();
			const _state = getState();

			// Reopen completed worktree before sending message
			if (_state.worktreeDeleted && _state.worktreeId && _state.project) {
				update(s => ({ ...s, isReopeningWorktree: true, error: null }));
				try {
					await reopenWorktree(_state.project, _state.worktreeId);
					update(s => ({ ...s, worktreeDeleted: false, isReopeningWorktree: false }));
				} catch (e: any) {
					const msg = e?.message || String(e);
					update(s => ({ ...s, isReopeningWorktree: false, error: `Failed to reopen worktree: ${msg}` }));
					return false;
				}
			}

			// Legacy worktree sessions: worktree was deleted before reopen feature existed
			if (_state.worktreeDeleted && !_state.worktreeId) {
				update(s => ({ ...s, error: 'This worktree session cannot be resumed — the worktree was permanently deleted. Start a new session instead.' }));
				return false;
			}

			// Cloud setup on first message — offload to the configured second
			// install. Subsequent messages flow through the normal WS query path
			// (the backend forwards them to the worker).
			if (!options?.skipWorktreeSetup && _state.executionMode === 'cloud' && !_state.sessionId && _state.messages.length === 0) {
				await getActiveSession().startCloudSession(prompt, options?.pendingUploadPaths);
				return true;
			}

			// Worktree setup on first message
			if (!options?.skipWorktreeSetup && _state.executionMode === 'worktree' && !_state.sessionId && _state.messages.length === 0) {
				if (_state.worktreeMode === 'existing' && _state.worktreeId) {
					await startSessionOnExistingWorktree(prompt);
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
			// executionMode is 'worktree' with no sessionId yet but a message is
			// already present — i.e. autoCreateWorktree/createWorktree is still in
			// flight (e.g. a second send slipped in during the multi-second
			// auto-name step). Refuse rather than silently run in main; the
			// in-flight creation owns the first message.
			if (!options?.skipWorktreeSetup && _state.executionMode === 'worktree' && !_state.sessionId) {
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
					// Safety timeout matches backend drain (8s) + buffer.
					setTimeout(() => {
						if (internal.interruptConfirmResolve === resolve) {
							internal.interruptConfirmResolve = null;
							resolve(false);
						}
					}, 10000);
				});

				if (!confirmed) {
					// Interrupt failed — abort the send and let the user retry.
					// Clear interruptPending so subsequent polls work.
					internal.interruptPending = false;
					update(s => ({ ...s, error: 'Unable to interrupt the active generation. Please try again.' }));
					return false;
				}
			}

			// awaitingNewSession means "we sent a prompt with no session id yet and
			// are waiting for the backend to mint one" (consumed as
			// `!state.sessionId && awaitingNewSession` in ws-message-handlers and
			// session_created/start acceptance). It must only be set when there is
			// NO session id — setting it on the 2nd+ send of an EXISTING session
			// left a stale flag that forced shouldNotify=true for every event and
			// muddied session-routing. Gate it on the absence of a session id.
			if (!_state.sessionId) {
				internal.awaitingNewSession = true;
				sessionEphemeralCache.remove('__new_chat__');
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
						wsStreamContent: '',
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
						}],
						isStreaming: true,
						error: null,
						title: newTitle,
						currentQueryProfileId: queryProfileId,
						wsStreamContent: '',
					};
				});
			}

			wsConn.resetLastMessageTime();
			const querySent = wsConn.send({
				type: 'query',
				prompt,
				session_id: _state.sessionId,
				profile: _state.profile,
				project: _state.project || undefined,
				worktree_id: _state.worktreeId || undefined,
				overrides: buildOverrides(),
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
			return true;
		},

		stopGeneration() {
			const _state = getState();
			if (_state.sessionId) {
				wsConn.send({ type: 'stop', session_id: _state.sessionId });
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
			if (!_state.project) { update(s => ({ ...s, worktreeError: 'Please select a project' })); return; }
			if (!_state.worktreeBranch) { update(s => ({ ...s, worktreeError: 'Please select a branch' })); return; }

			const creationGen = internal.worktreeCreationGeneration;
			const projectId = _state.project;
			const queryProfileId = _state.profile;
			const branchName = _state.worktreeBranch;

			update(s => ({ ...s, isCreatingWorktree: true, worktreeError: null }));

			try {
				const response = await api.post<{
					session_id: string; worktree_id: string; branch_name: string;
					base_branch: string; worktree_path: string; status: string;
				}>('/sessions/with-worktree', {
					project_id: projectId,
					branch_name: branchName,
					base_branch: _state.worktreeBaseBranch || 'main',
					profile_id: queryProfileId,
					create_new_branch: _state.worktreeCreateNewBranch,
				});

				appData.loadSessions();
				appData.loadApiUserSessions();

				if (creationGen !== internal.worktreeCreationGeneration) {
					console.debug('[Worktree] User navigated away during createWorktree, sending query in background');
					if (prompt && wsConn.isConnected()) {
						wsConn.send({
							type: 'query',
							prompt,
							session_id: response.session_id,
							profile: queryProfileId,
							project: projectId,
							worktree_id: response.worktree_id,
						});
					}
					return;
				}

				update(s => ({
					...s,
					sessionId: response.session_id,
					worktreeId: response.worktree_id,
					isCreatingWorktree: false,
				}));

				persistSession();

				if (pendingUploadPaths?.length && projectId) {
					try {
						await migrateUploadsToWorktree(projectId, response.worktree_id, pendingUploadPaths);
					} catch (e) {
						console.warn('[Worktree] Failed to migrate uploads:', e);
					}
				}

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
							worktree_id: response.worktree_id,
						});
						return;
					}

					getActiveSession().sendMessage(prompt, { skipWorktreeSetup: true });
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
			if (!_state.project) { update(s => ({ ...s, worktreeError: 'Please select a project' })); return; }

			const queryProfileId = _state.profile;
			const projectId = _state.project;
			const baseBranch = _state.worktreeBaseBranch || 'main';
			const modelOverrideSnap = _state.modelOverride;
			const permissionModeOverrideSnap = _state.permissionModeOverride;
			const effortLevelOverrideSnap = _state.effortLevelOverride;
			const userMsgId = `user-${Date.now()}`;

			const creationGen = internal.worktreeCreationGeneration;

			// NOTE: intentionally no AbortController. The backend now creates the
			// worktree AND kicks off the first Claude query in a single request,
			// so we want the POST to finish even if the user navigates away,
			// locks their phone, or closes the tab. Using ``keepalive: true``
			// lets the browser complete the POST in the background.
			const overridesPayload: Record<string, string> = {};
			if (modelOverrideSnap) overridesPayload.model = modelOverrideSnap;
			if (permissionModeOverrideSnap) overridesPayload.permission_mode = permissionModeOverrideSnap;
			if (effortLevelOverrideSnap) overridesPayload.effort = effortLevelOverrideSnap;

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
					}],
				};
			});

			try {
				let branchName: string;
				try {
					const result = await generateBranchName(projectId, prompt);
					branchName = result.branch_name;
				} catch {
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
					session_id: string; worktree_id: string; branch_name: string;
					base_branch: string; worktree_path: string; status: string;
					query_started?: boolean;
					upload_migration_errors?: string[] | null;
				}>('/sessions/with-worktree', {
					project_id: projectId,
					branch_name: branchName,
					base_branch: baseBranch,
					profile_id: queryProfileId,
					create_new_branch: true,
					prompt,
					overrides: Object.keys(overridesPayload).length > 0 ? overridesPayload : undefined,
					pending_upload_paths: pendingUploadPaths?.length ? pendingUploadPaths : undefined,
				}, { keepalive: true });

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
							worktree_id: response.worktree_id,
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
					worktreeId: response.worktree_id,
					selectedWorktreeBranch: response.branch_name,
					isCreatingWorktree: false,
					...(response.query_started
						? { isStreaming: true, wsStreamContent: '', error: null }
						: {}),
				}));

				persistSession();

				if (response.query_started) {
					// Kick the poller FIRST — before any async WS work. The
					// headless query runner (sessions_worktree.py →
					// launch_headless_query) starts run_query WITHOUT a
					// WebSocket attached, so session-level sync_engine
					// broadcasts (stream_start / stream_chunk /
					// message_added) are dropped because no device is
					// registered to this freshly-created session_id.
					// The poller is the PRIMARY data source for
					// auto-worktree streams; starting it synchronously
					// (with isStreaming already true in state) ensures the
					// onPollApplied guard in session-init doesn't reject
					// the first response due to sessionId mismatch, and
					// that typing indicators render immediately.
					sessionPoller.startStreaming(response.session_id);

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

				// Fallback path: backend failed to spawn the query (rare).
				// Migrate uploads client-side if backend didn't, then send via WS.
				if (pendingUploadPaths?.length && projectId) {
					try {
						await migrateUploadsToWorktree(projectId, response.worktree_id, pendingUploadPaths);
					} catch (e) {
						console.warn('[Worktree] Failed to migrate uploads:', e);
					}
				}

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
						worktree_id: response.worktree_id,
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

		// ── Cloud offload (run on a second install) ─────────────────────

		async startCloudSession(prompt: string, pendingUploadPaths?: string[]) {
			prompt = prompt.trim();
			const _state = getState();
			if (!_state.profile) { update(s => ({ ...s, error: 'Please select a profile' })); return; }
			if (!_state.project) { update(s => ({ ...s, error: 'Please select a project' })); return; }

			const queryProfileId = _state.profile;
			const projectId = _state.project;
			const userMsgId = `user-${Date.now()}`;
			const creationGen = internal.worktreeCreationGeneration;

			const overridesPayload: Record<string, string> = {};
			if (_state.modelOverride) overridesPayload.model = _state.modelOverride;
			if (_state.permissionModeOverride) overridesPayload.permission_mode = _state.permissionModeOverride;
			if (_state.effortLevelOverride) overridesPayload.effort = _state.effortLevelOverride;

			// Optimistically show the prompt + a creating/streaming state.
			update(s => ({
				...s,
				isCreatingWorktree: true,
				error: null,
				title: s.title === 'New Chat'
					? prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '')
					: s.title,
				messages: [...s.messages, {
					id: userMsgId,
					role: 'user' as const,
					content: prompt,
					profileId: queryProfileId,
				}],
			}));

			try {
				const response = await api.post<{
					session_id: string; remote_session_id: string; remote_url: string;
					project_id: string; status: string; query_started?: boolean;
				}>('/sessions/cloud', {
					project_id: projectId,
					prompt,
					profile_id: queryProfileId,
					pending_upload_paths: pendingUploadPaths?.length ? pendingUploadPaths : undefined,
					overrides: Object.keys(overridesPayload).length > 0 ? overridesPayload : undefined,
				}, { keepalive: true });

				appData.loadSessions();
				appData.loadApiUserSessions();

				// User navigated away mid-request: the worker is already running
				// the query, so leave the (switched) view untouched.
				if (creationGen !== internal.worktreeCreationGeneration) {
					console.debug('[Cloud] User navigated away — worker query running in background');
					return;
				}

				update(s => ({
					...s,
					sessionId: response.session_id,
					isCreatingWorktree: false,
					isStreaming: true,
					wsStreamContent: '',
					error: null,
				}));

				persistSession();

				// The worker runs the query headlessly; the proxied /live endpoint
				// is our data source, so start the poller immediately.
				sessionPoller.startStreaming(response.session_id);

				// Connect WS for follow-up messages / interrupt forwarding.
				if (!wsConn.isConnected()) {
					wsConn.connect();
					await waitForConnectionVoid();
				}
			} catch (error) {
				if (creationGen !== internal.worktreeCreationGeneration) return;
				const err = error as { detail?: string; message?: string; status?: number };
				const rawError = err.detail || err.message || 'Failed to start cloud session.';
				update(s => ({
					...s,
					isCreatingWorktree: false,
					error: rawError,
					messages: s.messages.filter(m => m.id !== userMsgId),
				}));
			}
		},

		// ── Queued Messages (server-side queue) ─────────────────────────

		queueMessage(content: string): boolean {
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

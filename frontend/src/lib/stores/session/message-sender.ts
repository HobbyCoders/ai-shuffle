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
import { generateBranchName, generateBranchNameLocal, migrateUploadsToWorktree } from '$lib/api/git';

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
		sendMessage: (prompt: string, options?: SendMessageOptions) => Promise<void>;
		createWorktree: (prompt?: string, pendingUploadPaths?: string[]) => Promise<void>;
		autoCreateWorktree: (prompt: string, pendingUploadPaths?: string[]) => Promise<void>;
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
		if (_state.fastMode) overrides.fast_mode = 'true';
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
		async sendMessage(prompt: string, options?: SendMessageOptions) {
			const _state = getState();

			// Reopen completed worktree before sending message
			if (_state.worktreeDeleted && _state.worktreeId && _state.project) {
				update(s => ({ ...s, isReopeningWorktree: true, error: null }));
				try {
					const { reopenWorktree } = await import('$lib/api/git');
					await reopenWorktree(_state.project, _state.worktreeId);
					update(s => ({ ...s, worktreeDeleted: false, isReopeningWorktree: false }));
				} catch (e: any) {
					const msg = e?.message || String(e);
					update(s => ({ ...s, isReopeningWorktree: false, error: `Failed to reopen worktree: ${msg}` }));
					return;
				}
			}

			// Legacy worktree sessions: worktree was deleted before reopen feature existed
			if (_state.worktreeDeleted && !_state.worktreeId) {
				update(s => ({ ...s, error: 'This worktree session cannot be resumed — the worktree was permanently deleted. Start a new session instead.' }));
				return;
			}

			// Worktree setup on first message
			if (!options?.skipWorktreeSetup && _state.executionMode === 'worktree' && !_state.sessionId && _state.messages.length === 0) {
				if (_state.worktreeMode === 'existing' && _state.worktreeId) {
					await startSessionOnExistingWorktree(prompt);
					return;
				} else if (_state.worktreeBranch) {
					await getActiveSession().createWorktree(prompt, options?.pendingUploadPaths);
					return;
				} else {
					await getActiveSession().autoCreateWorktree(prompt, options?.pendingUploadPaths);
					return;
				}
			}

			if (!wsConn.isConnected()) {
				wsConn.resetReconnectAttempts();
				wsConn.connect();
				const connected = await waitForConnection();
				if (!connected) {
					update(s => ({ ...s, error: 'Unable to connect. Please check your connection and try again.' }));
					return;
				}
			}

			const userMsgId = `user-${Date.now()}`;
			const isFirstMessage = _state.title === 'New Chat';
			const newTitle = isFirstMessage
				? prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '')
				: _state.title;
			const queryProfileId = _state.profile;

			// Interrupt and re-query if currently streaming.
			if (_state.isStreaming && _state.sessionId) {
				const interruptSessionId = _state.sessionId;

				sessionPoller.stop(interruptSessionId);
				sessionState.removeSession(interruptSessionId);
				internal.interruptPending = true;

				update(s => {
					const updatedMessages = s.messages.map(m => {
						if (m.type === 'tool_use' && m.toolStatus === 'running') {
							return { ...m, streaming: false, toolStatus: 'error' as const };
						}
						return m.streaming ? { ...m, streaming: false } : m;
					});
					return {
						...s,
						messages: updatedMessages,
						isStreaming: true,
						error: null,
						title: newTitle,
						currentQueryProfileId: queryProfileId,
						wsStreamContent: '',
						pendingInterruptPrompt: prompt,
					};
				});

				internal.pendingTools.clear();

				wsConn.resetLastMessageTime();
				const sent = wsConn.send({
					type: 'interrupt_for_query',
					session_id: interruptSessionId,
				});

				if (!sent) {
					internal.interruptPending = false;
					update(s => ({ ...s, isStreaming: false, error: 'Failed to send interrupt. Please try again.' }));
					return;
				}

				const INTERRUPT_TIMEOUT_MS = 12_000;
				let timeoutId: ReturnType<typeof setTimeout>;

				const confirmed = await new Promise<boolean>((resolve) => {
					internal.interruptConfirmResolve = resolve;
					timeoutId = setTimeout(() => {
						if (internal.interruptConfirmResolve === resolve) {
							internal.interruptConfirmResolve = null;
							resolve(false);
						}
					}, INTERRUPT_TIMEOUT_MS);
				});
				clearTimeout(timeoutId!);

				if (getState().sessionId !== interruptSessionId) {
					internal.interruptPending = false;
					return;
				}

				if (!confirmed) {
					internal.interruptPending = false;
					update(s => ({
						...s,
						isStreaming: false,
						error: 'Interrupt timed out. Please try sending your message again.',
					}));
					return;
				}

				internal.interruptPending = false;

				update(s => ({
					...s,
					messages: [...s.messages, {
						id: userMsgId,
						role: 'user' as const,
						content: prompt,
						profileId: queryProfileId,
					}],
					isStreaming: true,
					error: null,
					wsStreamContent: '',
					pendingInterruptPrompt: null,
				}));

				wsConn.resetLastMessageTime();
				wsConn.send({
					type: 'query',
					prompt,
					session_id: interruptSessionId,
					profile: getState().profile,
					project: getState().project || undefined,
					worktree_id: getState().worktreeId || undefined,
					overrides: buildOverrides(),
				});
				return;
			}

			// Normal path (not currently streaming)
			internal.awaitingNewSession = true;
			if (!_state.sessionId) {
				sessionEphemeralCache.remove('__new_chat__');
			}
			console.debug(`[ActiveSession] sendMessage: sessionId=${_state.sessionId}, awaitingNewSession=true, wsConnected=${wsConn.isConnected()}`);

			if (options?.skipMessageAppend) {
				update(s => ({
					...s,
					isStreaming: true,
					error: null,
					title: newTitle,
					currentQueryProfileId: queryProfileId,
					wsStreamContent: '',
				}));
			} else {
				update(s => ({
					...s,
					messages: [...s.messages, {
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
				}));
			}

			wsConn.resetLastMessageTime();
			wsConn.send({
				type: 'query',
				prompt,
				session_id: _state.sessionId,
				profile: _state.profile,
				project: _state.project || undefined,
				worktree_id: _state.worktreeId || undefined,
				overrides: buildOverrides(),
			});
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
			const _state = getState();
			if (!_state.profile) { update(s => ({ ...s, worktreeError: 'Please select a profile' })); return; }
			if (!_state.project) { update(s => ({ ...s, worktreeError: 'Please select a project' })); return; }

			const queryProfileId = _state.profile;
			const projectId = _state.project;
			const baseBranch = _state.worktreeBaseBranch || 'main';
			const modelOverrideSnap = _state.modelOverride;
			const permissionModeOverrideSnap = _state.permissionModeOverride;
			const effortLevelOverrideSnap = _state.effortLevelOverride;
			const fastModeSnap = _state.fastMode;
			const userMsgId = `user-${Date.now()}`;

			const creationGen = internal.worktreeCreationGeneration;

			update(s => ({
				...s,
				isCreatingWorktree: true,
				worktreeError: null,
				messages: [...s.messages, {
					id: userMsgId,
					role: 'user' as const,
					content: prompt,
					profileId: queryProfileId,
				}],
			}));

			try {
				let branchName: string;
				try {
					const result = await generateBranchName(projectId, prompt);
					branchName = result.branch_name;
				} catch {
					branchName = generateBranchNameLocal(prompt);
				}

				const staleAfterBranchGen = creationGen !== internal.worktreeCreationGeneration;

				if (!staleAfterBranchGen) {
					update(s => ({
						...s,
						worktreeBranch: branchName,
						worktreeCreateNewBranch: true,
					}));
				}

				const response = await api.post<{
					session_id: string; worktree_id: string; branch_name: string;
					base_branch: string; worktree_path: string; status: string;
				}>('/sessions/with-worktree', {
					project_id: projectId,
					branch_name: branchName,
					base_branch: baseBranch,
					profile_id: queryProfileId,
					create_new_branch: true,
				});

				appData.loadSessions();
				appData.loadApiUserSessions();

				if (staleAfterBranchGen || creationGen !== internal.worktreeCreationGeneration) {
					console.debug('[Worktree] User navigated away, sending query in background');
					const sendBackgroundQuery = () => {
						const overrides: Record<string, string> = {};
						if (modelOverrideSnap) overrides.model = modelOverrideSnap;
						if (permissionModeOverrideSnap) overrides.permission_mode = permissionModeOverrideSnap;
						if (effortLevelOverrideSnap) overrides.effort = effortLevelOverrideSnap;
						if (fastModeSnap) overrides.fast_mode = 'true';
						wsConn.send({
							type: 'query',
							prompt,
							session_id: response.session_id,
							profile: queryProfileId,
							project: projectId,
							worktree_id: response.worktree_id,
							overrides: Object.keys(overrides).length > 0 ? overrides : undefined,
						});
					};
					if (wsConn.isConnected()) {
						sendBackgroundQuery();
					} else {
						wsConn.connect();
						const timeout = setTimeout(() => clearInterval(check), 3000);
						const check = setInterval(() => {
							if (wsConn.isConnected()) {
								clearTimeout(timeout);
								clearInterval(check);
								sendBackgroundQuery();
							}
						}, 50);
					}
					return;
				}

				update(s => ({
					...s,
					sessionId: response.session_id,
					worktreeId: response.worktree_id,
					selectedWorktreeBranch: response.branch_name,
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

				if (!wsConn.isConnected()) {
					wsConn.connect();
					await waitForConnectionVoid();
				}

				if (creationGen !== internal.worktreeCreationGeneration) {
					console.debug('[Worktree] User navigated away during WS connect wait, sending query in background');
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

		queueMessage(content: string) {
			const _state = getState();
			if (!_state.sessionId) return;

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
			}
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

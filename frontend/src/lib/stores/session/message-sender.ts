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
import { compactSession } from '$lib/api/limit-resume';
import { compactRetryId, clearCompactRetry } from './compact-first-retry';
import { isCallActiveFor, sendVoiceText } from '$lib/services/voiceCall';
import { persistVoiceContext, voiceContextFor } from '$lib/services/voiceExecutionContext';
import { STREAM_DEBUG } from '$lib/utils/debug-log';
import { generateBranchName, generateBranchNameLocal } from '$lib/api/git';
import { ATTACHMENTS_NEED_LOCAL_CHECKOUT } from '$lib/components/chat/attach-disable';
import { keepStopRecovery, STOP_TURN_NOT_NAMED, stopPressedOn } from '$lib/utils/not-sent-hold';
import { parkedSendTurnId } from '$lib/utils/work-log';
// V2 Phase 1: SSE-fed reducer — the transcript authority for a session.
import { feedController, OPTIMISTIC_STREAMING_ON } from './v2/feed-controller';
// Arbiter of which pending ("not seen yet") bubbles survive.
import { pendingQueue, newLocalPendingId, SEND_MAYBE_APPLIED_MESSAGE } from './pending-queue';
import { selectedContextWindow } from './context-window';
import { getDefaultModel, getServerDefaultModel } from '$lib/constants/models';
import { composerPreferences } from '../composer-preferences';
import { sendableComposerBinding } from './composer-binding';
import { registerReloadBlocker } from '$lib/services/app-update';
import { claimBootSend, releaseBootSend, getInitGeneration } from './session-init';
import { retireSends, runWithSend, sendRunIn, stillPressedOn } from './send-run';
import { ANY_CHAT, createSendOrder, type SendLane, type SendReservation, type SendTurn } from './send-order';

// ─── Sends in flight ─────────────────────────────────────────────────────────

/**
 * Sends whose request has not settled. The composer clears its draft as a
 * send starts, so until the send settles a reload would lose the message (a
 * refused send puts it back). Kept here, not in the composer, so the guard
 * outlives a composer that unmounts mid-send (an account switch, the
 * composer leaving view) and covers every caller (#711).
 */
let sendsInFlight = 0;

/** Why a reload now would lose a message, or null. */
export function pendingSendReason(): string | null {
	return sendsInFlight > 0 ? 'message-sending' : null;
}

registerReloadBlocker(pendingSendReason);

/** Run one send, counted as in flight until it settles either way. */
export async function trackSend<T>(send: () => Promise<T>): Promise<T> {
	sendsInFlight++;
	try {
		return await send();
	} finally {
		sendsInFlight--;
	}
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

// Match the streamed typed-text admission deadline in voiceAudioStream.
const VOICE_CONTEXT_SAVE_TIMEOUT_MS = 10_000;
// Send order (#1444): the longest a send waits for an earlier send to its
// chat to end. Past it the LATER send is refused, never the earlier one.
export const SEND_ORDER_WAIT_MS = 15_000;
/** How a send's turn outlives its return: until the chat it creates is
 *  created, or the create failed. `ended` answers now, without waiting. */
type CreationEnd = { ended: () => boolean; done: Promise<void> };

/**
 * Who owns the session an unbound compose is about to create. The backend
 * creates it on this worker unless the composer's "Runs on" pick names
 * another one, so the owner is known before any request goes out.
 *
 * Every creation path writes this onto the compose BEFORE it binds the new
 * session id, and every bind keeps it (they spread the state). Left unset,
 * `isRemote` stayed undefined until the sidebar list reloaded, and ChatView's
 * MirrorSyncGate reads an unknown owner as a mirrored chat: it swapped the
 * new transcript for "Checking sync…", then, on its first "current" answer,
 * reloaded the feed from seq 0. That replay could catch up before the turn's
 * first event was logged, so the chat went full page → reload → the centred
 * empty composer → full page on every new session (the owner's report).
 * Returns null for a bound compose: its owner comes from the row or /live.
 */
export function newSessionWorker(
	state: Pick<ActiveSessionState, 'sessionId' | 'deviceIntent'>
): Pick<ActiveSessionState, 'isRemote' | 'ownerDeviceName' | 'createdFromComposer'> | null {
	if (state.sessionId) return null;
	const target = state.deviceIntent?.id ? state.deviceIntent : null;
	return { isRemote: target !== null, ownerDeviceName: target?.name ?? null, createdFromComposer: true };
}

/** An optimistic echo's attachments, in the shape its durable row will carry. */
function uploadsMetadata(paths: string[] = [], references: import('$lib/api/rewind').UploadReference[] = []): { metadata?: { upload_paths: string[] } } {
	const all = [...paths, ...references.map(reference => reference.path)];
	return all.length ? { metadata: { upload_paths: all } } : {};
}

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
		createWorktree: (prompt?: string, pendingUploadPaths?: string[], creation?: Creation) => Promise<string | null>;
		autoCreateWorktree: (prompt: string, pendingUploadPaths?: string[], creation?: Creation) => Promise<boolean>;
	};
}

/** A worktree create in progress, handed to its own first send (made with
 *  skipWorktreeSetup) so that send runs inside the creating send's turn
 *  (#1444). Passed, never shared: creates from two composes can end in any
 *  order. */
export type Creation = { holdUntil: (after: CreationEnd) => void };

interface SendMessageOptions {
	skipWorktreeSetup?: boolean;
	/** The create this first send belongs to (see Creation). */
	creation?: Creation;
	skipMessageAppend?: boolean;
	pendingUploadPaths?: string[];
	pendingUploadReferences?: import('$lib/api/rewind').UploadReference[];
	compactFirst?: boolean;
	/** The composer's check that the user is still where they sent from
	 *  (send-destination.ts). Read once the send knows its target; false
	 *  sends nothing, sets no error, and the caller keeps the draft (#1077). */
	stillAtDestination?: () => boolean;
	/** A message with a note chip sent into an open chat (#1364). `prompt`
	 *  carries the notes as `@Notes/<id>`; `runsHere` asks the server whether
	 *  this worker runs the chat, and only "yes" sends `pathPrompt`, with each
	 *  note as this worker's file. Asked after every wait of the send, right
	 *  before the frame is written, so a move during those waits is seen. */
	notesForm?: { pathPrompt: string; runsHere: () => Promise<boolean> };
	/** Called at most once, synchronously, right after this send's frame is
	 *  written: the query frame, or a voice call's text frame before its
	 *  acknowledgement is awaited. Not called by a send that ends without a
	 *  frame, a compact-first send or a send that creates a worktree. The
	 *  composer lets the next send go here, so sends keep their order. */
	onDispatched?: () => void;
	/** The place in line this send took earlier (`reserveSend`), for a caller
	 *  that awaits before it calls sendMessage (#1444). */
	turn?: SendReservation;
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createMessageSender(deps: MessageSenderDeps) {
	const {
		update, getState, internal, wsConn,
		sessionEphemeralCache,
		appData, persistSession, stopTimeout, getActiveSession,
	} = deps;

	/** Mark this as a fresh compose and write its owner before anything
	 *  binds it (see newSessionWorker). No-op once a session is bound. */
	function claimNewSessionWorker() {
		update(s => {
			const owner = newSessionWorker(s);
			return owner ? { ...s, ...owner } : s;
		});
	}

	// ─── Internal helper for worktree existing branch sessions ──────

	async function startSessionOnExistingWorktree(prompt: string, pendingUploadPaths?: string[], creation?: Creation): Promise<boolean> {
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
			creation,
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
		return selectedContextWindow(state);
	}

	/** `_state` is the snapshot the send's session and agent came from, so a
	 *  switch while it waited for the socket cannot mix two sessions' settings. */
	function buildOverrides(_state: ActiveSessionState = getState()): Record<string, string | string[] | number | boolean> | undefined {
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
		// Agent, access and Fast still waiting for this session's own binding
		// are left out: the backend then keeps the row's value (#418).
		const binding = sendableComposerBinding(_state);
		if (binding.permissionMode) overrides.permission_mode = binding.permissionMode;
		if (_state.effortLevelOverride) overrides.effort = _state.effortLevelOverride;
		// Always an explicit boolean. Omitting the key when fast mode is OFF made
		// the backend fall back to the session row, which still held the earlier
		// `true` — so turning fast mode off never reached the CLI. `false` is a
		// real pick and has to travel; an unhydrated placeholder is not one.
		if (binding.fast !== undefined) overrides.fast = binding.fast;
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

	/** A sent agent / Fast pick is now the session's stored binding, so a
	 *  later /live answer may describe it again (#418). */
	function spendBindingPicks(sessionId: string | null) {
		if (!sessionId) return;
		update(s => s.sessionId === sessionId && Object.keys(s.bindingPicks ?? {}).length > 0
			? { ...s, bindingPicks: {} } : s);
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
		// A "Runs on" pick creates the conversation on that worker; the call
		// itself runs here and forwards to it (#700).
		claimNewSessionWorker();
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
				target_instance_id: _state.deviceIntent?.id || undefined,
			});
			// The pick is spent once the row carries the ownership (see sendMessage).
			return adopt(response.session_id, _state.deviceIntent ? { deviceIntent: null } : {});
		} catch (error) {
			if (creationGen !== internal.worktreeCreationGeneration) return null;
			const err = error as { detail?: string; message?: string };
			update(s => ({ ...s, error: err.detail || err.message || 'Could not create the session.' }));
			return null;
		}
	}

	// Sends to one chat keep their order (#1444); see the wrapper at the end.
	const sendOrder = createSendOrder();

	const sender = {
		/** `letNextGo` is the wrapper's: called where this send's frame is
		 *  written, it ends this send's turn so the next send to its chat may
		 *  go. Given a promise, the turn ends when that settles instead (a new
		 *  chat's creation). Not called, the turn ends when this returns. */
		async sendMessage(prompt: string, options?: SendMessageOptions, letNextGo?: (after?: CreationEnd) => void): Promise<boolean> {
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
			// New chat or a switch during that wait: the target below would be
			// the view the user moved to, not the chat they wrote in.
			if (options?.stillAtDestination && !options.stillAtDestination()) return false;
			// Picks the note form (see notesForm). False when the view moved while
			// the server was asked: nothing is sent and the words stay with their chat.
			// Awaited only with a notesForm: any other send keeps its timing.
			const settleNotesForm = async (): Promise<boolean> => {
				if (!options?.notesForm) return true;
				if (await options.notesForm.runsHere()) prompt = options.notesForm.pathPrompt.trim();
				return !options.stillAtDestination || options.stillAtDestination();
			};

			const _state = getState();
			// The call button is creating this compose's session right now. A
			// send here would mint a second one over the WebSocket.
			if (!_state.sessionId && promptlessCreate) {
				update(s => ({ ...s, error: 'This session is still being created — try again in a moment.' }));
				return false;
			}
			if (_state.sessionId && isCallActiveFor(_state.sessionId)) {
				let contextTimeout: ReturnType<typeof setTimeout> | undefined;
				try {
					// Race only persistence, not a continuation that sends: after the
					// deadline this attempt is dead even if the PATCH later succeeds.
					await Promise.race([
						persistVoiceContext(_state.sessionId, voiceContextFor(_state)),
						new Promise<never>((_, reject) => {
							contextTimeout = setTimeout(() => reject(new Error('Voice context save timed out')), VOICE_CONTEXT_SAVE_TIMEOUT_MS);
						})
					]);
				} catch {
					update(s => s.sessionId === _state.sessionId ? { ...s, error: 'Could not save voice execution settings. Your message has been kept.' } : s);
					return false;
				} finally {
					clearTimeout(contextTimeout);
				}
				if (options?.notesForm && !(await settleNotesForm())) return false;
				const paths = options?.pendingUploadPaths;
				const references = options?.pendingUploadReferences;
				const uploads = paths?.length || references?.length ? { paths, references } : undefined;
				try {
					// Streaming calls acknowledge admission before the composer clears
					// its draft; WebRTC retains its existing local-transport result.
					// The text frame is written inside this call; only its
					// acknowledgement is awaited below.
					const pending = uploads ? sendVoiceText(_state.sessionId, prompt, uploads) : sendVoiceText(_state.sessionId, prompt);
					letNextGo?.();
					if (pending !== false) options?.onDispatched?.();
					const sent = await pending;
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
			// The same spend point records who owns the session about to be
			// created, so every bind below starts with a known owner.
			if (!_state.sessionId) {
				sessionEphemeralCache.remove('__new_chat__');
				claimNewSessionWorker();
			}

			// Worktree sessions never block on send: the backend resolves/validates
			// the cwd on the query path (worktreeDeleted is a display-only badge).

            if (!_state.sessionId && options?.pendingUploadReferences?.length) {
                update(s => ({ ...s, error: 'Restored attachments belong to their original chat. Reattach the files to send them in a new chat.' }));
                return false;
            }

			// Worktree setup on first message
			if (!options?.skipWorktreeSetup && _state.worktreeIntent === 'worktree' && !_state.sessionId && _state.messages.length === 0) {
				// The create is this send: its turn lasts until the create ends, so
				// a send made meanwhile waits and goes to the chat it creates
				// rather than starting a second create. The create's own first
				// send runs inside this turn (Creation).
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
				const bootClaim = claimBootSend();
				let accepted = false;
				const creation: Creation = { holdUntil: (after) => letNextGo?.(after) };
				let createEnded!: () => void;
				let createOver = false;
				letNextGo?.({ ended: () => createOver, done: new Promise<void>(resolve => { createEnded = resolve; }) });
				try {
					if (_state.worktreeMode === 'existing' && _state.worktreeId) {
						accepted = await startSessionOnExistingWorktree(prompt, options?.pendingUploadPaths, creation);
					} else if (_state.worktreeBranch) {
						accepted = (await getActiveSession().createWorktree(prompt, options?.pendingUploadPaths, creation)) !== null;
					} else {
						accepted = await getActiveSession().autoCreateWorktree(prompt, options?.pendingUploadPaths, creation);
					}
					accepted ||= creationGen !== internal.worktreeCreationGeneration;
					return accepted;
				} finally {
					createOver = true;
					createEnded();
					// Creation can adopt the chat before its inner first send fails.
					// That live chat still owns boot even when its draft is restored.
					if (!accepted && !getState().sessionId) releaseBootSend(bootClaim);
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

			if (options?.compactFirst && !_state.sessionId) {
				throw new Error('Open a chat before compacting it.');
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

			// Validation has admitted the attempt. Own boot BEFORE the first
			// connection await; withdraw the claim if no query reaches the wire.
			const bootClaim = !_state.sessionId ? claimBootSend() : null;
			let querySent = false;
			try {
				if (!options?.compactFirst && !wsConn.isConnected()) {
					wsConn.resetReconnectAttempts();
					wsConn.connect();
					const connected = await waitForConnection();
					if (!connected) {
						update(s => ({ ...s, error: 'Unable to connect. Please check your connection and try again.' }));
						return false;
					}
				}

				// An unbound send must not create into a newly adopted target.
				// Existing-chat sends already know their target: keep that id and
				// its captured binding even if the view switches meanwhile (#418).
				if (!_state.sessionId && getState().sessionId !== _state.sessionId) return false;

				// The note form, from an answer taken after the waits above. From
				// here to the frame nothing awaits, so a move cannot slip in.
				if (options?.notesForm && !(await settleNotesForm())) return false;

				const userMsgId = `user-${Date.now()}`;
				const isFirstMessage = _state.title === 'New Chat';
				const newTitle = isFirstMessage
					? prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '')
					: _state.title;
				// Undefined while an existing session's agent is still loading: the
				// backend resolves an omitted agent to the session's own (#418).
				const queryProfileId = sendableComposerBinding(_state).profile;

				// Sending during a live turn is a plain `query`: the turn keeps running
				// and the backend decides when the model sees the new prompt (mid-turn
				// steer, or the start of the next turn). Until it lands, the message
				// shows as a pending bubble fed by `queuedMessages` — so the optimistic
				// append below goes there instead of onto the transcript.
				const sendWhileStreaming = !!(_state.isStreaming && _state.sessionId);

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
				// Admission is command-specific; an unrelated queue snapshot cannot
				// suppress the echo or its recoverable text.
				const overrides = buildOverrides(_state);
				const retryOverrides = { ...overrides };
				// The worker can acknowledge a newer pin before a lost reply is retried.
				// Its revision precondition is not a new owner instruction.
				delete retryOverrides.context_window_base;
				const clientMessageId = options?.compactFirst
					? compactRetryId(_state.sessionId!, JSON.stringify({ prompt, profile: queryProfileId,
						project: _state.project, uploadPaths: options.pendingUploadPaths, uploadReferences: options.pendingUploadReferences, overrides: retryOverrides }), newLocalPendingId)
					: newLocalPendingId();
				const queuedEcho = (sendWhileStreaming || options?.compactFirst) && !options?.skipMessageAppend;
				// An unrelated snapshot cannot admit this newly minted command.
				const optimisticQueuedId = queuedEcho ? clientMessageId : null;
				// A confirmed withdrawal or refusal hands these back with the words.
				pendingQueue.noteSendUploads(_state.sessionId, clientMessageId, options?.pendingUploadPaths, options?.pendingUploadReferences);

				// A fresh local send can clear an error without adding a transcript row
				// (queued/authoritative/pre-appended paths). Publish that intent in the
				// same transaction, rather than making presentation infer it from a row.
				const localSendIntent = (s: ActiveSessionState) => ({
					sessionId: _state.sessionId,
					revision: (s.localSendIntent?.revision ?? 0) + 1,
					// A Stop that times out names its turn from the receipt of the send
					// it was pressed on, whether or not that send has a transcript row
					// (steered, queued, pre-appended). See send-run.ts for the run.
					commandIds: runWithSend(s, _state.sessionId, clientMessageId,
						sendWhileStreaming || !!options?.compactFirst,
						id => !!feedController.hasReceipt?.(_state.sessionId, id)),
				});

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
						localSendIntent: localSendIntent(s),
						title: newTitle,
						currentQueryProfileId: queryProfileId ?? null,
					}));
				} else if (optimisticQueuedId) {
					pendingQueue.noteOptimistic(_state.sessionId, optimisticQueuedId);
					// One entry for the bubble and for the copy a later snapshot rebuilds
					// it from, so its chips show before and after an unrelated snapshot.
					const sentPaths = pendingQueue.sendUploadPaths(clientMessageId);
					const queuedEntry = { id: optimisticQueuedId, clientMessageId, content: prompt, queuedAt: Date.now(),
						...(sentPaths.length ? { uploadPaths: sentPaths } : {}) };
					pendingQueue.trackSend(_state.sessionId, queuedEntry, () => {
						update(s => s.sessionId === _state.sessionId ? { ...s,
							queuedMessages: s.queuedMessages.map(m => m.clientMessageId === clientMessageId ? { ...m, unconfirmed: true } : m),
							error: 'Sending was not confirmed. Unsend this message to recover the text and try again.',
						} : s);
					});
					update(s => ({
						...s,
						// A compact retry reuses the command id of a send still shown unconfirmed.
						queuedMessages: [...s.queuedMessages.filter(m => m.id !== optimisticQueuedId), queuedEntry],
						error: null,
						localSendIntent: localSendIntent(s),
					}));
				} else {
					// Retain idle drafts by session/command even if navigation evicts
					// the optimistic transcript before its correlated refusal arrives.
					pendingQueue.trackSend(_state.sessionId, { id: userMsgId, clientMessageId, content: prompt, queuedAt: Date.now() });
					update(s => {
						const baseMessages = s.messages;
						// eslint-disable-next-line no-console
						if (STREAM_DEBUG) console.log(`[AS] optimistic append A id=${userMsgId} prevLen=${baseMessages.length} "${prompt.slice(0, 30)}"`);
						// A send into a parked turn is a steer: carry that turn's id so the
						// transcript keeps it inside the turn instead of folding the turn.
						const steerTurnId = parkedSendTurnId(s);
						return {
							...s,
							messages: [...baseMessages, {
								id: userMsgId,
								role: 'user' as const,
								content: prompt,
								profileId: queryProfileId,
								...(steerTurnId ? { turnId: steerTurnId } : {}),
								// The same command identity comes back on the durable receipt.
								commandId: clientMessageId,
								timestamp: new Date().toISOString(),
								// The bubble shows its chips before the durable row arrives.
								...uploadsMetadata(options?.pendingUploadPaths, options?.pendingUploadReferences),
							}],
							// S6: declared optimistic flip (see the skipMessageAppend
							// branch above) — not a competing write of isStreaming.
							...OPTIMISTIC_STREAMING_ON,
							error: null,
							localSendIntent: localSendIntent(s),
							title: newTitle,
							currentQueryProfileId: queryProfileId ?? null,
						};
					});
				}

				const remoteOnly = !_state.project && !!_state.projectV2Id;
				const query = {
					type: 'query',
					client_message_id: clientMessageId,
					prompt,
					session_id: _state.sessionId,
					profile: queryProfileId,
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
                    upload_references: options?.pendingUploadReferences?.length ? options.pendingUploadReferences : undefined,
					// Adoption token — present only for a new-session query (see requestId).
					// The backend echoes it in session_created + the first start frame.
					request_id: requestId || undefined,
					overrides,
				};
				if (options?.compactFirst) {
					// Whether the worker answered that it did not queue this send.
					let refused = false;
					try {
						// The worker admits this send when it answers; its turn ends when
						// this returns, so a later send cannot be admitted first.
						const result = await compactSession(_state.sessionId!, query);
						if (!result.queued_turn_id) {
							refused = true;
							throw new Error('This worker needs an update to compact and send. Your draft has been kept.');
						}
						pendingQueue.acknowledgeSend(_state.sessionId, clientMessageId);
						clearCompactRetry(_state.sessionId!);
						spendBindingPicks(_state.sessionId);
					} catch (error) {
						// Only a refusal proves the worker did not take it. A lost or
						// unreadable reply, a timeout or a 5xx (504: the owner may have it)
						// may follow acceptance: like an ambiguous WS answer, the send is
						// unconfirmed, not refused. Its chip stays and can be unsent, its
						// words are not handed back (a resend gets a new command id the
						// worker cannot dedupe, so it could run twice), and it stays in the
						// run until its receipt, a snapshot or a withdrawal settles it.
						const status = (error as { status?: unknown } | null)?.status;
						const notTaken = refused || (typeof status === 'number' && status >= 400 && status < 500);
						if (!notTaken) {
							if (!pendingQueue.answerLost(_state.sessionId, clientMessageId)) {
								update(s => s.sessionId === _state.sessionId ? { ...s, error: SEND_MAYBE_APPLIED_MESSAGE } : s);
							}
							return true;
						}
						pendingQueue.acknowledgeSend(_state.sessionId, clientMessageId);
						// Refused: the composer restores the draft, attachments included.
						pendingQueue.forgetSendUploads(clientMessageId);
						// Remove only our optimistic echo, never a newer session's draft
						// or an authoritative queue snapshot that arrived in the meantime.
						update(s => s.sessionId === _state.sessionId ? {
							...retireSends(s, clientMessageId), queuedMessages: s.queuedMessages.filter(m => m.id !== optimisticQueuedId),
							error: error instanceof Error ? error.message : 'Could not compact and send.',
						} : retireSends(s, clientMessageId));
						throw error;
					}
					return true;
				}
				querySent = wsConn.send(query);
				// A new chat's turn lasts until the chat is created: the next send
				// goes to it. Sent unbound too, it would create a second chat.
				letNextGo?.(requestId && querySent ? untilCreated(requestId, clientMessageId) : undefined);
				if (!querySent) {
					pendingQueue.acknowledgeSend(_state.sessionId, clientMessageId);
					pendingQueue.forgetSendUploads(clientMessageId);
					// WS dropped before the frame went out. Roll the optimistic echo back
					// and surface an error so the caller can restore the input.
					//
					// S6: withdrawing the flip restores the AUTHORITATIVE value — what
					// the feed reducer currently says — rather than forcing false. A
					// turn that is genuinely live (this send was a steer into it) keeps
					// its indicator; nothing has to guess.
					feedController.retractOptimisticSend(_state.sessionId, clientMessageId);
					update(s => s.sessionId === _state.sessionId ? {
						...retireSends(s, clientMessageId),
						...(optimisticQueuedId
							? { queuedMessages: s.queuedMessages.filter(m => m.id !== optimisticQueuedId) }
							: {
									messages: s.messages.filter(m => m.commandId !== clientMessageId),
									optimisticStreaming: false,
									isStreaming: feedController.isReducerStreaming(s.sessionId),
								}),
						error: 'Connection lost before the message was sent. Please try again.',
					} : retireSends(s, clientMessageId));
					feedController.clearOptimisticSend(_state.sessionId);
					return false;
				}
				options?.onDispatched?.();
				clearCompactRetry(_state.sessionId!);
				spendBindingPicks(_state.sessionId);
				// The pick is spent: the session now exists and its row carries the
				// ownership, so follow-up sends must not re-assert a target. (Left
				// intact on a failed send so a retry still goes to the chosen device.)
				if (_state.deviceIntent) update(s => ({ ...s, deviceIntent: null }));
				return true;
			} finally {
				if (!querySent) releaseBootSend(bootClaim);
			}
		},

		stopGeneration() {
			const _state = getState();
			let recorded = false;
			// Anything the feed has already shown came before this Stop.
			const pressedAfterSeq = feedController.reducerSeq?.(_state.sessionId) ?? null;
			// The send this Stop is pressed on: the oldest of the run the feed has not
			// receipted. The run holds only sends that may still run (send-run.ts),
			// so a message queued behind a turn the feed has shown is the one that
			// runs once that turn ends, not the send that started it.
			const pressedRun = sendRunIn(_state, _state.sessionId);
			const pressedAt = pressedRun.findIndex(id => !feedController.hasReceipt?.(_state.sessionId, id));
			// If it is retired before the timeout, the next send in the run takes its place.
			const pressedOn = pressedAt < 0 ? [] : pressedRun.slice(pressedAt);
			if (_state.sessionId) {
				// The interrupt is always a full stop (the query-scoped SDK interrupt
				// tears down in-flight background sub-agents too).
				// #585: carry the live turn id so the backend binds the stop to that
				// turn — a stop that lands after its turn settled then no-ops instead
				// of interrupting the successor. Omitted when the FE knows no live turn
				// (legacy session-scoped stop server-side).
				recorded = wsConn.send({
					type: 'stop',
					session_id: _state.sessionId,
					...(_state.liveTurnId ? { turn_id: _state.liveTurnId } : {}),
				});
				// Only a Stop that left this page can have refused anything.
				if (recorded) update(s => ({
					...s,
					stopRequestedTurnId: s.liveTurnId ?? STOP_TURN_NOT_NAMED,
					stopRequestedPressedOn: pressedOn,
					stopTimedOutTurnId: keepStopRecovery(s.stopTimedOutTurnId, s.liveTurnId),
				}));
			} else if (internal.awaitingNewSession) {
				// Brand-new session whose id the backend hasn't minted yet, so we
				// have nothing to address the stop to. Defer it: the session_created
				// / start handlers send the stop once the id arrives, so the backend
				// can discard the orphan turn instead of silently dropping the click.
				internal.stopRequestedBeforeSession = true;
				recorded = true;
				update(s => ({ ...s, stopRequestedTurnId: STOP_TURN_NOT_NAMED, stopRequestedPressedOn: pressedOn, stopTimedOutTurnId: null }));
			}

			const current = stopTimeout.get();
			if (current) clearTimeout(current);
			stopTimeout.set(setTimeout(() => {
				const state = getState();
				// A navigation or a started successor is not the Stop still waiting. A
				// Stop that never left the page records nothing, so go by the turn it
				// saw: this recovery is all that frees a page cut off offline, and a
				// feed that has named a newer turn is not cut off.
				if (state.isStreaming
					&& (!_state.sessionId || state.sessionId === _state.sessionId)
					&& (recorded
						? stopPressedOn(state.stopRequestedTurnId, state.liveTurnId)
						: state.liveTurnId === _state.liveTurnId)) {
					console.warn('[ActiveSession] Stop confirmation timeout - forcing UI recovery');
					// Deliberate override, not a competing writer: the stop was never
					// confirmed, so this is the UI's last-resort recovery. Withdraw
					// the optimistic flip FIRST — otherwise the next fold would
					// re-raise isStreaming straight back to true.
					feedController.clearOptimisticSend(getState().sessionId);
					update(s => {
						// A Stop pressed on sends is named only by their receipts (send-run.ts):
						// a live turn none of them started was begun elsewhere. The feed is
						// not cut off, and that turn and its rows are left as they are.
						const unnamed = pressedOn.length > 0 && s.stopRequestedTurnId === STOP_TURN_NOT_NAMED;
						const leftOn = pressedOn.length ? stillPressedOn(s, _state.sessionId, pressedOn) : [];
						if (unnamed && s.liveTurnId) {
							return { ...s, stopTimedOutTurnId: leftOn.length ? STOP_TURN_NOT_NAMED : null,
								stopTimedOutCommandId: leftOn[0] ?? null, stopTimedOutPressedOn: leftOn,
								stopTimedOutAfterSeq: pressedAfterSeq };
						}
						let messages = s.messages.map(m => {
							if (m.type === 'tool_use' && m.toolStatus === 'running') return { ...m, streaming: false, toolStatus: 'error' as const };
							return m.streaming ? { ...m, streaming: false } : m;
						});
						messages = messages.filter(m => !(m.type === 'text' && m.role === 'assistant' && !m.content));
						// A Stop still waiting for the feed to name a turn only times out
						// while the turn of the send it was pressed on has not shown up.
						// Steered and row-less sends count too (#1114). Without one, a
						// newest user row with no turn yet is that send.
						// A Stop pressed on sends only ever hands off among them (send-run.ts).
						const newestUser = s.messages.findLast(m => m.role === 'user');
						const fallback = newestUser && !newestUser.turnId ? newestUser.commandId ?? null : null;
						const left = pressedOn.length ? stillPressedOn(s, _state.sessionId, pressedOn)
							: fallback ? [fallback] : [];
						const turn = unnamed ? STOP_TURN_NOT_NAMED : s.liveTurnId ?? s.stopRequestedTurnId ?? STOP_TURN_NOT_NAMED;
						// Every send it was pressed on was retired while it waited: none of
						// them will run, so there is no turn of this page's to recover.
						const nothingLeft = pressedOn.length > 0 && !left.length && turn === STOP_TURN_NOT_NAMED;
						return { ...s, messages, isStreaming: false, optimisticStreaming: false,
							stopTimedOutTurnId: nothingLeft ? null : turn,
							stopTimedOutCommandId: left[0] ?? null,
							stopTimedOutPressedOn: left,
							stopTimedOutAfterSeq: pressedAfterSeq };
					});
				}
				stopTimeout.set(null);
			}, 10000));
		},

		/** Create the staged branch session. Resolves to the new session id once
		 *  this view adopted it, or null (the reason is in `worktreeError` or
		 *  `error`, or the user navigated away). With a prompt, null also means
		 *  the session was created but its first message was refused. */
		async createWorktree(prompt?: string, pendingUploadPaths?: string[], creation?: Creation): Promise<string | null> {
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

			claimNewSessionWorker();
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

				// Connect BEFORE binding: the bind and the first message's
				// optimistic row (sendMessage below) must land in the same frame.
				// Bound with no row, the chat reads as a settled empty session —
				// the centred "cleared" composer — for as long as the socket takes.
				if (prompt && !wsConn.isConnected()) {
					wsConn.connect();
					await waitForConnectionVoid();
				}

				if (prompt && creationGen !== internal.worktreeCreationGeneration) {
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
					// The POST created the session but carried no prompt, so it had no
					// turn to attach uploads to — this follow-up query is that turn.
					// Locally the files are already in the checkout and the backend
					// ignores the list; on a remote target it lifts them into blobs.
					// Its refusal is the send's refusal: the composer keeps the text.
					const sent = await getActiveSession().sendMessage(prompt, {
						skipWorktreeSetup: true,
						pendingUploadPaths,
						creation,
					});
					// This view bound the session itself, so nothing else opens its
					// feed: the follow-up query lands as a queued turn, whose WS
					// `start` may never reach this device. Without the feed the
					// reply only appeared when some later reload opened it, after
					// the turn had finished — and a turn folded whole in one batch
					// never clears the optimistic streaming flag, so the chat sat
					// on "working" for good. Start it now, after the optimistic row
					// exists, as autoCreateWorktree does (FIX B in feed-controller).
					if (creationGen === internal.worktreeCreationGeneration
						&& getState().sessionId === response.session_id
						&& !feedController.isFeeding(response.session_id)) {
						feedController.start(response.session_id);
					}
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
		async autoCreateWorktree(prompt: string, pendingUploadPaths?: string[], creation?: Creation): Promise<boolean> {
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
			// This send's own command identity. The server stamps it on the first
			// turn, so the delivered `message.user` reconciles onto the local row by
			// id: its text names the checkout's copy of each upload, not the staging
			// path this row shows, so a text match cannot find it (#1351).
			const clientMessageId = newLocalPendingId();

			claimNewSessionWorker();
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
						commandId: clientMessageId,
						timestamp: new Date().toISOString(),
						...uploadsMetadata(pendingUploadPaths),
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
					client_message_id: clientMessageId,
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

				const sent = await getActiveSession().sendMessage(prompt, { skipWorktreeSetup: true, skipMessageAppend: true, creation });
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

		/** Request withdrawal; the verdict handler restores the draft on success. */
		removeQueuedMessage(id: string): string | null {
			const _state = getState();
			const entry = _state.queuedMessages.find(m => m.id === id);
			if (!entry) return null;
			if (entry.status) return null;
			// Already taken to run (#1052): the queue no longer has it to withdraw.
			if (pendingQueue.isHeld(_state.sessionId, entry)) return null;
			const key = pendingQueue.requestRemoval(_state.sessionId, entry, () => {
				update(s => s.sessionId === _state.sessionId ? { ...s,
					queuedMessages: s.queuedMessages.map(m => (m.clientMessageId ?? m.id) === (entry.clientMessageId ?? entry.id) ? { ...m, status: undefined } : m),
					error: 'Cancellation was not confirmed. The message has not been withdrawn. Please try again.',
				} : s);
			});
			const sent = wsConn.send({
				type: 'remove_queued_message',
				message_id: entry.id,
				client_message_id: entry.clientMessageId,
				session_id: _state.sessionId,
			});
			if (!sent) {
				pendingQueue.removalFailed(_state.sessionId, key);
				update(s => ({ ...s, error: 'Connection lost before cancellation was sent. Please try again.' }));
				return null;
			}
			update(s => ({ ...s, queuedMessages: pendingQueue.reconcile(s.sessionId, s.queuedMessages) }));
			return null; // restore the draft only after a withdrawn verdict
		},
	};

	// Every send, whichever caller started it, holds off an update reload
	// until it settles (see trackSend), and is written after every earlier
	// send to its chat (send-order.ts, #1444). A send with no earlier one open
	// goes on without an await, exactly as before.

	/** Ends once the new chat this send creates is adopted, or its creation
	 *  ended another way: refused (its echo withdrawn), replaced, or the user
	 *  left the compose. Never on time alone. Looked at every 50 ms, and at
	 *  once whenever a send is made or reaches its turn in the compose. */
	function untilCreated(requestId: string, commandId: string): CreationEnd {
		const navigation = getInitGeneration();
		const echoed = () => getState().messages.some(m => m.commandId === commandId);
		const watchEcho = echoed();
		let resolve!: () => void;
		const done = new Promise<void>(r => { resolve = r; });
		let over = false;
		const ended = () => {
			if (!over && (getState().sessionId || internal.pendingRequestId !== requestId || !internal.awaitingNewSession
				|| getInitGeneration() !== navigation || (watchEcho && !echoed()))) {
				over = true;
				clearInterval(poll);
				resolve();
			}
			return over;
		};
		const poll = setInterval(ended, 50);
		return { ended, done };
	}

	/** A new chat's compose, shared by its sends: the chat creation one of
	 *  them started (pending), and how many of its creations ended without a
	 *  chat (failed). A creation that ended with one is the chat now open. */
	type Compose = { lane: string; creating: CreationEnd | null; failed: number; chat?: string };
	let compose: Compose | null = null;
	/** Earlier navigations' composes that adopted a chat, while a send of
	 *  theirs is still open: that chat's sends keep going in their lane. */
	let adopted: Compose[] = [];
	function settle(c: Compose): number {
		if (c.creating?.ended()) {
			if (!getState().sessionId) c.failed++;
			c.creating = null;
		}
		return c.failed;
	}

	type Taken = {
		lane: SendLane; chat: string | null | typeof ANY_CHAT; navigation: number; turn: SendTurn;
		/** The compose this send waits in, and its failed count when made. */
		compose?: Compose; failed?: number;
	};
	function takeTurn(): Taken {
		const made = getState();
		const navigation = getInitGeneration();
		if (made.isRestoringSession) return { lane: ANY_CHAT, chat: ANY_CHAT, navigation, turn: sendOrder.take(ANY_CHAT) };
		const lane = `compose:${navigation}`;
		if (compose?.lane !== lane) {
			if (compose?.chat) adopted.push(compose);
			compose = { lane, creating: null, failed: 0 };
		}
		adopted = adopted.filter(c => sendOrder.isOpen(c.lane));
		// A chat adopted from a compose stays in its lane while a send of the
		// compose is open, even after the user left and reopened that chat: a
		// send queued there before the id existed keeps its place ahead of one
		// made after.
		if (made.sessionId) {
			const from = adopted.find(c => c.chat === made.sessionId)?.lane;
			if (from) return { lane: from, chat: made.sessionId, navigation, turn: sendOrder.take(from) };
			const chat = `chat:${made.sessionId}`;
			if (!sendOrder.isOpen(lane)) return { lane: chat, chat: made.sessionId, navigation, turn: sendOrder.take(chat) };
			compose.chat = made.sessionId;
		}
		return { lane, chat: made.sessionId, navigation, compose, failed: settle(compose), turn: sendOrder.take(lane) };
	}

	/** Was the chat the send was made in left while it waited? Then nothing
	 *  goes, no error lands in the chat open now, and the caller keeps the
	 *  words. A new chat's compose counts as left on navigation only: its
	 *  adoption is the same chat. */
	function left({ chat, navigation }: Taken): boolean {
		if (chat === ANY_CHAT) return false;
		if (chat !== null) return getState().sessionId !== chat;
		return getInitGeneration() !== navigation;
	}

	/** Waits for the earlier turns, at most SEND_ORDER_WAIT_MS. */
	function inTime(ready: Promise<void>): Promise<boolean> {
		return new Promise<boolean>((resolve) => {
			const limit = setTimeout(() => resolve(false), SEND_ORDER_WAIT_MS);
			void ready.then(() => { clearTimeout(limit); resolve(true); });
		});
	}

	const reserved = new WeakMap<SendReservation, Taken>();
	/** Take a send's place in line now, for a caller that awaits before it
	 *  sends (the composer, a generation choice). Pass it as `options.turn`;
	 *  cancel it on every path that sends nothing. It never lapses: a send made
	 *  later waits for it, at most SEND_ORDER_WAIT_MS. */
	function reserveSend(): SendReservation {
		const taken = takeTurn();
		const reservation: SendReservation = { cancel() { if (reserved.delete(reservation)) taken.turn.release(); } };
		reserved.set(reservation, taken);
		return reservation;
	}

	const send = sender.sendMessage;
	sender.sendMessage = (prompt: string, options?: SendMessageOptions): Promise<boolean> => {
		// A worktree create's own first send: part of the creating send,
		// inside its turn, which lasts while this one's chat is created.
		const creation = options?.skipWorktreeSetup ? options.creation : undefined;
		if (creation) {
			return trackSend(() => send(prompt, options, (after) => { if (after) creation.holdUntil(after); }));
		}
		// A send goes only on a place in line, used once. A caller with no
		// await before it takes one now. A cancelled or used one sends
		// nothing: that submission was called off, and its caller keeps the
		// words as on any refusal.
		const reservation = options?.turn ?? reserveSend();
		const taken = reserved.get(reservation);
		if (!taken) return Promise.resolve(false);
		reserved.delete(reservation);
		const { turn } = taken;
		// What this send's turn still waits for once it returns (a new
		// chat's creation). Written with nothing to wait for, it ends now.
		const holds: Promise<void>[] = [];
		const letNextGo = (after?: CreationEnd) => {
			if (after) {
				if (taken.compose) taken.compose.creating = after;
				holds.push(after.done);
			} else if (!holds.length) turn.release();
		};
		return trackSend(async () => {
			try {
				if (turn.ready) {
					if (!(await inTime(turn.ready))) {
						// An earlier send to this chat has not ended: this one never
						// goes ahead of it. Refused, the caller keeps the words.
						if (!left(taken)) {
							update(s => ({ ...s, error: taken.chat === null
								? 'This session is still being created — try again in a moment.'
								: 'An earlier message to this chat is still being sent. This one was not sent; try again in a moment.' }));
						}
						return false;
					}
					if (left(taken)) return false;
					// The chat this compose was creating, read now: sent to it once
					// it exists; refused if that creation failed after this send
					// was made, never sent as a second create.
					if (taken.compose && settle(taken.compose) > taken.failed!) return false;
				}
				// The chat this compose adopted, for its sends made after a leave.
				if (taken.compose && getState().sessionId) taken.compose.chat ??= getState().sessionId!;
				return await send(prompt, options, letNextGo);
			} finally {
				if (holds.length) void Promise.allSettled(holds).then(turn.release);
				else turn.release();
			}
		});
	};
	return Object.assign(sender, { reserveSend });
}

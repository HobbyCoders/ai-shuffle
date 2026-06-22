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
		loadSession: (sessionId: string) => Promise<PollAppliedData | null>;
		startStreaming: (sessionId: string) => void;
		stop: (sessionId: string) => void;
		stopAll: () => void;
		isPolling: (sessionId: string) => boolean;
	};
	sessionState: {
		getSession: (sessionId: string) => { messages: ChatMessage[] } | undefined;
		removeSession: (sessionId: string) => void;
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
	messageCache: {
		get: (sessionId: string) => Promise<ChatMessage[] | null>;
		set: (sessionId: string, messages: ChatMessage[]) => void;
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

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createSessionInit(deps: SessionInitDeps) {
	const {
		update, getState, notifySubscribers, defaultState, setState,
		internal, sessionPoller, sessionState,
		wsConn, switcher, messageCache, appData,
		disconnect: disconnectFn, persistSession, persistTimer,
	} = deps;

	// ─── Poll callback ───────────────────────────────────────────────
	// Direct callback from the poller → activeSession, replacing the old bridge
	// subscription pattern. One _update() call per poll cycle — no double-update
	// risk that corrupted Svelte 5's effect linked-list.

	function setupPollCallback() {
		sessionPoller.onPollApplied((sessionId, data) => {
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
				const clientOnlyEntries: Array<{msg: ChatMessage; anchorId: string | null}> = [];
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
						clientOnlyEntries.push({ msg: m, anchorId });
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
					clientOnlyEntries.push({ msg: m, anchorId });
				}
				if (clientOnlyEntries.length > 0) {
					finalMessages = [...finalMessages];
					const idToIndex = new Map<string, number>();
					for (let i = 0; i < finalMessages.length; i++) {
						if (finalMessages[i].id) idToIndex.set(finalMessages[i].id, i);
					}
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
				if (data.isStreaming) {
					let pollerStreamIdx = -1;
					for (let i = finalMessages.length - 1; i >= 0; i--) {
						if (finalMessages[i].role === 'assistant' && finalMessages[i].type === 'text' && finalMessages[i].streaming) {
							pollerStreamIdx = i;
							break;
						}
					}

					if (pollerStreamIdx >= 0 && finalWsContent) {
						const pollerContent = finalMessages[pollerStreamIdx].content || '';
						if (finalWsContent.length > pollerContent.length) {
							finalMessages = [...finalMessages];
							finalMessages[pollerStreamIdx] = { ...finalMessages[pollerStreamIdx], content: finalWsContent };
						}
					} else if (pollerStreamIdx < 0) {
						const currentSynthetic = s.messages.find(
							(m: ChatMessage) => m.role === 'assistant' && m.streaming && m.type === 'text'
						);
						if (currentSynthetic) {
							const alreadyFinalized = finalMessages.some(
								(m: ChatMessage) => m.role === 'assistant' && m.type === 'text' && !m.streaming &&
									m.content === currentSynthetic.content
							);
							if (!alreadyFinalized) {
								finalMessages = [...finalMessages, {
									...currentSynthetic,
									content: finalWsContent || currentSynthetic.content,
								}];
							}
						}
					} else {
						finalWsContent = '';
					}
				} else {
					finalWsContent = '';
				}

				// ── Phase 3: Stabilize message identity ────────────────────
				if (s.messages.length > 0 && finalMessages.length > 0) {
					const existingById = new Map<string, ChatMessage>();
					for (const m of s.messages) {
						if (m.id) existingById.set(m.id, m);
					}

					let needsStabilization = false;
					const stabilized = finalMessages.map((msg, i) => {
						let existing = existingById.get(msg.id);

						if (!existing) {
							const positional = s.messages[i];
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

						if (!existing) return msg;

						if (msg.type === 'subagent') {
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
								return existing;
							}
							if (existing.id !== msg.id) {
								needsStabilization = true;
								return { ...msg, id: existing.id };
							}
							return msg;
						}

						if (
							existing.content === msg.content &&
							existing.streaming === msg.streaming &&
							existing.toolResult === msg.toolResult &&
							existing.toolStatus === msg.toolStatus
						) {
							if (existing !== msg) needsStabilization = true;
							return existing;
						}
						if (existing.id !== msg.id) {
							// eslint-disable-next-line no-console
							if (STREAM_DEBUG) console.log(`[AS] phase3 id-swap from=${msg.id} to=${existing.id} role=${msg.role} type=${msg.type} "${(msg.content || '').slice(0, 30)}"`);
							needsStabilization = true;
							return { ...msg, id: existing.id };
						}
						return msg;
					});
					if (needsStabilization) finalMessages = stabilized;
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

			const finalSession = sessionState.getSession(sessionId);
			if (finalSession && finalSession.messages.length > 0) {
				messageCache.set(sessionId, finalSession.messages);
			}
		});
	}

	// ─── init ────────────────────────────────────────────────────────

	async function init() {
		const initGen = bumpInitGeneration();
		setupOnStreamEnd();
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
						sessionId: persisted.sessionId,
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

					if (persisted.sessionId) {
						const cached = await messageCache.get(persisted.sessionId);

						if (initGen !== getInitGeneration()) {
							console.debug(`[ActiveSession] init(): stale after messageCache (gen ${initGen} < ${getInitGeneration()}), bailing`);
							return;
						}

						console.debug(`[ActiveSession] init(): cached messages for ${persisted.sessionId}: ${cached?.length ?? 0}`);
						if (cached && cached.length > 0) {
							update(s => ({ ...s, messages: cached }));
						}
					}
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

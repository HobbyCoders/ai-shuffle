/**
 * Session Init — Initialization and destruction lifecycle.
 *
 * Handles:
 * - init() / destroy() lifecycle
 * - Session-restore guards (sessionStorage-based)
 *
 * Plain .ts file — no $state/$derived/$effect runes (rune-free so it can be imported before component init).
 */

import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
// V2 Phase 1: the SSE-fed reducer owns the transcript; destroy() tears its feed down.
import { feedController } from './v2/feed-controller';
import { TAB_ACTIVE_SESSION_KEY } from '../device-id';
import { resolvePersistedProject } from './session-project';
import { normalizeSessionMode } from '../sessionMode.svelte';
import { DEFAULT_CONTEXT_WINDOW_CHOICE } from '$lib/constants/model-context';
import { ALL_BINDING_UNHYDRATED, restoreComposerBinding } from './composer-binding';

// ─── Interfaces ──────────────────────────────────────────────────────────────

export interface SessionInitDeps {
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	getState: () => ActiveSessionState;
	notifySubscribers: () => void;
	defaultState: () => ActiveSessionState;
	setState: (s: ActiveSessionState) => void;
	internal: InternalMutableState;

	// External modules
	wsConn: {
		activate: () => void;
		connect: () => void;
		disconnect: () => void;
		destroy: () => void;
	};
	switcher: {
		loadSelectionFromBackend: () => Promise<{ selectedProfile?: string; selectedProject?: string } | null>;
		loadPersistedSession: () => Promise<import('../active-session').PersistedSession | null>;
		getPersistedProfile: () => string;
		getPersistedProject: () => string;
		getPersistedWorktreeIntent: (projectId:string, projectV2Id:string | null) => 'local' | 'worktree';
		refreshSessionMetadata: (sessionId: string) => Promise<void>;
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

function getSkipRestore(): boolean {
	if (typeof window === 'undefined') return false;
	return sessionStorage.getItem(SKIP_RESTORE_KEY) === '1';
}

/**
 * Whether this page load is going to reopen a chat: a `/?session=` link, or
 * this tab's own saved chat (and no "New chat" told it to stay new). Read
 * synchronously, so the store starts in the restore window and the first frame
 * after a reload shows that chat loading. Waiting for init's first round trip
 * painted the new-chat screen for up to 1.5s first (found by the
 * reload-midstream journey). init clears the window on every path that ends
 * up restoring nothing.
 */
export function tabWillRestoreChat(): boolean {
	if (typeof window === 'undefined') return false;
	try {
		if (new URLSearchParams(window.location.search).get('session')) return true;
		if (getSkipRestore()) return false;
		const raw = sessionStorage.getItem(TAB_ACTIVE_SESSION_KEY);
		const saved: unknown = raw ? JSON.parse(raw) : null;
		return !!(saved && typeof saved === 'object' && (saved as { sessionId?: unknown }).sessionId);
	} catch {
		return false;
	}
}

export function setSkipRestore(value: boolean) {
	if (typeof window === 'undefined') return;
	if (value) {
		sessionStorage.setItem(SKIP_RESTORE_KEY, '1');
	} else {
		sessionStorage.removeItem(SKIP_RESTORE_KEY);
	}
}

/** Navigation epoch: session adoption during a send does not advance it. */
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

// A send can create its chat before init's preferences arrive (#821). Keep
// this claim separate from the navigation epoch: adopting one's own new chat
// is NOT navigation (ChatInput uses that distinction for draft recovery and
// collapsing the phone composer). AppPage retires it after both boot reconciles.
let bootSend: { generation: number; pendingSends: Set<symbol> } | null = null;

export function claimBootSend(): symbol | null {
	if (bootSend?.generation !== getInitGeneration()) return null;
	const claim = Symbol('boot-send');
	bootSend.pendingSends.add(claim);
	return claim;
}

export function releaseBootSend(claim: symbol | null): void {
	if (claim !== null) bootSend?.pendingSends.delete(claim);
}

export function hasBootSendClaim(generation: number): boolean {
	return bootSend?.generation === generation && bootSend.pendingSends.size > 0;
}

export function finishBootReconciliation(generation: number): void {
	if (bootSend?.generation === generation) bootSend = null;
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createSessionInit(deps: SessionInitDeps) {
	const {
		update, getState, notifySubscribers, defaultState, setState,
		wsConn, switcher,
		disconnect: disconnectFn, persistTimer,
	} = deps;

	// ─── init ────────────────────────────────────────────────────────

	/**
	 * @param options.deepLinkSessionId A `/?session=<id>` link this load opens.
	 *   It is adopted as the open session before anything awaits, and the
	 *   tab's previously open chat is not restored at all: otherwise that chat's
	 *   title, sidebar highlight and composer showed (and took sends) until the
	 *   link opened after the first loads.
	 */
	async function init(options: { deepLinkSessionId?: string | null } = {}) {
		const initGen = bumpInitGeneration();
		bootSend = { generation: initGen, pendingSends: new Set() };

		const deepLinkSessionId = options.deepLinkSessionId || null;
		const skipRestore = getSkipRestore() || deepLinkSessionId !== null;

		if (deepLinkSessionId) {
			update(s => ({
				...s,
				sessionId: deepLinkSessionId,
				// Unknown until /live answers; never the previous chat's title.
				title: '',
				messages: [],
				// Same restore window as a reload: the surface shows its loading
				// state and a send waits until the linked chat's history is in.
				isRestoringSession: true,
				// Run binding: the linked chat is an EXISTING session, so it
				// opens on its own row's model, effort and window, exactly as a
				// reload does. The state here is the fresh-tab seed, which holds
				// the last-used compose picks (`aihub_lastModel` and friends);
				// keeping them showed another chat's model in the picker, sized
				// the context meter for it and sent it with the next message.
				// Cleared BEFORE refreshSessionMetadata below snapshots them, so
				// /live treats them as holes and fills them from the session row.
				modelOverride: null,
				permissionModeOverride: null,
				effortLevelOverride: null,
				contextWindowChoice: DEFAULT_CONTEXT_WINDOW_CHOICE,
				contextWindowPin: undefined,
				// Agent, access and Fast are the linked chat's own too (#418): the
				// seed's last-used picks are not, so they wait for /live as
				// placeholders that are neither shown as its binding nor sent.
				profile: '',
				fastMode: false,
				bindingUnhydrated: { ...ALL_BINDING_UNHYDRATED },
				bindingPicks: {},
			}));
			feedController.start(deepLinkSessionId);
			void switcher.refreshSessionMetadata(deepLinkSessionId);
		}

		try {
			// Boot waterfall (finding S5): these two preference reads are
			// INDEPENDENT — the device selection (profile/project) and this tab's
			// persisted session. Awaiting them in series added a whole round trip
			// before anything could be rendered or fetched. The restore read is
			// skipped entirely (not just ignored) when skipRestore is set, so the
			// parallel form costs nothing extra on that path.
			const [backendSelection, persisted] = await Promise.all([
				switcher.loadSelectionFromBackend(),
				skipRestore
					? Promise.resolve(null)
					: switcher.loadPersistedSession(),
			]);

			if (initGen !== getInitGeneration()) {
				return;
			}

			const defaultProfile = backendSelection?.selectedProfile || switcher.getPersistedProfile();
			const defaultProject = backendSelection?.selectedProject || switcher.getPersistedProject();

			if (deepLinkSessionId) {
				// The linked chat's project comes from its own row (/live, then the
				// open after the first loads). A device default written here would
				// read as a pick made during that fetch and block it.
				// Its agent is the chat's own, from /live — not the device default.
				update(s => ({ ...s, profile: s.profile || (s.bindingUnhydrated?.profile ? '' : defaultProfile) }));
			} else if (skipRestore || hasBootSendClaim(initGen)) {
				// A send owns the live chat, not init's saved snapshot. Still fill
				// missing defaults and re-arm the socket below, even after remount.
				update(s => ({
					...s,
					isRestoringSession: false,
					profile: s.profile || defaultProfile,
					// A new session starts without a project (#371 step 4);
					// only a compose that already picked one keeps it.
					...(s.mode === 'chat' ? {} : resolvePersistedProject(
						{ project: s.project, projectV2Id: s.projectV2Id },
						defaultProject,
					)),
				}));
			} else {
				if (persisted) {
					// An existing session's agent, access and Fast: the snapshot's
					// real values, else placeholders /live fills (#418). A snapshot
					// written before that record existed may hold fabricated ones.
					const binding = persisted.sessionId ? restoreComposerBinding(persisted, null) : null;
					update(s => ({
						...s,
						sessionId: persisted.sessionId ?? null,
						// Lane. A restored EXISTING session must NOT inherit the
						// lane the sidepanel happens to be showing: `appMode` is
						// device-wide localStorage, so another tab moving the
						// switcher would repaint this tab's restored code session
						// as chat on reload (dropping the whole context pill bar),
						// or hang the code controls off a restored chat session.
						// The snapshot is the lane that session was actually open
						// in; +page's reconcile re-reads the row once the session
						// list lands and corrects a stale snapshot. An unbound
						// compose is a project session only when it was saved
						// with the project the user picked (#371 step 4).
						mode: persisted.sessionId
							? normalizeSessionMode(persisted.mode)
							: (persisted.project || persisted.projectV2Id ? 'code' : 'chat'),
						// Adopting a sessionId whose history hasn't rendered yet opens
						// the restore-race window: the UI shows "New Chat" while sends
						// would land in the old session. Mark it until onConnected's
						// feed start settles. !! (not `!== null`): a persisted entry
						// missing the key yields undefined, which must not strand the
						// flag as true with nothing to restore.
						isRestoringSession: !!persisted.sessionId,
						title: persisted.title || 'New Chat',
						profile: binding
							? (binding.profile || (binding.bindingUnhydrated.profile ? '' : defaultProfile))
							: (persisted.profile || defaultProfile),
						bindingUnhydrated: binding?.bindingUnhydrated ?? {},
						bindingPicks: binding?.bindingPicks ?? {},
						// An unbound compose saved without a project stays without one,
						// and so does a restored chat: the device default would read
						// as the session's own project, hiding a later attach from
						// reconcile (which then keeps the wrong project and a stale
						// Bypass, #371).
						...(persisted.sessionId && normalizeSessionMode(persisted.mode) === 'chat'
							? { project: persisted.project ?? '', projectV2Id: persisted.projectV2Id ?? null }
							: persisted.sessionId || persisted.project || persisted.projectV2Id
								? resolvePersistedProject(persisted, defaultProject)
								: { project: '', projectV2Id: null }),
						// Run binding. A restored EXISTING session keeps whatever the
						// record holds — null included, which lets the backend fall
						// back to what that session actually last ran on. Only an
						// unbound compose (no sessionId) inherits the seeded
						// last-used pick, so reopening an old chat never reroutes it
						// onto the model the user happened to choose elsewhere.
						modelOverride:
							persisted.modelOverride ?? (persisted.sessionId ? null : s.modelOverride),
						permissionModeOverride: binding
							? binding.permissionModeOverride
							: (persisted.permissionModeOverride ?? s.permissionModeOverride),
						effortLevelOverride:
							persisted.effortLevelOverride ?? (persisted.sessionId ? null : s.effortLevelOverride),
						fastMode: binding ? binding.fastMode : (persisted.fastMode ?? s.fastMode),
						workspaceProjectIds: persisted.workspaceProjectIds ?? [],
						contextWindowChoice:
							persisted.contextWindowChoice ??
							(persisted.sessionId ? DEFAULT_CONTEXT_WINDOW_CHOICE : s.contextWindowChoice),
						worktreeIntent: persisted.sessionId ? persisted.worktreeIntent || 'local'
							: switcher.getPersistedWorktreeIntent(resolvePersistedProject(persisted, defaultProject).project, persisted.projectV2Id ?? null),
						worktreeBranch: persisted.worktreeBranch || '',
						worktreeBaseBranch: persisted.worktreeBaseBranch || 'main',
						worktreeId: persisted.worktreeId ?? null,
						worktreeMode: persisted.worktreeMode || 'new',
						selectedWorktreeBranch: persisted.selectedWorktreeBranch ?? null,
						worktreeDeleted: persisted.worktreeDeleted ?? false,
					}));

					// Start the transcript feed the moment the session id is known
					// (finding S5). The SSE feed is a cookie-authed fetch — it needs
					// no WebSocket at all, yet history used to wait for the WS
					// handshake below because the only start call sat in
					// `onConnected`. Starting here removes that round trip from the
					// critical path for the restored session.
					//
					// The single-feed invariant is preserved: start() is idempotent
					// for the session already feeding, `onConnected` re-checks
					// isFeeding() before starting (and only revives otherwise), and
					// sse-feed itself closes any earlier reader for the same session.
					if (persisted.sessionId) {
						feedController.start(persisted.sessionId);
						void switcher.refreshSessionMetadata(persisted.sessionId);
					}
				} else {
					// No saved compose: a new session starts without a project.
					update(s => ({ ...s, isRestoringSession: false, profile: defaultProfile, ...(s.mode === 'chat' ? {} : { project: defaultProject }) }));
				}
			}

			wsConn.activate();
			wsConn.connect();
		} catch (error) {
			console.error('[ActiveSession] Failed to initialize:', error);
			if (initGen !== getInitGeneration()) return;
			// A live send only vetoes stale restore writes, never error recovery.
			update(s => ({ ...s, isRestoringSession: false }));
			wsConn.activate();
			wsConn.connect();
		}
	}

	// ─── destroy ─────────────────────────────────────────────────────

	function destroy() {
		bootSend = null;
		bumpInitGeneration();
		disconnectFn();
		feedController.stop();
		wsConn.destroy();
		switcher.destroy();
		persistTimer.clear();
		setState(defaultState());
		notifySubscribers();
	}

	return { init, destroy };
}

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
import { resolvePersistedProject } from './session-project';
import { normalizeSessionMode } from '../sessionMode.svelte';
import { DEFAULT_CONTEXT_WINDOW_CHOICE } from '$lib/constants/model-context';

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
				update(s => ({ ...s, profile: s.profile || defaultProfile }));
			} else if (skipRestore) {
				update(s => ({
					...s,
					profile: s.profile || defaultProfile,
					...resolvePersistedProject(
						{ project: s.project, projectV2Id: s.projectV2Id },
						defaultProject,
					),
				}));
			} else {
				if (persisted) {
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
						// compose keeps `appMode`'s pick — there, it IS the truth.
						mode: persisted.sessionId
							? normalizeSessionMode(persisted.mode)
							: s.mode,
						// Adopting a sessionId whose history hasn't rendered yet opens
						// the restore-race window: the UI shows "New Chat" while sends
						// would land in the old session. Mark it until onConnected's
						// feed start settles. !! (not `!== null`): a persisted entry
						// missing the key yields undefined, which must not strand the
						// flag as true with nothing to restore.
						isRestoringSession: !!persisted.sessionId,
						title: persisted.title || 'New Chat',
						profile: persisted.profile || defaultProfile,
						...resolvePersistedProject(persisted, defaultProject),
						// Run binding. A restored EXISTING session keeps whatever the
						// record holds — null included, which lets the backend fall
						// back to what that session actually last ran on. Only an
						// unbound compose (no sessionId) inherits the seeded
						// last-used pick, so reopening an old chat never reroutes it
						// onto the model the user happened to choose elsewhere.
						modelOverride:
							persisted.modelOverride ?? (persisted.sessionId ? null : s.modelOverride),
						permissionModeOverride:
							persisted.permissionModeOverride ?? (persisted.sessionId ? null : s.permissionModeOverride),
						effortLevelOverride:
							persisted.effortLevelOverride ?? (persisted.sessionId ? null : s.effortLevelOverride),
						fastMode:
							persisted.fastMode ?? (persisted.sessionId ? false : s.fastMode),
						workspaceProjectIds: persisted.workspaceProjectIds ?? [],
						contextWindowChoice:
							persisted.contextWindowChoice ??
							(persisted.sessionId ? DEFAULT_CONTEXT_WINDOW_CHOICE : s.contextWindowChoice),
						worktreeIntent: persisted.worktreeIntent || 'local',
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
					update(s => ({ ...s, profile: defaultProfile, project: defaultProject }));
				}
			}

			wsConn.activate();
			wsConn.connect();
		} catch (error) {
			if (initGen !== getInitGeneration()) return;
			console.error('[ActiveSession] Failed to initialize:', error);
			wsConn.activate();
			wsConn.connect();
		}
	}

	// ─── destroy ─────────────────────────────────────────────────────

	function destroy() {
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

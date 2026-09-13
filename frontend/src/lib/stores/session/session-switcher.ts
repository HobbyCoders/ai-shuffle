/**
 * Session Switcher — Session switching, caching, and persistence logic.
 *
 * Architecture (SSE-fed reducer):
 * - The transcript + streaming state load via the SSE feed (feed-controller),
 *   which replays the session event log from_seq=0 on switch-in.
 * - A one-shot `/live` snapshot fills the metadata the feed does not carry
 *   (remote controllability, usage totals,
 *   worktree/cloud fields).
 */

import { apiBase, withAuth } from '$lib/api/runtime';
import type { ActiveSessionState, PersistedSession } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import type { SessionEphemeral } from '../session-cache';
import type { ExecutionMode, Task } from '../types';
import { api } from '$lib/api/client';
import { composerPreferences, resolveComposerProject } from '../composer-preferences';
import { STREAM_DEBUG } from '$lib/utils/debug-log';
import { openHeaderDropdown } from '../header-dropdowns';
import { getOrCreateDeviceId, TAB_ACTIVE_SESSION_KEY } from '../device-id';
import { appData } from '../app-data.svelte';
// V2 Phase 1: SSE-fed reducer store — the transcript authority for a session.
import { feedController } from './v2/feed-controller';
import { resolveNewChatProject, resolveSessionProject } from './session-project';
import { appMode, normalizeSessionMode, type SessionMode } from '../sessionMode.svelte';
import { encodeProviderModel } from '$lib/constants/models.svelte';
import {
	contextChoiceFromWindow,
	DEFAULT_CONTEXT_WINDOW_CHOICE,
	isContextWindowChoice,
	type ContextWindowChoice
} from '$lib/constants/model-context';

// ─── Constants ───────────────────────────────────────────────────────────────

const PERSIST_DEBOUNCE_MS = 500;

// ─── Interfaces ──────────────────────────────────────────────────────────────

export interface SessionSwitcherDeps {
	// Store access
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	getState: () => ActiveSessionState;

	// WebSocket
	wsSend: (data: Record<string, unknown>) => boolean;

	// Shared mutable state
	internal: InternalMutableState;

	// External dependencies
	sessionEphemeralCache: {
		save: (sessionId: string, ephemeral: SessionEphemeral) => void;
		restore: (sessionId: string) => SessionEphemeral | null;
	};

	// Callbacks
	onPersistTimerChanged: (timer: ReturnType<typeof setTimeout> | null) => void;
}

/** Minimal shape of the one-shot `/live` metadata snapshot used on switch-in. */
interface LiveSnapshot {
	usage?: {
		input_tokens?: number;
		output_tokens?: number;
		cache_creation_tokens?: number;
		cache_read_tokens?: number;
	};
	session_meta?: {
		project_id?: string | null;
		project_v2_id?: string | null;
		model?: string | null;
		provider_id?: string | null;
		effort?: string | null;
		workspace_project_ids?: string[];
		title?: string;
		worktree_id?: string | null;
		worktree_branch?: string | null;
		worktree_deleted?: boolean;
	};
	/** Durable background-task view (task registry `get_visible`), already in
	 *  the `Task` wire shape. Used to SEED the chip strip on switch-in. */
	activeTasks?: Task[];
}

/** Worktree metadata from the session list, used to restore worktree state
 * when the ephemeral cache doesn't have an entry for the target session. */
export interface SessionWorktreeInfo {
	worktreeId: string | null;
	worktreeBranch: string;
	worktreeDeleted: boolean;
	worktreeIntent: 'local' | 'worktree';
}

interface SessionSwitcher {
	switchSession: (sessionId: string, projectId?: string | null, worktreeInfo?: SessionWorktreeInfo | null, title?: string | null, projectV2Id?: string | null) => Promise<void>;
	refreshSessionMetadata: (sessionId: string) => Promise<void>;
	newChat: () => boolean;
	persistSession: () => void;
	loadPersistedSession: () => Promise<PersistedSession | null>;
	getPersistedProfile: () => string;
	getPersistedProject: () => string;
	loadSelectionFromBackend: () => Promise<{ selectedProfile: string; selectedProject: string } | null>;
	saveSelectionToBackend: (profile: string, project: string) => void;
	destroy: () => void;
}

// ─── Module-Level Selection Helpers ──────────────────────────────────────────

export function getPersistedProfile(): string {
	return typeof window !== 'undefined' ? localStorage.getItem('aihub_selectedProfile') || '' : '';
}

export function getPersistedProject(): string {
	if (!composerPreferences.current.project.projectV2Id) return typeof window !== 'undefined' ? localStorage.getItem('aihub_selectedProject') || '' : '';
	return resolveComposerProject(composerPreferences.current.project, appData.current.projects, appData.current.clusterProjects).project;
}

/**
 * The last worktree intent (Local vs Worktree) the user toggled, persisted so it
 * survives starting a new session / page reload. Defaults to 'local'.
 *
 * This is a UI *preference* — distinct from a session's own worktreeIntent, which
 * is restored per-session from the persisted/backend session record. Only new
 * chats consult this preference.
 *
 * The localStorage key stays `aihub_executionMode` (unchanged) to avoid a
 * migration — only the in-memory/property naming changed.
 */
export function getPersistedWorktreeIntent(): ExecutionMode {
	if (typeof window !== 'undefined') {
		const stored = localStorage.getItem('aihub_executionMode');
		if (stored === 'worktree') return stored;
	}
	return 'local';
}

export function setPersistedWorktreeIntent(mode: ExecutionMode): void {
	composerPreferences.set('worktreeIntent', mode);
	if (typeof window !== 'undefined') {
		localStorage.setItem('aihub_executionMode', mode);
	}
}

/**
 * The lane an OPEN session belongs to, re-derived from the session list.
 *
 * The boot restore adopts a sessionId from a per-tab snapshot, at a point in
 * init() where no row exists yet — so the snapshot's lane is the best it can
 * do. This closes that loop once the list has loaded: the row is the authority
 * for a session's lane (same ruling switchSession follows), and a snapshot can
 * be stale, since the fallback slot is the DEVICE key and any tab may have
 * written it.
 *
 * `null` means "nothing to defer to" — no session is open, or its row is not in
 * the list (a foreign/filtered/deleted session) — and the caller keeps the mode
 * it already has rather than guessing 'code'.
 */
export function reconciledSessionMode(
	sessionId: string | null,
	sessions: { id: string; mode?: string | null }[],
): SessionMode | null {
	if (!sessionId) return null;
	const row = sessions.find(s => s.id === sessionId);
	if (!row) return null;
	return normalizeSessionMode(row.mode);
}

/**
 * The repository slice of a fresh compose, which depends on the lane it opens in.
 *
 * Chat and voice run projectless — neither surface renders a project picker, a
 * branch picker or the Local/Worktree toggle. Restoring the last code-mode
 * project and worktree intent into one of them would bind a checkout the user
 * has no control to inspect or undo: the first message would quietly cut a
 * branch in a repo they thought they had left behind. So those lanes clear the
 * slice outright and only 'code' consults the remembered picks.
 */
type NewChatModeSlice = Pick<
	ActiveSessionState,
	'project' | 'projectV2Id' | 'worktreeIntent' | 'worktreeId' | 'worktreeBranch' | 'selectedWorktreeBranch'
>;

function newChatModeState(
	mode: SessionMode,
	ephemeral: { project: string; projectV2Id: string | null } | null | undefined,
	fallbackProject: string,
): NewChatModeSlice {
	if (mode === 'chat') {
		return {
			project: '',
			projectV2Id: null,
			worktreeIntent: 'local',
			worktreeId: null,
			worktreeBranch: '',
			selectedWorktreeBranch: null,
		};
	}
	return {
		...resolveNewChatProject(ephemeral, fallbackProject),
		worktreeIntent: getPersistedWorktreeIntent(),
		worktreeId: null,
		worktreeBranch: '',
		selectedWorktreeBranch: null,
	};
}

/**
 * Last-used run binding — the model, effort and permission mode the user last
 * picked in the composer.
 *
 * Agents no longer pin any of these: a session's binding is chosen when the
 * session is created and then lives on its row. A brand-new chat has no row
 * yet, so it opens on whatever the user picked last, and falls back to the
 * registry default only on a first-ever run. Like the worktree intent above,
 * this is a UI *preference*: an existing session always restores its OWN
 * binding and never consults these.
 */
export function getPersistedModel(): string | null {
	if (typeof window === 'undefined') return null;
	return localStorage.getItem('aihub_lastModel') || null;
}

/** A worker stores a bare model id alongside its provider; the picker uses both. */
function sessionModelBinding(row: { model?: string | null; provider_id?: string | null } | undefined): string | null {
	const model = row?.model;
	if (!model) return null;
	return row?.provider_id && !model.startsWith('prov:')
		? encodeProviderModel(row.provider_id, model)
		: model;
}

export function getPersistedEffort(): string | null {
	if (typeof window === 'undefined') return null;
	return localStorage.getItem('aihub_lastEffort') || null;
}

export function normalizePermissionMode(mode: string | null | undefined): string | null {
	return mode === 'plan' ? 'default' : mode ?? null;
}

export function getPersistedPermissionMode(): string | null {
	if (typeof window === 'undefined') return null;
	return normalizePermissionMode(localStorage.getItem('aihub_lastPermissionMode'));
}

/** Last-used fast-tier pick. Stored as `'1'` / absent, so anything else is off. */
export function getPersistedFastMode(): boolean {
	if (typeof window === 'undefined') return false;
	return localStorage.getItem('aihub_lastFastMode') === '1';
}

export function getPersistedContextWindowChoice(): ContextWindowChoice {
	if (typeof window === 'undefined') return DEFAULT_CONTEXT_WINDOW_CHOICE;
	const stored = localStorage.getItem('aihub_lastContextWindowChoice');
	return isContextWindowChoice(stored) ? stored : DEFAULT_CONTEXT_WINDOW_CHOICE;
}

/** Remember one leg of the run binding. `null` clears it. */
export function setPersistedRunBinding(
	key: 'model' | 'effort' | 'permissionMode' | 'contextWindowChoice' | 'fastMode',
	value: string | null
): void {
	composerPreferences.set(key, value);
	if (typeof window === 'undefined') return;
	const storageKey = {
		model: 'aihub_lastModel',
		effort: 'aihub_lastEffort',
		permissionMode: 'aihub_lastPermissionMode',
		contextWindowChoice: 'aihub_lastContextWindowChoice',
		fastMode: 'aihub_lastFastMode'
	}[key];
	if (value) localStorage.setItem(storageKey, value);
	else localStorage.removeItem(storageKey);
}

async function loadSelectionFromBackend(): Promise<{ selectedProfile: string; selectedProject: string } | null> {
 await composerPreferences.hydrate();
 return {selectedProfile: getPersistedProfile(), selectedProject: getPersistedProject()};
}
/**
 * Which snapshot fields may still be applied (finding W7).
 *
 * The `/live` snapshot and the SSE feed race on switch-in and the apply used
 * to be last-write-wins, guarded only by switch generation + session id. A
 * slow snapshot landing after a fresher `turn.usage` overwrote `contextUsed`
 * and the token counters with older DB values — counters visibly jumping
 * backwards mid-turn.
 *
 * Rather than special-casing fields ad hoc, the rule is stated once: the feed
 * OWNS a field group as soon as it has published values for it, and a
 * snapshot may only fill groups the feed does not own yet.
 *
 *   - usage/context totals: owned the moment the feed folds any `turn.usage`.
 *   - title: owned by any source that already replaced the placeholder — the
 *     sessions row, a WS `session_updated`, an inline rename. The snapshot
 *     may only fill in a title that is still the 'New Chat' placeholder
 *     (which is exactly the deep-link / notification-open case it exists for).
 *   - worktree/cloud fields: the feed never carries them, so the snapshot is
 *     authoritative and they always apply.
 *   - activeTasks: the feed owns the strip as soon as it has painted anything
 *     on it. The snapshot is a FLOOR, not a correction — it exists so a
 *     background task that is genuinely running right now (task registry
 *     truth) still shows after a switch-in when the session's event log has no
 *     running `task.updated` row to replay, which is every task started before
 *     the durable start row existed. Once the strip is non-empty the feed's
 *     view is the fresher one and the snapshot must not touch it.
 *
 * Pure and exported so the precedence is unit-testable on its own.
 */
export function gateSnapshotPatch(
	patch: Partial<ActiveSessionState>,
	context: { feedOwnsUsage: boolean; currentTitle: string; currentTaskCount?: number }
): Partial<ActiveSessionState> {
	const gated: Partial<ActiveSessionState> = { ...patch };
	if (context.feedOwnsUsage) {
		delete gated.totalTokensIn;
		delete gated.totalTokensOut;
		delete gated.totalCacheCreationTokens;
		delete gated.totalCacheReadTokens;
		delete gated.contextUsed;
	}
	if (context.currentTitle && context.currentTitle !== 'New Chat') {
		delete gated.title;
	}
	if ((context.currentTaskCount ?? 0) > 0) {
		delete gated.activeTasks;
	}
	return gated;
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createSessionSwitcher(deps: SessionSwitcherDeps): SessionSwitcher {
	const { update, getState, wsSend, internal, sessionEphemeralCache, onPersistTimerChanged } = deps;

	let persistTimer: ReturnType<typeof setTimeout> | null = null;
	let _switchGeneration = 0;

	// ── Helpers ──────────────────────────────────────────────────────────

	function buildEphemeral(state: ActiveSessionState): SessionEphemeral {
		return {
			profile: state.profile,
			title: state.title,
			draft: state.draft,
			scrollTop: state.scrollTop,
			project: state.project,
			projectV2Id: state.projectV2Id,
			modelOverride: state.modelOverride,
			permissionModeOverride: state.permissionModeOverride,
			effortLevelOverride: state.effortLevelOverride,
			fastMode: state.fastMode,
			workspaceProjectIds: state.workspaceProjectIds ?? [],
			contextWindowChoice: state.contextWindowChoice,
			worktreeIntent: state.worktreeIntent,
			deviceIntent: state.deviceIntent,
			worktreeId: state.worktreeId,
			worktreeBranch: state.worktreeBranch,
			worktreeBaseBranch: state.worktreeBaseBranch,
			selectedWorktreeBranch: state.selectedWorktreeBranch,
			worktreeMode: state.worktreeMode,
			worktreeCreateNewBranch: state.worktreeCreateNewBranch,
			worktreeDeleted: state.worktreeDeleted,
		};
	}

	function saveSelectionToBackend(profile: string, project: string) {
		composerPreferences.set('profile', profile);
		const v2 = appData.current.clusterProjects.find(p => p.local_project_id === project)?.project_v2_id ?? getState().projectV2Id;
		composerPreferences.set('project', {projectId:project, projectV2Id:v2});
	}
	// ── Persistence ─────────────────────────────────────────────────────

	/** The active-session snapshot persisted to both the server (device key) and
	 * this tab's sessionStorage slot. */
	function buildPersistValue(): PersistedSession {
		const state = getState();
		return {
			sessionId: state.sessionId,
			// The lane this surface is in, so a reload paints the right one on the
			// first frame instead of whatever lane the sidepanel happens to be
			// showing on this device (see PersistedSession).
			mode: state.mode,
			title: state.title,
			profile: state.profile,
			project: state.project,
			projectV2Id: state.projectV2Id,
			modelOverride: state.modelOverride,
			permissionModeOverride: state.permissionModeOverride,
			effortLevelOverride: state.effortLevelOverride,
			fastMode: state.fastMode,
			workspaceProjectIds: state.workspaceProjectIds ?? [],
			contextWindowChoice: state.contextWindowChoice,
			worktreeIntent: state.worktreeIntent,
			worktreeId: state.worktreeId,
			worktreeBranch: state.worktreeBranch,
			worktreeBaseBranch: state.worktreeBaseBranch,
			selectedWorktreeBranch: state.selectedWorktreeBranch,
			worktreeMode: state.worktreeMode,
			worktreeDeleted: state.worktreeDeleted,
		} satisfies PersistedSession;
	}

	/** Server key stays device-scoped (unchanged): one row per device, updated in
	 * place on every persist — so a new tab / new device restores the device's
	 * most recent session. */
	function serverPrefKey(): string {
		return `active_session:${getOrCreateDeviceId()}`;
	}

	async function doPersist() {
		const value = buildPersistValue();
		// (a) Tab-local slot: sessionStorage is per-tab, so reloading THIS tab
		//     restores THIS tab's session even when another tab wrote the server
		//     device key afterwards. No server row, no tab id in the key.
		try {
			if (typeof sessionStorage !== 'undefined') {
				sessionStorage.setItem(TAB_ACTIVE_SESSION_KEY, JSON.stringify(value));
			}
		} catch {
			// sessionStorage unavailable / full — server key still covers restore.
		}
		// (b) Server device key: unchanged behavior (debounce/keepalive intact).
		try {
			const prefKey = serverPrefKey();
			await fetch(`${apiBase()}/preferences/${encodeURIComponent(prefKey)}`, withAuth({
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ key: prefKey, value }),
			}));
		} catch (e) {
			console.error('[ActiveSession] Failed to persist session:', e);
		}
	}

	function persistSession() {
		if (persistTimer) clearTimeout(persistTimer);
		persistTimer = setTimeout(doPersist, PERSIST_DEBOUNCE_MS);
		onPersistTimerChanged(persistTimer);
	}

	function persistSessionImmediate() {
		if (persistTimer) clearTimeout(persistTimer);
		persistTimer = null;
		onPersistTimerChanged(persistTimer);
		doPersist();
	}

	async function loadPersistedSession(): Promise<PersistedSession | null> {
		// Prefer THIS tab's own sessionStorage slot so two tabs no longer clobber
		// each other on reload. A brand-new tab (or new device) has no slot yet, so
		// fall back to the server device key — existing users keep restoring the
		// device's most recent session instead of landing on an empty one.
		try {
			if (typeof sessionStorage !== 'undefined') {
				const raw = sessionStorage.getItem(TAB_ACTIVE_SESSION_KEY);
				if (raw) {
					const value = JSON.parse(raw) as PersistedSession;
					if (value && typeof value === 'object') return value;
				}
			}
		} catch {
			// Absent or corrupt tab-local slot — fall through to the server key.
		}
		try {
			const prefKey = serverPrefKey();
			const response = await api.get<{ key: string; value: PersistedSession } | null>(`/preferences/${encodeURIComponent(prefKey)}`);
			if (response?.value) return response.value;
		} catch {
			// No persisted session — fresh start
		}
		return null;
	}

	// ── Session Navigation ──────────────────────────────────────────────

	async function switchSession(sessionId: string, projectId?: string | null, worktreeInfo?: SessionWorktreeInfo | null, title?: string | null, projectV2Id?: string | null) {
		const generation = ++_switchGeneration;
		const state = getState();
		// The header stays mounted across sessions; its popovers must not follow.
		if (state.sessionId !== sessionId) openHeaderDropdown.set(null);
		// eslint-disable-next-line no-console
		if (STREAM_DEBUG) console.log(`[AS] switchSession CALLED gen=${generation} from=${state.sessionId} to=${sessionId} visibility=${typeof document !== 'undefined' ? document.visibilityState : 'n/a'}`);

		// Tear down the active SSE feed for the session we're leaving so a late
		// event can't write into the target session's state.
		if (feedController.isFeeding()) {
			feedController.stop();
		}

		// Reset cross-session internal state
		internal.pendingTools.clear();
		internal.awaitingNewSession = false;
		internal.worktreeCreationGeneration++;
		// NOTE: do NOT abort an in-flight autoCreateWorktree POST on nav —
		// the backend now creates the worktree + session AND kicks off the
		// first query in one request. Aborting would cancel the user's
		// intended work. The generation counter still prevents stale
		// responses from mutating the now-current session.
		internal.worktreeCreateAbort = null;

		// Save ephemeral for session we're leaving
		const leavingKey = state.sessionId || '__new_chat__';
		sessionEphemeralCache.save(leavingKey, buildEphemeral(state));

		const ephemeral = sessionEphemeralCache.restore(sessionId);
		const row = appData.current.sessions.find((s) => s.id === sessionId);
		const { project: targetProject, projectV2Id: targetV2 } = resolveSessionProject({
			projectId: projectId !== undefined ? projectId : row?.project_id,
			projectV2Id: projectV2Id !== undefined ? projectV2Id : (row?.project_v2_id ?? undefined),
			ephemeralProject: ephemeral?.project,
			ephemeralProjectV2Id: ephemeral?.projectV2Id,
		});

		// Single update: set sessionId, reset + restore ephemeral state.
		// This is one _update call instead of the previous two, preventing
		// Svelte 5 double-update issues.
		//
		// IMPORTANT: Worktree state fallback uses worktreeInfo (from session list)
		// then safe defaults — NEVER s.currentValue. Using s.currentValue caused
		// worktreeDeleted to bleed between sessions (the leaving session's state
		// would infect the target session when no ephemeral cache existed).
		// Resolve title (finding S10). Server-derived values win over the ephemeral
		// cache, which is written from whatever this tab last displayed and can
		// easily hold a stale placeholder — 'New Chat' saved before the backend
		// backfilled the real title, which then flashed until /live corrected it.
		// A cached 'New Chat' is therefore treated as ABSENT, not as a value.
		//   caller-provided (sessions list / deep link) > appData row > ephemeral
		//   (unless placeholder) > 'New Chat'
		const cachedTitle =
			ephemeral?.title && ephemeral.title !== 'New Chat' ? ephemeral.title : '';
		const resolvedTitle = title || row?.title || cachedTitle || 'New Chat';

		// S2: the exact transcript this tab already holds for the target session,
		// as of its cached cursor. Spread into THIS update (below) so the switch
		// never paints an empty list first — there is no window in which the
		// surface could read the reset as a cleared conversation, and the feed
		// resumes at from_seq=lastSeq instead of replaying the whole log.
		const cachedTranscript = feedController.cachedPatch(sessionId);

		update(s => ({
			...s,
			sessionId,
			title: resolvedTitle,
			project: targetProject,
			projectV2Id: targetV2,
			// Start empty; the authoritative messages load from the SSE feed
			// (full replay from_seq=0) started below. No cache-first render —
			// showing a stale snapshot first caused a visible flash before history.
			messages: [],
			error: null,
			// Explicit navigation supersedes any pending boot restore — the user
			// chose this session; don't leave sendMessage waiting on the old one.
			isRestoringSession: false,
			// History fetch for this target is in flight below. messages=[] is
			// transient during this window, so ChatView must not read the empty
			// state as a cleared/new conversation and flash the cleared hint for a
			// session that turns out to have messages. Cleared once the feed starts
			// (below). Unlike isRestoringSession, sendMessage ignores this.
			isLoadingSession: true,
			pendingPermissions: [],
			deferredPermissions: [],
			pendingQuestions: [],
			isStreaming: false,
			// #585: the leaving session's live turn id must not bleed into the
			// target — the target feed re-derives it from its own turn.* events.
			liveTurnId: null,
			// Same for the live model: the leaving session's rerouted-model note
			// must not flag a phantom reroute on the target before its feed replays.
			liveModel: null,
			liveRequestedModel: null,
			liveProvider: null,
			pendingRestorePrompt: null,
			isCreatingWorktree: false,
			worktreeError: null,
			sessionLocked: false,
			sessionLockedBy: undefined,
			// Per-session runtime state — MUST be reset on every switch so the
			// leaving session's values never bleed into the target. These mirror
			// the resets in newChat()/defaultState(); omitting them caused e.g. the
			// previous session's profile (sessionProfileId), next-query profile
			// (currentQueryProfileId — used by queued/auto-worktree sends), token
			// totals, and stream timer to carry over until a later update happened
			// to overwrite them. The /live metadata snapshot repopulates the usage
			// fields with the target session's real values.
			sessionProfileId: null,
			currentQueryProfileId: null,
			totalTokensIn: 0,
			totalTokensOut: 0,
			totalCacheCreationTokens: 0,
			totalCacheReadTokens: 0,
			contextMax: 200_000,
			streamStartTime: null,
			// Background tasks are per-session and written only by the SSE feed for
			// the CURRENT session. Without this reset the leaving session's running
			// tasks (e.g. another worktree's sub-agents) stay in the shared store and
			// flash in the ActiveTasks strip until the target session's feed replays.
			activeTasks: [],
			worktreeCreateNewBranch: false,
			// Mark context as pending — null signals "not yet loaded" so the
			// context bar holds its last value instead of flashing to 0%. The feed
			// / metadata snapshot will populate with correct values.
			contextUsed: null,
			// Same reason: the leaving session's per-call breakdown must not
			// describe the incoming one until its own feed replays.
			liveContextUsage: null,
			// Merge ephemeral state in the same update
			draft: ephemeral?.draft || '',
			scrollTop: ephemeral?.scrollTop || 0,
			// The target session's run binding: this tab's unsent pick first (the
			// user may have changed the model without sending yet), then the
			// session's own row — the server-side binding, so a session opened on
			// a second device shows the model it actually runs on. NEVER fall
			// back to s.X (the leaving session's state): that is what used to
			// cascade one session's model/effort/permission into the next.
			// The session's own row decides its lane — never the leaving session's
			// mode, and never the sidepanel's current pick. Same reasoning as the
			// run binding just below: a session opened on a second device has to
			// land in the lane it was actually created in, whatever this tab is
			// currently showing. A pre-modes row has mode NULL, which normalizes
			// to 'code'.
			mode: normalizeSessionMode(row?.mode),
			modelOverride: ephemeral?.modelOverride ?? sessionModelBinding(row),
			permissionModeOverride: normalizePermissionMode(ephemeral?.permissionModeOverride ?? row?.permission_mode),
			effortLevelOverride: ephemeral?.effortLevelOverride ?? row?.effort ?? null,
			// Cross-device hydration: the row carries the session's own fast-tier
			// pick, so a chat opened elsewhere shows the tier it actually runs on.
			fastMode: ephemeral?.fastMode ?? (row?.fast ?? false),
			workspaceProjectIds: ephemeral?.workspaceProjectIds ?? row?.workspace_project_ids ?? [],
			contextWindowChoice:
				ephemeral?.contextWindowChoice ??
				contextChoiceFromWindow(row?.context_window) ??
				DEFAULT_CONTEXT_WINDOW_CHOICE,
			// Worktree fields: ephemeral > worktreeInfo from session list > safe defaults
			worktreeIntent: ephemeral?.worktreeIntent ?? worktreeInfo?.worktreeIntent ?? 'local',
			// Compose-time only: an existing session's device is already settled
			// on its row, so a cache miss means "no pick" rather than a default.
			deviceIntent: ephemeral?.deviceIntent ?? null,
			worktreeId: ephemeral?.worktreeId ?? worktreeInfo?.worktreeId ?? null,
			worktreeBranch: ephemeral?.worktreeBranch ?? worktreeInfo?.worktreeBranch ?? '',
			worktreeBaseBranch: ephemeral?.worktreeBaseBranch ?? 'main',
			selectedWorktreeBranch: ephemeral?.selectedWorktreeBranch ?? null,
			worktreeMode: ephemeral?.worktreeMode ?? 'new',
			worktreeDeleted: ephemeral?.worktreeDeleted ?? worktreeInfo?.worktreeDeleted ?? false,
			queuedMessages: [],  // Populated from backend via history/queue_updated
			// A send in flight belongs to the session being LEFT (S6).
			optimisticStreaming: false,
			isHydrationSlow: false,
			// LAST: on a cache hit this replaces messages / isStreaming / usage /
			// the loading flags above with the cached session's own exact values.
			// On a miss it is null and the reset stands unchanged.
			...(cachedTranscript ?? {}),
		}));

		// The SSE-fed reducer is the transcript authority (full replay from_seq=0 =
		// history load). Start the feed; permissions/questions flow over legacy WS.
		if (generation !== _switchGeneration) return;
		// Keep isLoadingSession true until this feed's caught_up sentinel folds the
		// complete transcript atomically. Merely starting the fetch is not loaded.
		// Register with SyncEngine so cross-device broadcast events reach this tab.
		wsSend({ type: 'watch_session', session_id: sessionId });
		// Request pending permissions/questions UNCONDITIONALLY (not gated on
		// streaming): a session parked on a permission/question reports
		// isStreaming=false, so gating would hide the modal until a reconnect. The
		// pending_* replies REPLACE by request_id, so re-requesting is idempotent.
		wsSend({ type: 'get_pending_permissions' });
		wsSend({ type: 'get_pending_questions' });
		feedController.start(sessionId);
		persistSession();

		// One-shot metadata snapshot: the feed carries the transcript, streaming
		// flag, tasks and usage, but NOT the lifecycle metadata. Fetch it once from
		// /live so worktree/cloud fields are correct on switch-in.
		void applyMetadataSnapshot(sessionId, generation);
	}

	/**
	 * Fetch the `/live` snapshot once and fold the metadata the SSE feed does not
	 * carry into activeSession — closing the v2 metadata gap
	 * (usage totals and server-authoritative
	 * worktree/cloud fields). Never touches
	 * messages / isStreaming (the feed owns those); `activeTasks` is SEEDED from
	 * it only while the strip is still empty (see gateSnapshotPatch). Guarded on the
	 * switch generation + active sessionId so a stale response can't write into a
	 * session the user already left.
	 */
	async function applyMetadataSnapshot(sessionId: string, generation: number) {
		const initialProject = getState().project;
		const initialProjectV2Id = getState().projectV2Id;
		const initialFolders = getState().workspaceProjectIds;
		const initialModel = getState().modelOverride;
		const initialEffort = getState().effortLevelOverride;
		let snap: LiveSnapshot | null;
		try {
			// `include_messages=false`: the SSE feed is the transcript authority, so
			// the message list this endpoint would otherwise build (megabytes on a
			// long session) is discarded on arrival. Asking for metadata only keeps
			// the switch-in off the transcript read path entirely.
			snap = await api.get<LiveSnapshot>(
				`/sessions/${encodeURIComponent(sessionId)}/live?since_update_id=0&include_messages=false`
			);
		} catch (e) {
			console.warn('[ActiveSession] metadata snapshot failed:', e);
			return;
		}
		if (!snap) return;
		if (generation !== _switchGeneration) return;
		if (getState().sessionId !== sessionId) return;

		const patch: Partial<ActiveSessionState> = {};

		// Usage totals + a best-effort context gauge (input + cache tokens), so the
		// context bar shows a value before the feed's first usage event lands.
		const usage = snap.usage;
		if (usage) {
			const inTok = usage.input_tokens ?? 0;
			const cacheCreate = usage.cache_creation_tokens ?? 0;
			const cacheRead = usage.cache_read_tokens ?? 0;
			patch.totalTokensIn = inTok;
			patch.totalTokensOut = usage.output_tokens ?? 0;
			patch.totalCacheCreationTokens = cacheCreate;
			patch.totalCacheReadTokens = cacheRead;
			patch.contextUsed = inTok + cacheCreate + cacheRead;
		}

		// Seed the chip strip from durable truth. The switch-in reset above wipes
		// `activeTasks` (the leaving session's tasks must not flash on the target),
		// and the feed repaints it from the target's `task.updated` rows — but a
		// task that started BEFORE those rows existed, or in a session whose log
		// predates them, would leave a genuinely running background shell with no
		// surface at all. `/live` reports the task registry's own view, so use it
		// as the floor: only RUNNING rows (never the recently-failed retention
		// rows `get_visible` also carries), and only while the strip is still
		// empty, so a fresher feed fold is never overwritten.
		const snapshotTasks = (snap.activeTasks ?? []).filter((t) => t?.status === 'running');
		if (snapshotTasks.length > 0) patch.activeTasks = snapshotTasks;

		const meta = snap.session_meta;
		if (meta) {
			// Deep links can open before the sidebar row arrives. Restore the
			// durable project alongside its branch, including remote-only/null IDs.
			// Do not replace a project selection made while the request was in flight.
			if ((meta.project_id !== undefined || meta.project_v2_id !== undefined)
				&& getState().project === initialProject
				&& getState().projectV2Id === initialProjectV2Id) {
				Object.assign(patch, resolveSessionProject({
					projectId: meta.project_id,
					projectV2Id: meta.project_v2_id,
					ephemeralProject: initialProject,
					ephemeralProjectV2Id: initialProjectV2Id,
				}));
			}
			// No sidebar row (hidden child/deep link), or a reload whose saved
			// binding predates hydration. Fill holes without replacing an unsent
			// selection or an edit made while this request was in flight.
			if (initialModel == null && getState().modelOverride === initialModel && meta.model !== undefined) {
				patch.modelOverride = sessionModelBinding(meta);
			}
			if (initialEffort == null && getState().effortLevelOverride === initialEffort && meta.effort !== undefined) {
				patch.effortLevelOverride = meta.effort;
			}
			// An older chat may not be in the paginated session list. Hydrate its
			// folders from /live, without overwriting an edit made during the fetch.
			if (meta.workspace_project_ids && getState().workspaceProjectIds === initialFolders) {
				patch.workspaceProjectIds = meta.workspace_project_ids;
			}
			if (meta.title) patch.title = meta.title;
			if (meta.worktree_deleted !== undefined) patch.worktreeDeleted = meta.worktree_deleted;
			if (meta.worktree_id !== undefined) patch.worktreeId = meta.worktree_id;
			if (meta.worktree_branch !== undefined && meta.worktree_branch !== null) {
				patch.worktreeBranch = meta.worktree_branch;
			}
		}

		update(s => {
			if (s.sessionId !== sessionId) return s;
			const gated = gateSnapshotPatch(patch, {
				feedOwnsUsage: feedController.hasAppliedUsage(sessionId),
				currentTitle: s.title,
				currentTaskCount: (s.activeTasks ?? []).length,
			});
			return Object.keys(gated).length > 0 ? { ...s, ...gated } : s;
		});
		// Persist any server-authoritative worktree/cloud correction.
		persistSession();
	}

	function newChat() {
		openHeaderDropdown.set(null);
		++_switchGeneration;
		const state = getState();
		const leavingSessionId = state.sessionId;
		// newChat: leaving sessionId=${leavingSessionId}

		// Tear down the active SSE feed for the session we're leaving.
		if (feedController.isFeeding()) {
			feedController.stop();
		}

		// Save ephemeral for the session we're leaving
		const leavingKey = leavingSessionId || '__new_chat__';
		sessionEphemeralCache.save(leavingKey, buildEphemeral(state));

		// Tell backend to release session lock
		if (leavingSessionId) {
			wsSend({ type: 'close_session' });
		}

		// Reset all internal state
		internal.pendingTools.clear();
		internal.awaitingNewSession = false;
		internal.worktreeCreationGeneration++;
		// NOTE: do NOT abort an in-flight autoCreateWorktree POST on nav —
		// the backend now creates the worktree + session AND kicks off the
		// first query in one request. Aborting would cancel the user's
		// intended work. The generation counter still prevents stale
		// responses from mutating the now-current session.
		internal.worktreeCreateAbort = null;

		// Restore new chat draft if one was saved earlier
		const newChatEphemeral = sessionEphemeralCache.restore('__new_chat__');
		const savedDraft = newChatEphemeral && (newChatEphemeral.draft || newChatEphemeral.worktreeBranch || newChatEphemeral.worktreeId)
			? newChatEphemeral : null;

		// A new session is created into whichever lane the sidepanel is showing.
		const mode = appMode.current;

		// Single update: reset everything (including draft from ephemeral cache)
		update(s => ({
			...s,
			sessionId: null,
			mode,
			// A fresh chat has no session to restore — cancel any pending boot
			// restore (and any in-flight openChat load) so neither window can
			// outlive the navigation into the New Chat compose screen.
			isRestoringSession: false,
			isLoadingSession: false,
			sessionProfileId: null,
			currentQueryProfileId: null,
			profile: getPersistedProfile() || s.profile,
			messages: [],
			isStreaming: false,
			// A send in flight belonged to the session being left (S6), and a
			// fresh compose has no hydration to be slow about (S1).
			optimisticStreaming: false,
			isHydrationSlow: false,
			// #585: reset the live turn id on newChat (mirrors defaultState).
			liveTurnId: null,
			// Reset the live model too so no stale reroute flag survives into the
			// fresh compose screen (mirrors defaultState).
			liveModel: null,
			liveRequestedModel: null,
			liveProvider: null,
			pendingRestorePrompt: null,
			error: null,
			title: 'New Chat',
			totalTokensIn: 0,
			totalTokensOut: 0,
			totalCacheCreationTokens: 0,
			totalCacheReadTokens: 0,
			contextUsed: null,
			contextMax: 200_000,
			liveContextUsage: null,
			// A fresh compose restores the complete last-used run binding. Existing
			// sessions still restore their own row/cache values in switchSession().
			modelOverride: getPersistedModel(),
			// ...except the permission mode outside the code lane. Only the code
			// composer renders the tool-access control, so a chat or voice session
			// that inherited the last code-lane pick would be stuck with it for
			// life: reopening it restores 'chat' from the row, which keeps the
			// control hidden. Inheriting 'plan' would be a deadlock (the plan-exit
			// tool is not on the chat ceiling, so the turn could never leave plan
			// mode) and inheriting 'bypassPermissions' would loosen a lane whose
			// whole premise is a hard ceiling. null means "no session binding",
			// so the backend settles it — and pins it for chat besides.
			permissionModeOverride: mode !== 'chat' ? getPersistedPermissionMode() : null,
			effortLevelOverride: getPersistedEffort(),
			fastMode: getPersistedFastMode(),
			workspaceProjectIds: newChatEphemeral?.workspaceProjectIds ?? [],
			contextWindowChoice: getPersistedContextWindowChoice(),
			pendingPermissions: [],
			deferredPermissions: [],
			pendingQuestions: [],
			queuedMessages: [],
			// Clear another session's running tasks so they don't flash in the
			// ActiveTasks strip on the fresh compose screen (mirrors defaultState).
			activeTasks: [],
			draft: newChatEphemeral?.draft || '',
			scrollTop: 0,
			streamStartTime: null,
			// Project + worktree slice, and the last-toggled Local/Worktree mode —
			// restored for a code compose, cleared for the projectless lanes.
			...newChatModeState(
				mode,
				newChatEphemeral,
				getPersistedProject() || s.project,
			),
			// The device pick rides the new-chat draft's ephemeral entry (memory
			// only) — restored alongside the draft, never from a preference.
			deviceIntent: newChatEphemeral?.deviceIntent ?? null,
			worktreeBaseBranch: 'main',
			worktreeCreateNewBranch: false,
			worktreeMode: 'new' as const,
			isCreatingWorktree: false,
			worktreeError: null,
			worktreeDeleted: false,
			sessionLocked: false,
			sessionLockedBy: undefined,
		}));

		// Typed text and its execution context are one draft. New account-wide
		// defaults must not reroute a restored draft into a different checkout.
		if (savedDraft) update(s => ({...s,
			profile:savedDraft.profile ?? s.profile,
			modelOverride:savedDraft.modelOverride, effortLevelOverride:savedDraft.effortLevelOverride,
			permissionModeOverride:mode === 'chat' ? null : normalizePermissionMode(savedDraft.permissionModeOverride),
			fastMode:savedDraft.fastMode ?? false,
			contextWindowChoice:savedDraft.contextWindowChoice ?? DEFAULT_CONTEXT_WINDOW_CHOICE,
			...(mode === 'chat' ? {} : {
				project:savedDraft.project, projectV2Id:savedDraft.projectV2Id,
				worktreeIntent:savedDraft.worktreeIntent, worktreeId:savedDraft.worktreeId,
				worktreeBranch:savedDraft.worktreeBranch, worktreeBaseBranch:savedDraft.worktreeBaseBranch,
				selectedWorktreeBranch:savedDraft.selectedWorktreeBranch, worktreeMode:savedDraft.worktreeMode,
				worktreeCreateNewBranch:savedDraft.worktreeCreateNewBranch ?? false,
			}),
		}));
		const afterState = getState();
		// newChat complete

		// Persist immediately (not debounced)
		persistSessionImmediate();
		return savedDraft !== null;
	}

	function destroy() {
		_switchGeneration++;
		internal.worktreeCreationGeneration++;
		internal.awaitingNewSession = false;
		if (persistTimer) {
			clearTimeout(persistTimer);
			persistTimer = null;
		}
	}

	return {
		switchSession,
		refreshSessionMetadata: (sessionId: string) => applyMetadataSnapshot(sessionId, _switchGeneration),
		newChat,
		persistSession,
		loadPersistedSession,
		getPersistedProfile,
		getPersistedProject,
		loadSelectionFromBackend,
		saveSelectionToBackend,
		destroy,
	};
}

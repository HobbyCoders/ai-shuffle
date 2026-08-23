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

import type { ActiveSessionState, PersistedSession } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import type { SessionEphemeral } from '../session-cache';
import type { ExecutionMode } from '../types';
import { api } from '$lib/api/client';
import { STREAM_DEBUG } from '$lib/utils/debug-log';
import { getOrCreateDeviceId, TAB_ACTIVE_SESSION_KEY } from '../device-id';
import { appData } from '../app-data.svelte';
// V2 Phase 1: SSE-fed reducer store — the transcript authority for a session.
import { feedController } from './v2/feed-controller';
import { resolveNewChatProject, resolveSessionProject } from './session-project';
import {
	contextChoiceFromWindow,
	DEFAULT_CONTEXT_WINDOW_CHOICE,
	isContextWindowChoice,
	type ContextWindowChoice
} from '$lib/constants/model-context';

// ─── Constants ───────────────────────────────────────────────────────────────

const PERSIST_DEBOUNCE_MS = 500;
const SELECTION_SYNC_DEBOUNCE_MS = 1000;

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
		title?: string;
		worktree_id?: string | null;
		worktree_branch?: string | null;
		worktree_deleted?: boolean;
	};
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
	newChat: () => void;
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
	if (typeof window !== 'undefined') {
		return localStorage.getItem('aihub_selectedProfile') || '';
	}
	return '';
}

export function getPersistedProject(): string {
	if (typeof window !== 'undefined') {
		return localStorage.getItem('aihub_selectedProject') || '';
	}
	return '';
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
	if (typeof window !== 'undefined') {
		localStorage.setItem('aihub_executionMode', mode);
	}
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

export function getPersistedEffort(): string | null {
	if (typeof window === 'undefined') return null;
	return localStorage.getItem('aihub_lastEffort') || null;
}

export function getPersistedPermissionMode(): string | null {
	if (typeof window === 'undefined') return null;
	return localStorage.getItem('aihub_lastPermissionMode') || null;
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
	if (typeof window === 'undefined') return null;
	try {
		const deviceId = getOrCreateDeviceId();
		const prefKey = `selection:${deviceId}`;
		const response = await fetch(`/api/v1/preferences/${encodeURIComponent(prefKey)}`, { credentials: 'include' });
		if (response.ok) {
			const data = await response.json();
			if (data?.value) {
				const sel = data.value as { selectedProfile?: string; selectedProject?: string };
				const selectedProfile = sel.selectedProfile || '';
				const selectedProject = sel.selectedProject || '';
				if (selectedProfile) localStorage.setItem('aihub_selectedProfile', selectedProfile);
				if (selectedProject) localStorage.setItem('aihub_selectedProject', selectedProject);
				return { selectedProfile, selectedProject };
			}
		}
	} catch (e) {
		console.warn('[ActiveSession] Failed to load selection from backend:', e);
	}
	return null;
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createSessionSwitcher(deps: SessionSwitcherDeps): SessionSwitcher {
	const { update, getState, wsSend, internal, sessionEphemeralCache, onPersistTimerChanged } = deps;

	let persistTimer: ReturnType<typeof setTimeout> | null = null;
	let pendingSelection: { profile: string; project: string } | null = null;
	let selectionSyncTimeout: ReturnType<typeof setTimeout> | null = null;
	let _switchGeneration = 0;

	// ── Helpers ──────────────────────────────────────────────────────────

	function buildEphemeral(state: ActiveSessionState): SessionEphemeral {
		return {
			title: state.title,
			draft: state.draft,
			scrollTop: state.scrollTop,
			project: state.project,
			projectV2Id: state.projectV2Id,
			modelOverride: state.modelOverride,
			permissionModeOverride: state.permissionModeOverride,
			effortLevelOverride: state.effortLevelOverride,
			fastMode: state.fastMode,
			contextWindowChoice: state.contextWindowChoice,
			worktreeIntent: state.worktreeIntent,
			deviceIntent: state.deviceIntent,
			worktreeId: state.worktreeId,
			worktreeBranch: state.worktreeBranch,
			worktreeBaseBranch: state.worktreeBaseBranch,
			selectedWorktreeBranch: state.selectedWorktreeBranch,
			worktreeMode: state.worktreeMode,
			worktreeDeleted: state.worktreeDeleted,
		};
	}

	function saveSelectionToBackend(profile: string, project: string) {
		const effectiveProfile = profile || getPersistedProfile();
		const effectiveProject = project || getPersistedProject();

		if (typeof window !== 'undefined') {
			if (effectiveProfile) localStorage.setItem('aihub_selectedProfile', effectiveProfile);
			if (effectiveProject) localStorage.setItem('aihub_selectedProject', effectiveProject);
		}

		pendingSelection = { profile: effectiveProfile, project: effectiveProject };

		if (selectionSyncTimeout) clearTimeout(selectionSyncTimeout);
		selectionSyncTimeout = setTimeout(async () => {
			if (!pendingSelection) return;
			try {
				const deviceId = getOrCreateDeviceId();
				const prefKey = `selection:${deviceId}`;
				await api.put(`/preferences/${encodeURIComponent(prefKey)}`, {
					key: prefKey,
					value: { selectedProfile: pendingSelection.profile, selectedProject: pendingSelection.project },
				});
			} catch (e) {
				console.error('[ActiveSession] Failed to sync selection to backend:', e);
			}
			pendingSelection = null;
			selectionSyncTimeout = null;
		}, SELECTION_SYNC_DEBOUNCE_MS);
	}

	// ── Persistence ─────────────────────────────────────────────────────

	/** The active-session snapshot persisted to both the server (device key) and
	 * this tab's sessionStorage slot. */
	function buildPersistValue(): PersistedSession {
		const state = getState();
		return {
			sessionId: state.sessionId,
			title: state.title,
			profile: state.profile,
			project: state.project,
			projectV2Id: state.projectV2Id,
			modelOverride: state.modelOverride,
			permissionModeOverride: state.permissionModeOverride,
			effortLevelOverride: state.effortLevelOverride,
			fastMode: state.fastMode,
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
			await fetch(`/api/v1/preferences/${encodeURIComponent(prefKey)}`, {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ key: prefKey, value }),
			});
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
		// Resolve title: caller-provided (from sessions list API) > ephemeral cache > 'New Chat'
		// Caller-provided title is authoritative (from the backend); ephemeral may
		// be stale (e.g., 'New Chat' saved before the backend set the real title).
		const resolvedTitle = title || ephemeral?.title || 'New Chat';

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
			modelOverride: ephemeral?.modelOverride ?? row?.model ?? null,
			permissionModeOverride: ephemeral?.permissionModeOverride ?? row?.permission_mode ?? null,
			effortLevelOverride: ephemeral?.effortLevelOverride ?? row?.effort ?? null,
			// Cross-device hydration: the row carries the session's own fast-tier
			// pick, so a chat opened elsewhere shows the tier it actually runs on.
			fastMode: ephemeral?.fastMode ?? (row?.fast ?? false),
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
		}));

		// The SSE-fed reducer is the transcript authority (full replay from_seq=0 =
		// history load). Start the feed; permissions/questions flow over legacy WS.
		if (generation !== _switchGeneration) return;
		update(s => (s.sessionId === sessionId ? { ...s, isLoadingSession: false } : s));
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
	 * messages / isStreaming / activeTasks (the feed owns those). Guarded on the
	 * switch generation + active sessionId so a stale response can't write into a
	 * session the user already left.
	 */
	async function applyMetadataSnapshot(sessionId: string, generation: number) {
		let snap: LiveSnapshot | null;
		try {
			snap = await api.get<LiveSnapshot>(`/sessions/${encodeURIComponent(sessionId)}/live?since_update_id=0`);
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

		const meta = snap.session_meta;
		if (meta) {
			if (meta.title) patch.title = meta.title;
			if (meta.worktree_deleted !== undefined) patch.worktreeDeleted = meta.worktree_deleted;
			if (meta.worktree_id !== undefined) patch.worktreeId = meta.worktree_id;
			if (meta.worktree_branch !== undefined && meta.worktree_branch !== null) {
				patch.worktreeBranch = meta.worktree_branch;
			}
		}

		update(s => (s.sessionId === sessionId ? { ...s, ...patch } : s));
		// Persist any server-authoritative worktree/cloud correction.
		persistSession();
	}

	function newChat() {
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

		// Single update: reset everything (including draft from ephemeral cache)
		update(s => ({
			...s,
			sessionId: null,
			// A fresh chat has no session to restore — cancel any pending boot
			// restore (and any in-flight openChat load) so neither window can
			// outlive the navigation into the New Chat compose screen.
			isRestoringSession: false,
			isLoadingSession: false,
			sessionProfileId: null,
			currentQueryProfileId: null,
			profile: getPersistedProfile() || s.profile,
			...resolveNewChatProject(
				newChatEphemeral,
				getPersistedProject() || s.project,
			),
			messages: [],
			isStreaming: false,
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
			permissionModeOverride: getPersistedPermissionMode(),
			effortLevelOverride: getPersistedEffort(),
			fastMode: getPersistedFastMode(),
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
			// Restore the user's last-toggled mode (Local/Worktree) for new chats
			worktreeIntent: getPersistedWorktreeIntent(),
			// The device pick rides the new-chat draft's ephemeral entry (memory
			// only) — restored alongside the draft, never from a preference.
			deviceIntent: newChatEphemeral?.deviceIntent ?? null,
			worktreeBranch: '',
			worktreeBaseBranch: 'main',
			worktreeCreateNewBranch: false,
			worktreeId: null,
			worktreeMode: 'new' as const,
			selectedWorktreeBranch: null,
			isCreatingWorktree: false,
			worktreeError: null,
			worktreeDeleted: false,
			sessionLocked: false,
			sessionLockedBy: undefined,
		}));

		const afterState = getState();
		// newChat complete

		// Persist immediately (not debounced)
		persistSessionImmediate();
	}

	function destroy() {
		if (selectionSyncTimeout) {
			clearTimeout(selectionSyncTimeout);
			selectionSyncTimeout = null;
		}
		if (persistTimer) {
			clearTimeout(persistTimer);
			persistTimer = null;
		}
	}

	return {
		switchSession,
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

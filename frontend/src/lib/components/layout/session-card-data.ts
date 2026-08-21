/**
 * Pure helpers for the sidebar session cards (Studio Noir shell).
 *
 * Every value here is derived from data the sessions store already carries
 * (`$sessions` rows plus the streaming / pending-permission maps). Nothing in
 * this module fetches anything: segments the store cannot supply — a per
 * session diffstat, the provider's display name — are simply not modelled.
 */
import type { Session } from '$lib/api/client';
import { parseUTC } from '$lib/utils/dateFormatting';

/** Status shown on a card's first line. */
export type SessionCardStatus = 'working' | 'await' | 'done' | 'idle';

export const STATUS_LABELS: Record<SessionCardStatus, string> = {
	working: 'Working',
	await: 'Awaiting input',
	done: 'Done',
	idle: 'Idle'
};

/** The two states that get a live-ticking timer. */
export function isLiveStatus(status: SessionCardStatus): boolean {
	return status === 'working' || status === 'await';
}

/**
 * Resolve a card status from the signals the app already tracks:
 * - streaming (activeSession.isStreaming / streamingSessions map)
 * - awaiting  (backgroundPermissions map — a session parked on a question)
 * - is_active (the session is in the working set but its last turn finished)
 */
export function deriveStatus(input: {
	streaming: boolean;
	awaiting: boolean;
	isActive: boolean;
}): SessionCardStatus {
	if (input.awaiting) return 'await';
	if (input.streaming) return 'working';
	if (input.isActive) return 'done';
	return 'idle';
}

/** Last-activity timestamp (ms) for ordering and the card timer. */
export function sessionTimestamp(s: Session): number {
	const raw = s.updated_at || s.created_at;
	if (!raw) return 0;
	const t = parseUTC(raw).getTime();
	return Number.isNaN(t) ? 0 : t;
}

/** Live elapsed time, e.g. "42s", "4m 12s", "1h 04m". */
export function formatElapsed(ms: number): string {
	const total = Math.max(0, Math.floor(ms / 1000));
	if (total < 60) return `${total}s`;
	const minutes = Math.floor(total / 60);
	if (minutes < 60) {
		const seconds = total % 60;
		return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
	}
	const hours = Math.floor(minutes / 60);
	return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`;
}

/** Compact age for settled sessions, e.g. "now", "12m", "2h", "3d". */
export function formatAge(ms: number): string {
	const total = Math.max(0, Math.floor(ms / 1000));
	if (total < 60) return 'now';
	const minutes = Math.floor(total / 60);
	if (minutes < 60) return `${minutes}m`;
	const hours = Math.floor(minutes / 60);
	if (hours < 24) return `${hours}h`;
	const days = Math.floor(hours / 24);
	if (days < 7) return `${days}d`;
	return `${Math.floor(days / 7)}w`;
}

/** Bucket key for the device filter. A local session buckets as `__local__`. */
export function sessionDeviceId(s: Session): string {
	return s.is_remote && s.instance_id ? s.instance_id : '__local__';
}

/** Human label for the device segment on line 3. */
export function deviceLabel(s: Session): string {
	if (!s.is_remote) return 'This device';
	return s.device_name || 'Unknown device';
}

/** Branch segment on line 3 (worktree branch wins, then the v2 branch field). */
export function branchLabel(s: Session): string | null {
	return s.worktree_branch || s.branch || null;
}

/** Grouping key that folds same-repo clones from several installs into one. */
export const UNGROUPED_PROJECT = '__ungrouped__';

export function projectKey(s: Session): string {
	return s.origin || s.link_group_id || s.project_id || UNGROUPED_PROJECT;
}

/** Identity tokens for a still-present local project. */
export type LiveProject = {
	id: string;
	link_group_id?: string | null;
};

/** Identity tokens for a still-present cluster catalog row. */
export type LiveClusterProject = {
	origin: string;
	project_v2_id: string;
	local_project_id: string | null;
};

/**
 * Keys a session can match against to prove its project still exists:
 * local id, link group, origin, v2 id.
 */
export function liveProjectKeys(
	projects: LiveProject[],
	cluster: LiveClusterProject[]
): Set<string> {
	const keys = new Set<string>();
	for (const p of projects) {
		keys.add(p.id);
		if (p.link_group_id) keys.add(p.link_group_id);
	}
	for (const c of cluster) {
		if (c.origin) keys.add(c.origin);
		if (c.project_v2_id) keys.add(c.project_v2_id);
		if (c.local_project_id) keys.add(c.local_project_id);
	}
	return keys;
}

/**
 * True when the session is attached to a project that is gone from both the
 * local list and the cluster catalog (deleted, or no checkout left anywhere).
 * Ungrouped chats (no project) stay visible.
 */
export function isRemovedProjectSession(s: Session, liveKeys: Set<string>): boolean {
	const key = projectKey(s);
	if (key === UNGROUPED_PROJECT) return false;
	if (liveKeys.has(key)) return false;
	if (s.project_id && liveKeys.has(s.project_id)) return false;
	if (s.origin && liveKeys.has(s.origin)) return false;
	if (s.project_v2_id && liveKeys.has(s.project_v2_id)) return false;
	if (s.link_group_id && liveKeys.has(s.link_group_id)) return false;
	return true;
}

/**
 * True when the owning install was deleted from the cluster registry.
 * Enrichment leaves `device_name` empty in that case. Local / this-device
 * sessions are never treated as removed.
 */
export function isRemovedDeviceSession(s: Session): boolean {
	return Boolean(s.is_remote) && !s.device_name;
}

/** Internal worker/command sessions never belong in the sidebar navigator. */
export function isSidebarSession(s: Session): boolean {
	return !s.hidden;
}

/** Sessions the sidebar may list: not hidden, and not from a removed project or device. */
export function isVisibleSidebarSession(s: Session, liveKeys: Set<string>): boolean {
	return isSidebarSession(s) && !isRemovedProjectSession(s, liveKeys) && !isRemovedDeviceSession(s);
}

/** Sessions the archive shelf owns (backend sets status='archived'). */
export function isArchived(s: Session): boolean {
	return s.status === 'archived';
}

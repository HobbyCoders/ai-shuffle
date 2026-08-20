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

/** Sessions the archive shelf owns (backend sets status='archived'). */
export function isArchived(s: Session): boolean {
	return s.status === 'archived';
}

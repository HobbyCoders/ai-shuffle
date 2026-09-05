/**
 * Pure helpers for the sidebar session cards (Studio Noir shell).
 *
 * Every value here is derived from data the caller already holds: `$sessions`
 * rows plus the streaming / pending-permission maps, and — for the git stage —
 * a working-tree snapshot the card's own store fetched. Nothing in this module
 * fetches anything itself; it only shapes what it is handed.
 */
import type { Session } from '$lib/api/client';
import { normalizeSessionMode, type SessionMode } from '$lib/stores/sessionMode.svelte';
import { parseUTC } from '$lib/utils/dateFormatting';

/** Status shown on a card's first line. */
export type SessionCardStatus =
	| 'working'
	| 'needs_permission'
	| 'has_question'
	| 'await'
	| 'pr'
	| 'pr_merged'
	| 'done'
	| 'idle';

export const STATUS_LABELS: Record<SessionCardStatus, string> = {
	working: 'Working',
	needs_permission: 'Needs Permission',
	has_question: 'Has Question',
	await: 'Awaiting input',
	pr: 'PR Opened',
	pr_merged: 'PR Merged',
	done: 'Done',
	idle: 'Idle'
};

/**
 * The runtime word a card shows on its second line, and the tone that colors
 * it. This is the SESSION's state — what the agent is doing — kept separate
 * from the branch's git stage, which now owns the leading glyph and its own
 * segment. `pr` / `pr_merged` are PR facts rather than runtime facts, so they
 * read as plain "Done" here and let the PR segment say the rest.
 */
export type StatusTone = 'accent' | 'amber' | 'muted';

export const RUNTIME_STATUS_WORDS: Record<SessionCardStatus, string> = {
	working: 'Working',
	needs_permission: 'Needs permission',
	has_question: 'Has question',
	await: 'Awaiting input',
	pr: 'Done',
	pr_merged: 'Done',
	done: 'Done',
	idle: 'Idle'
};

export const RUNTIME_STATUS_TONES: Record<SessionCardStatus, StatusTone> = {
	working: 'accent',
	needs_permission: 'amber',
	has_question: 'amber',
	await: 'amber',
	pr: 'muted',
	pr_merged: 'muted',
	done: 'muted',
	idle: 'muted'
};

/** Statuses that park the row on a prompt — they get the amber warning glyph
 *  ahead of the title, where `working` gets the pulsing live dot. */
export function isAttentionStatus(status: SessionCardStatus): boolean {
	return status === 'needs_permission' || status === 'has_question' || status === 'await';
}

/** The states that get a live-ticking timer — in-flight, including a session
 *  parked on a permission/question prompt (its turn hasn't finished). */
export function isLiveStatus(status: SessionCardStatus): boolean {
	return (
		status === 'working' ||
		status === 'needs_permission' ||
		status === 'has_question' ||
		status === 'await'
	);
}

/**
 * Resolve a card status from the signals the app already tracks:
 * - streaming (activeSession.isStreaming / streamingSessions map)
 * - backgroundWork (durable parked marker or running tasks for the open session)
 * - attention (backgroundPermissions map entry — the session is parked on one
 *   or more permission/question prompts; `kind` drives WHICH prompt label shows)
 * - prState   (persisted session PR lifecycle: open / merged / closed)
 * - openPr    (fallback: an open pull request exists with the session's branch as head)
 * - is_active (the session is in the working set but its last turn finished)
 *
 * Precedence follows what needs the user most: a parked prompt outranks an
 * in-flight turn (a blocked session can still hold an open stream), and PR
 * Opened / PR Merged outrank plain done so a merged PR stays visible while
 * the session is still Active.
 */
export function deriveStatus(input: {
	streaming: boolean;
	backgroundWork?: boolean;
	attention: { count: number; kind: 'permission' | 'question' } | null;
	openPr?: boolean;
	prState?: 'open' | 'merged' | 'closed' | null;
	isActive: boolean;
}): SessionCardStatus {
	if (input.attention && input.attention.count > 0) {
		return input.attention.kind === 'question' ? 'has_question' : 'needs_permission';
	}
	if (input.streaming || input.backgroundWork) return 'working';
	if (input.prState === 'open' || (!input.prState && input.openPr)) return 'pr';
	if (input.prState === 'merged') return 'pr_merged';
	if (input.isActive) return 'done';
	return 'idle';
}

// ============================================================================
// Git stage — the leading glyph
// ============================================================================

/**
 * Where the session's branch sits on the road from edit to merge. One value
 * per row, drawn as the glyph in the card's left column.
 *
 * `clean` is the "repo, nothing to say" fallback (a bare dot) and `none` marks
 * a row with no repo at all — a chat or voice session, a session whose project
 * is gone, or a folder that turned out not to be a git checkout.
 */
export type GitStage =
	| 'pr'
	| 'merged'
	| 'closed'
	| 'dirty'
	| 'committed'
	| 'pushed'
	| 'clean'
	| 'none';

/** Words for the git segment. Empty means the stage speaks through its glyph
 *  alone — a bare dot needs no caption. */
export const GIT_STAGE_LABELS: Record<GitStage, string> = {
	pr: 'PR open',
	merged: 'merged',
	closed: 'PR closed',
	dirty: 'uncommitted',
	committed: 'committed',
	pushed: 'pushed',
	clean: '',
	none: ''
};

export const GIT_STAGE_TONES: Record<GitStage, 'green' | 'purple' | 'amber' | 'muted'> = {
	pr: 'green',
	merged: 'purple',
	closed: 'muted',
	dirty: 'amber',
	committed: 'muted',
	pushed: 'muted',
	clean: 'muted',
	none: 'muted'
};

/** Tooltip on the glyph, so a colored icon is never the only explanation. */
export const GIT_STAGE_TIPS: Record<GitStage, string> = {
	pr: 'Pull request open',
	merged: 'Pull request merged',
	closed: 'Pull request closed without merge',
	dirty: 'Uncommitted changes',
	committed: 'Committed, not pushed',
	pushed: 'Pushed, no PR yet',
	clean: 'No changes',
	none: 'Chat session'
};

/**
 * The working-tree facts the stage needs, flattened out of `GitStatus` so this
 * module stays free of the git API's shape (and so tests can hand it a
 * literal). Absent entirely for rows we never fetch — settled ones.
 */
export interface SessionGitFacts {
	/** False once we positively know the folder is not a checkout. */
	isRepo: boolean;
	/** Any uncommitted work at all: staged, modified, untracked or conflicted. */
	hasChanges: boolean;
	/** Commits on the branch ahead of the default branch (base...HEAD count). */
	ahead: number;
	/**
	 * Commits on HEAD missing from origin/<branch>. `null` when the branch was
	 * never pushed; `0` when it is fully on the remote. This is what separates a
	 * brand-new worktree (ahead=0, unpushed=null → clean) from a feature branch
	 * that has been pushed and is waiting on a PR (ahead>0, unpushed=0 → pushed).
	 */
	unpushed?: number | null;
	/** The repo's real default branch, for telling a session branch from trunk. */
	defaultBranch?: string | null;
}

export interface GitStageInput {
	/** Which lane the row is in — chat and voice sessions never own a branch. */
	mode?: SessionMode;
	/** Whether the session is attached to a project at all. */
	hasProject: boolean;
	/** Persisted PR lifecycle from the session row. */
	prState?: 'open' | 'merged' | 'closed' | null;
	/** Worktree branch, else the v2 branch field. */
	branch?: string | null;
	/** Live working tree, when the card fetched one. */
	git?: SessionGitFacts | null;
}

/**
 * Resolve one stage from the session row plus (optionally) its working tree.
 *
 * An open pull request owns the glyph: stray edits on top of it still show
 * up as the diff counts next to the PR segment. Merged and closed do not —
 * they describe the last PR, and new work on the same session restarts the
 * git lifecycle (dirty → committed → pushed → a new PR) so a session that
 * keeps going after merge is not frozen on "PR Merged".
 *
 * Below that the tree decides, newest work first — uncommitted, then
 * unpushed commits, then a pushed branch waiting on a PR. A fully-pushed
 * feature branch that was just merged still reports ahead>0 / unpushed=0
 * (those commits are the ones that landed); that stays `merged`, not
 * `pushed`.
 *
 * `ahead` counts commits vs the default branch; `unpushed` counts commits vs
 * origin/<branch> (null when that remote ref does not exist). A brand-new
 * worktree sits at ahead=0 with unpushed=null — that is clean, not pushed.
 * "Pushed, no PR yet" requires real commits on the branch (ahead>0) that the
 * remote already has (unpushed===0).
 *
 * Without `git` facts (every settled row — we don't spend a request on those)
 * the answer comes from `prState` alone and otherwise falls to the quiet dot,
 * because "no data" must never be drawn as "no changes to push".
 */
export function deriveGitStage(input: GitStageInput): GitStage {
	if (input.mode === 'chat' || input.mode === 'voice') return 'none';
	if (!input.hasProject) return 'none';
	if (input.prState === 'open') return 'pr';
	if (input.git && !input.git.isRepo) return 'none';
	if (!input.git) {
		if (input.prState === 'merged') return 'merged';
		if (input.prState === 'closed') return 'closed';
		return 'clean';
	}
	if (input.git.hasChanges) return 'dirty';
	// Unique commits vs default branch: fully on the remote → waiting on a PR;
	// otherwise still local (never pushed, or pushed-then-committed-more).
	// After merge those remote commits are the ones that landed, so unpushed===0
	// must not promote a merged row to "pushed". New local commits (unpushed
	// null or >0) do restart the lifecycle.
	if (input.git.ahead > 0 && input.git.unpushed !== 0) return 'committed';
	if (input.prState === 'merged') return 'merged';
	if (input.prState === 'closed') return 'closed';
	if (input.git.ahead > 0) return 'pushed';
	return 'clean';
}

/**
 * The PR segment's text: "PR #123 open" / "PR #123 merged" / "PR closed".
 * Null when the row has no pull request to talk about.
 */
export function prStateText(
	prState: 'open' | 'merged' | 'closed' | null | undefined,
	prNumber?: number | null
): string | null {
	if (!prState) return null;
	const ref = prNumber ? `PR #${prNumber}` : 'PR';
	if (prState === 'open') return `${ref} open`;
	if (prState === 'merged') return `${ref} merged`;
	return `${ref} closed`;
}

/** Diff counts for the second line, e.g. `{ plus: '+124', minus: '−38' }`.
 *  Null when there is nothing changed to count. Uses a real minus sign so the
 *  two columns line up in the mono face. */
export function diffCounts(
	stats: { additions: number; deletions: number } | null | undefined
): { plus: string; minus: string } | null {
	if (!stats) return null;
	const additions = Math.max(0, stats.additions || 0);
	const deletions = Math.max(0, stats.deletions || 0);
	if (additions === 0 && deletions === 0) return null;
	return { plus: `+${additions}`, minus: `−${deletions}` };
}

/**
 * Memo for `sessionTimestamp`. Keyed on the session OBJECT (weak, so rows
 * dropped from the list are collected with it) and validated against the raw
 * string, so a row mutated in place can never serve a stale timestamp.
 *
 * Why: the sidebar sorts every visible session on each recompute and each card
 * recomputes its elapsed time on every ticker frame. Parsing the date inside
 * those loops cost thousands of `Date` constructions per second on a large
 * list (PERFORMANCE_REVIEW_2026-08 R2/R15).
 */
const timestampMemo = new WeakMap<object, { raw: string; ms: number }>();

/** Last-activity timestamp (ms) for ordering and the card timer.
 *  Parsed once per (session object, raw string) pair. */
export function sessionTimestamp(s: Session): number {
	const raw = s.updated_at || s.created_at || '';
	const memo = timestampMemo.get(s);
	if (memo && memo.raw === raw) return memo.ms;
	let ms = 0;
	if (raw) {
		const t = parseUTC(raw).getTime();
		ms = Number.isNaN(t) ? 0 : t;
	}
	timestampMemo.set(s, { raw, ms });
	return ms;
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

/** Human label for the device icon (title + aria-label; no longer shown as text). */
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

/**
 * Which sidepanel lane a row belongs to.
 *
 * `mode` is nullable on the server and was only ever written for voice calls,
 * so every pre-modes row arrives with it absent — those are code sessions, and
 * an unrecognised value falls back the same way rather than vanishing from all
 * three lists.
 */
export function sessionModeOf(s: Session): SessionMode {
	return normalizeSessionMode(s.mode);
}

/** Sessions the archive shelf owns (backend sets status='archived'). */
export function isArchived(s: Session): boolean {
	return s.status === 'archived';
}

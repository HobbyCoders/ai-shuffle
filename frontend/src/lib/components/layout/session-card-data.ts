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
import { lastProseAsksQuestion } from '$lib/utils/work-log';
import type { ChatMessage } from '$lib/stores/types';

/** Status shown on a card's first line. */
export type SessionCardStatus =
	| 'working'
	| 'parked'
	| 'needs_permission'
	| 'has_question'
	| 'await'
	| 'pr'
	| 'pr_merged'
	| 'done'
	| 'idle'
	| 'offline'
	| 'unknown';

export const STATUS_LABELS: Record<SessionCardStatus, string> = {
	working: 'Working',
	parked: 'Parked',
	needs_permission: 'Needs Permission',
	has_question: 'Has Question',
	await: 'Awaiting input',
	pr: 'PR Opened',
	pr_merged: 'PR Merged',
	done: 'Done',
	idle: 'Idle',
	offline: 'Offline',
	unknown: 'Status unavailable'
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
	parked: 'Parked',
	needs_permission: 'Needs permission',
	has_question: 'Has question',
	await: 'Awaiting input',
	pr: 'Done',
	pr_merged: 'Done',
	done: 'Done',
	idle: 'Idle',
	offline: 'Offline',
	unknown: 'Status unavailable'
};

/** The row's status word: a working session whose conversation is being
 *  compacted says so (only the open session knows — see Sidebar). */
export function runtimeStatusWord(status: SessionCardStatus, compacting = false): string {
	return compacting && status === 'working' ? 'Compacting' : RUNTIME_STATUS_WORDS[status];
}

export const RUNTIME_STATUS_TONES: Record<SessionCardStatus, StatusTone> = {
	working: 'accent',
	parked: 'muted',
	needs_permission: 'amber',
	has_question: 'amber',
	await: 'amber',
	pr: 'muted',
	pr_merged: 'muted',
	done: 'muted',
	idle: 'muted',
	offline: 'muted',
	unknown: 'muted'
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
		status === 'parked' ||
		status === 'needs_permission' ||
		status === 'has_question' ||
		status === 'await'
	);
}

/**
 * What the OPEN session is waiting on, read from its own state rather than
 * the background attention map (which the open session clears: its prompts
 * live in its own queues). Counts are distinct items: each pending
 * permission (assistant confirms included) and question is one.
 */
export interface OpenSessionNeeds {
	permissions: number;
	questions: number;
	/** The newest prose since the last user message asks something (spec §5). */
	proseQuestion: boolean;
}

export const NO_OPEN_NEEDS: OpenSessionNeeds = Object.freeze({ permissions: 0, questions: 0, proseQuestion: false });

/**
 * Read the open session's needs. The prose-question part follows the
 * `groupToolCalls` preference (it is where folding protects questions); the
 * prompt queues count regardless.
 */
export function openSessionNeeds(
	state: {
		pendingPermissions?: readonly unknown[] | null;
		pendingQuestions?: readonly unknown[] | null;
		messages?: readonly ChatMessage[] | null;
	},
	groupToolCalls: boolean
): OpenSessionNeeds {
	return {
		permissions: state.pendingPermissions?.length ?? 0,
		questions: state.pendingQuestions?.length ?? 0,
		proseQuestion: groupToolCalls && !!state.messages && lastProseAsksQuestion(state.messages)
	};
}

/**
 * Resolve a card status from the signals the app already tracks:
 * - streaming (user-wide run map, or an optimistic local send)
 * - optimisticStreaming (this tab just sent to the session and the server has
 *   not confirmed the turn yet)
 * - parked (durable parked marker)
 * - backgroundWork (durable running-task summary)
 * - attention (backgroundPermissions map entry — the session is parked on one
 *   or more permission/question prompts; `kind` drives WHICH prompt label shows)
 * - prState   (persisted session PR lifecycle: open / merged / closed)
 * - openPr    (fallback: an open pull request exists with the session's branch as head)
 * - is_active (the session is in the working set but its last turn finished)
 * - open      (the open session's own prompt queues and prose question)
 *
 * Precedence follows what needs the user most: a parked prompt outranks an
 * in-flight turn (a blocked session can still hold an open stream), and PR
 * Opened / PR Merged outrank plain done so a merged PR stays visible while
 * the session is still Active.
 */
export function deriveStatus(input: {
	remoteRuntimeState?: Session['remote_runtime_state'];
	streaming: boolean;
	optimisticStreaming?: boolean;
	backgroundWork?: boolean;
	parked?: boolean;
	attention: { count: number; kind: 'permission' | 'question' } | null;
	/** A private-access request or one-shot approval waits for the owner (#1807). */
	privateAccess?: boolean;
	openPr?: boolean;
	prState?: 'open' | 'merged' | 'closed' | null;
	isActive: boolean;
	open?: OpenSessionNeeds | null;
}): SessionCardStatus {
	// A private-access request blocks the agent on the owner like a permission
	// prompt. It is tracked apart from the run state, so it also outranks a
	// mirror's owner state (which reads `working` while the tool call waits).
	if (input.privateAccess) return 'needs_permission';
	// The open session's own queues are authoritative for it, and anything it
	// waits on beats Working (spec §5). A prose question parks it as Awaiting
	// input until the next user message.
	if (input.open) {
		if (input.open.permissions > 0) return 'needs_permission';
		if (input.open.questions > 0) return 'has_question';
		if (input.open.proseQuestion) return 'await';
	}
	// A mirror has no local leases/jobs. Owner state also clears stale local
	// streaming/attention maps when the remote turn settles.
	if (input.remoteRuntimeState) {
		switch (input.remoteRuntimeState) {
			case 'working': return 'working';
			case 'parked': return 'parked';
			case 'permission': return 'needs_permission';
			case 'question': return 'has_question';
			case 'offline': return 'offline';
			case 'unknown': return 'unknown';
			case 'idle':
				// The owner's "idle" lags a send from here until it republishes,
				// so this tab's own send wins over it. Stale user-wide map
				// entries still do not.
				return deriveStatus({ ...input, remoteRuntimeState: null,
					streaming: !!input.optimisticStreaming, backgroundWork: false, parked: false, attention: null });
		}
	}
	if (input.attention && input.attention.count > 0) {
		return input.attention.kind === 'question' ? 'has_question' : 'needs_permission';
	}
	// Working means a turn is actually running. A turn that ended with live
	// background work (a Monitor shell, a sub-agent) is parked even when the
	// durable marker is missing: some park paths never write it (durable agent
	// ids, unresolved launches), serving Monitors never set it, and the write
	// is best-effort.
	if (input.streaming) return 'working';
	if (input.parked || input.backgroundWork) return 'parked';
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
	/** Exact HEAD/default tree equality, independent of squash commit ancestry. */
	matchesBaseTree?: boolean | null;
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
 *
 * A row with no checkout to read (no project id, or a chat/voice row) has no
 * git lifecycle, but a PR it recorded is still a fact about the chat: the
 * stage then comes from `prState` alone, so the settled row keeps its chip.
 */
export function deriveGitStage(input: GitStageInput): GitStage {
	if (input.mode === 'chat' || input.mode === 'voice' || !input.hasProject) {
		if (input.prState === 'open') return 'pr';
		if (input.prState === 'merged') return 'merged';
		if (input.prState === 'closed') return 'closed';
		return 'none';
	}
	if (input.prState === 'open') return 'pr';
	if (input.git && !input.git.isRepo) {
		if (input.prState === 'merged') return 'merged';
		if (input.prState === 'closed') return 'closed';
		return 'none';
	}
	if (!input.git) {
		if (input.prState === 'merged') return 'merged';
		if (input.prState === 'closed') return 'closed';
		return 'clean';
	}
	if (input.git.hasChanges) return 'dirty';
	// A deleted remote ref has no push count. Squash ancestry alone does not
	// imply follow-up work when the committed tree is exactly what was merged.
	if (input.prState === 'merged' && input.git.unpushed == null && input.git.matchesBaseTree === true) return 'merged';
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

export type PullRequestState = 'open' | 'merged' | 'closed';

/** One pull request a chat opened, or that one of its sub-agents opened. */
export interface SessionPullRequest {
	number: number;
	url: string | null;
	state: PullRequestState;
	title: string | null;
	branch: string | null;
	source: 'session' | 'subagent';
}

const PR_STATES: readonly PullRequestState[] = ['open', 'merged', 'closed'];

/**
 * Every pull request the row knows about, oldest first. Rows written before
 * the list existed (or by an older worker) carry only the single `pr_*`
 * fields; those become a one-entry list. A primary PR missing from the list
 * is appended so the list never contradicts the row.
 */
export function sessionPullRequests(
	s: Pick<Session, 'pull_requests' | 'pr_number' | 'pr_url' | 'pr_state'>
): SessionPullRequest[] {
	const list: SessionPullRequest[] = [];
	for (const raw of Array.isArray(s.pull_requests) ? s.pull_requests : []) {
		const number = Number(raw?.number);
		const state = String(raw?.state ?? '').toLowerCase() as PullRequestState;
		if (!Number.isInteger(number) || number <= 0 || !PR_STATES.includes(state)) continue;
		list.push({
			number,
			url: raw.url || null,
			state,
			title: raw.title || null,
			branch: raw.branch || null,
			source: raw.source === 'subagent' ? 'subagent' : 'session'
		});
	}
	const primary = s.pr_number;
	if (primary && s.pr_state && !list.some((pr) => pr.number === primary && (!pr.url || !s.pr_url || pr.url === s.pr_url))) {
		list.push({
			number: primary,
			url: s.pr_url ?? null,
			state: s.pr_state,
			title: null,
			branch: null,
			source: 'session'
		});
	}
	return list;
}

export interface PullRequestSummary {
	total: number;
	open: number;
	merged: number;
	closed: number;
	/** The state the row reads as: any open PR wins, then merged, then closed. */
	state: PullRequestState;
	/** Short chip copy: "PR #12 open" for one, "2 PRs open · 1 merged" for many. */
	text: string;
	/** The chip copy split into toned parts, in display order. */
	parts: { state: PullRequestState; text: string }[];
}

/**
 * Collapse a chat's PRs into one chip. One PR reads exactly like the single
 * PR chip always has. Several read as counts by state (open, merged, closed,
 * zero counts left out) with the noun on the first part.
 */
export function summarizePullRequests(prs: readonly SessionPullRequest[]): PullRequestSummary | null {
	if (prs.length === 0) return null;
	const counts = { open: 0, merged: 0, closed: 0 };
	for (const pr of prs) counts[pr.state] += 1;
	const state: PullRequestState = counts.open ? 'open' : counts.merged ? 'merged' : 'closed';
	if (prs.length === 1) {
		const text = prStateText(prs[0].state, prs[0].number) ?? 'PR';
		return { total: 1, ...counts, state, text, parts: [{ state: prs[0].state, text }] };
	}
	const parts = PR_STATES.filter((k) => counts[k] > 0).map((k, i) => ({
		state: k,
		text: i === 0 ? `${counts[k]} ${counts[k] === 1 ? 'PR' : 'PRs'} ${k}` : `${counts[k]} ${k}`
	}));
	return {
		total: prs.length,
		...counts,
		state,
		text: parts.map((p) => p.text).join(' · '),
		parts
	};
}

/**
 * The PR state a row's status reads as across all its PRs: one still open
 * keeps the chat on "PR Opened" even after a later PR merged.
 */
export function sessionPrState(
	s: Pick<Session, 'pull_requests' | 'pr_number' | 'pr_url' | 'pr_state'>
): PullRequestState | null {
	return summarizePullRequests(sessionPullRequests(s))?.state ?? s.pr_state ?? null;
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

/** Memo for the live-turn start, same contract as `timestampMemo`. */
const workStartMemo = new WeakMap<object, { raw: string; ms: number }>();

function workStartedMs(s: Session): number | null {
	const raw = s.work_started_at || '';
	if (!raw) return null;
	const memo = workStartMemo.get(s);
	if (memo && memo.raw === raw) return Number.isNaN(memo.ms) ? null : memo.ms;
	const ms = parseUTC(raw).getTime();
	workStartMemo.set(s, { raw, ms });
	return Number.isNaN(ms) ? null : ms;
}

/**
 * Total time the session has spent working, in ms: the banked `work_ms`
 * (every finished turn) plus the turn running now. Idle time between turns
 * never counts, so the total survives Done/Parked and keeps growing when a
 * later turn runs (#1619).
 *
 * `turnLive` says whether this device still sees the turn running. When it
 * does not but the row still carries a start (the row update that banks the
 * finished turn has not arrived yet), the interval is closed at the row's
 * last activity instead of growing with the clock.
 */
export function sessionWorkMs(
	s: Session,
	now: number,
	turnLive: boolean,
	lastActivity: number = sessionTimestamp(s)
): number {
	const banked = Math.max(0, Number(s.work_ms) || 0);
	const start = workStartedMs(s);
	if (start === null) return banked;
	const end = turnLive ? now : lastActivity;
	return banked + Math.max(0, end - start);
}

/** Statuses during which a turn holds the live slot: a working session, or
 *  one blocked on a prompt mid-turn. A parked turn has ended its interval. */
export function isTurnLiveStatus(status: SessionCardStatus): boolean {
	return isLiveStatus(status) && status !== 'parked';
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

/** Device-filter bucket for a remote session that names no device. */
export const UNKNOWN_REMOTE_DEVICE = '__remote_unknown__';

/**
 * Bucket key for the device filter. A local session buckets as `__local__`.
 * A remote session keys by its owner device first: one worker can carry a
 * legacy install id, its network device id or no instance id at all, and all
 * of them must land in one bucket. A remote session never reads as local.
 */
export function sessionDeviceId(s: Session): string {
	if (!s.is_remote) return '__local__';
	return s.owner_device_id || s.instance_id || UNKNOWN_REMOTE_DEVICE;
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
	// Remote checkouts stay on their owner. A missing local checkout catalog
	// entry cannot prove deletion of a named, replicated shared project.
	if (s.is_remote && s.project_v2_id && s.project_name) return false;
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
function isSidebarSession(s: Session): boolean {
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

/** A sidebar row action. */
export type CardAction = 'settle' | 'activate' | 'archive' | 'restore';

/**
 * The actions a row offers. On touch, a left swipe commits the state toggle
 * (Settle for an Active row, Move to Active for a settled one, Restore for an
 * archived one) and a right swipe commits Archive. The card sorts Archive
 * first for its hover actions, so this order is not the on-screen order.
 */
export function cardActions(s: Pick<Session, 'is_active' | 'status'>): CardAction[] {
	if (isArchived(s as Session)) return ['restore'];
	if (s.is_active) return ['archive', 'settle'];
	return ['activate', 'archive'];
}

/**
 * How many things a session is waiting on the user for: the single "Needs
 * you" count (spec §5) behind the sidebar row, the TurnNavigator dot and the
 * tab title. It unions the permission/question prompts (assistant confirms
 * are permission prompts), private-access requests and, for the open
 * session, a prose question. A remote session's owner state wins over the
 * local attention map, matching `deriveStatus`. Archived rows never count.
 *
 * The open session clears its background attention entry but a reconnect
 * snapshot can restore it, so its prompt count is the larger of the two
 * views of the same queue rather than their sum.
 */
export function needsYouCount(
	s: Pick<Session, 'status' | 'is_remote' | 'remote_runtime_state'>,
	attention: { count: number } | null | undefined,
	privateAccessPending = 0,
	open: OpenSessionNeeds | null = null
): number {
	if (isArchived(s as Session)) return 0;
	const remoteWaiting = s.remote_runtime_state === 'permission' || s.remote_runtime_state === 'question' ? 1 : 0;
	const background = s.is_remote ? remoteWaiting : Math.max(0, attention?.count ?? 0);
	const prompts = open ? Math.max(background, open.permissions + open.questions) : background;
	return Math.max(0, privateAccessPending) + prompts + (open?.proseQuestion ? 1 : 0);
}

/** Whether a session is waiting on the user right now (see `needsYouCount`). */
export function sessionNeedsYou(
	s: Pick<Session, 'status' | 'is_remote' | 'remote_runtime_state'>,
	attention: { count: number } | null | undefined,
	privateAccessPending = 0,
	open: OpenSessionNeeds | null = null
): boolean {
	return needsYouCount(s, attention, privateAccessPending, open) > 0;
}

/** The tab title with the number of things waiting on the user in front. */
export function attentionTitle(base: string, count: number): string {
	return count > 0 ? `(${count}) ${base}` : base;
}

/** Minimum touch target edge, in CSS px (WCAG 2.5.5 / platform guidance). */
export const MIN_TAP_TARGET = 44;

/**
 * Whether a tap at (x, y) lands on `rect` once it is grown, about its centre,
 * to at least `min` px each way. The PR chip on a session row is a 16 px line
 * of text inside lines that clip their overflow, so its touch target is
 * widened in hit-testing instead of in layout: the row keeps its density.
 */
export function withinTapTarget(
	rect: { left: number; top: number; width: number; height: number },
	x: number,
	y: number,
	min: number = MIN_TAP_TARGET
): boolean {
	const padX = Math.max(0, (min - rect.width) / 2);
	const padY = Math.max(0, (min - rect.height) / 2);
	return (
		x >= rect.left - padX &&
		x <= rect.left + rect.width + padX &&
		y >= rect.top - padY &&
		y <= rect.top + rect.height + padY
	);
}

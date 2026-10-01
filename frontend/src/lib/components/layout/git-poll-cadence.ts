/**
 * Cadence rules for MainPanel's adaptive git poll.
 *
 * Extracted from the component so the timing decisions — the part that was
 * actually wrong — can be unit-tested without mounting the panel.
 *
 * Background (perf review F3 / G8): the poll ran `/git/status` every 5 s while
 * idle, and the backend's status cache has a 2 s TTL, so every idle tick was a
 * guaranteed cache miss → a full pygit2 working-tree scan, per visible tab,
 * forever. The diff-stats gate made it worse: it compared the elapsed time
 * against `GIT_POLL_IDLE_MS`, which was *also* the tick interval, so the
 * condition was always true and two full-tree diffs ran on every idle tick even
 * though the comment claimed 30 s.
 *
 * The fix keeps the poll (it is the fallback for missed events) but makes the
 * idle cadence cheap, and keeps the responsive 2 s cadence only while something
 * is actually happening: the agent is streaming, or the user just ran a git
 * action.
 */

/** Responsive cadence: agent streaming, or just after a user git action. */
export const GIT_POLL_ACTIVE_MS = 2_000;
/** Idle fallback cadence. Was 5 s — a guaranteed backend cache miss every tick. */
export const GIT_POLL_IDLE_MS = 30_000;
/** How long after the last git-relevant activity we stay on the fast cadence. */
export const GIT_IDLE_THRESHOLD_MS = 30_000;
/**
 * Minimum spacing between idle diff-stats polls. Deliberately a distinct
 * constant from the tick interval: the G8 bug was that the two were the same
 * value, which degenerates the "only occasionally" gate into "every tick".
 */
export const GIT_IDLE_DIFF_STATS_MS = 60_000;

/** Within the activity window → poll responsively. */
export function isGitActive(
	now: number,
	lastActivity: number,
	thresholdMs: number = GIT_IDLE_THRESHOLD_MS
): boolean {
	return now - lastActivity < thresholdMs;
}

/** Interval until the next poll tick. */
export function gitPollIntervalMs(now: number, lastActivity: number, menuOpen = false): number {
	return menuOpen || isGitActive(now, lastActivity) ? GIT_POLL_ACTIVE_MS : GIT_POLL_IDLE_MS;
}

/**
 * Should this tick also refresh diff stats?
 *
 * Active: yes, every tick — the numbers are changing under the user.
 * Idle: only once per `GIT_IDLE_DIFF_STATS_MS`, so a parked tab is not running
 * two full-tree diffs a minute apart from the status scan.
 */
export function shouldPollDiffStats(
	now: number,
	lastActivity: number,
	lastDiffStatsPoll: number
): boolean {
	if (isGitActive(now, lastActivity)) return true;
	return now - lastDiffStatsPoll >= GIT_IDLE_DIFF_STATS_MS;
}

export interface GitPollGateInput {
	/** The git UI is actually on screen for the active session. */
	gitUiVisible: boolean;
	/** `document.hidden` — a background tab polls nothing. */
	documentHidden: boolean;
}

/**
 * Whether the poll loop should be running at all. Polling for a surface nobody
 * is looking at is pure waste: the panel re-reads status when it comes back.
 */
export function shouldRunGitPoll(input: GitPollGateInput): boolean {
	return input.gitUiVisible && !input.documentHidden;
}

export interface GitPollTickInput {
	/** A commit/push/PR/pull is in flight; it makes its own calls. */
	operationInProgress: boolean;
	/** The worktree 404'd — polling it would just spam errors. */
	worktreeGone: boolean;
}

/** Whether a scheduled tick should actually hit the network. */
export function shouldExecuteGitPollTick(input: GitPollTickInput): boolean {
	return !input.operationInProgress && !input.worktreeGone;
}

/**
 * Shared option-bag types + pure derivations for the git header actions.
 *
 * The MainPanel handlers accept these option bags; a bare button click passes
 * nothing (falling back to the gitActionPreferences defaults), while the
 * split-button option popovers pass an explicit bag. Keeping the types here lets
 * GitHeaderActions, GitActionOptionsDropdown, and MainPanel agree on one shape.
 */

export type CommitNextStep = 'commit' | 'push' | 'pr';
export type PullStrategy = 'merge' | 'rebase';
export type MergeMethod = 'squash' | 'merge' | 'rebase';

export interface CommitOptions {
	message?: string;
	includeUnstaged?: boolean;
	nextStep?: CommitNextStep;
	/**
	 * Commit on a fresh branch instead of the current (default) branch. An empty
	 * string means "name it from the commit message".
	 */
	newBranch?: string;
	/** The user explicitly chose to push straight to the default branch. */
	pushToDefaultBranch?: boolean;
}

/**
 * Resolve what a commit request may actually do on the current checkout.
 *
 * On the default branch a push (or a PR, which needs a push first) runs only
 * when the user explicitly chose it: either a new branch or an explicit
 * "push to <default>". Anything else is refused, never silently downgraded
 * to a push (#1659). `blocked` carries the reason to show inline.
 */
export function resolveCommitPlan(input: {
	requested: CommitNextStep;
	isOnDefaultBranch: boolean;
	needsPull: boolean;
	newBranch?: string;
	pushToDefaultBranch?: boolean;
}): { nextStep: CommitNextStep; newBranch: string | null; blocked: string | null } {
	const newBranch = input.isOnDefaultBranch && input.newBranch !== undefined ? input.newBranch.trim() : null;
	if (newBranch !== null) return { nextStep: input.requested, newBranch, blocked: null };
	if (input.needsPull) return { nextStep: 'commit', newBranch: null, blocked: null };
	if (!input.isOnDefaultBranch || input.requested === 'commit') return { nextStep: input.requested, newBranch: null, blocked: null };
	if (input.requested === 'push' && input.pushToDefaultBranch) return { nextStep: 'push', newBranch: null, blocked: null };
	return {
		nextStep: input.requested,
		newBranch: null,
		blocked: 'You are on the default branch. Choose “Commit on new branch” or an explicit push to it.'
	};
}

/** A branch name derived from a commit message: `fix(git): Guard X` → `fix/guard-x`. */
export function branchNameFromMessage(message: string, now: Date = new Date()): string {
	const subject = (message.split('\n')[0] ?? '').trim();
	const conventional = /^([a-z]+)(?:\([^)]*\))?!?:\s*(.*)$/i.exec(subject);
	const prefix = conventional ? conventional[1].toLowerCase() : 'change';
	const rest = conventional ? conventional[2] : subject;
	const slug = rest.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48).replace(/-+$/, '');
	if (slug) return `${prefix}/${slug}`;
	const stamp = now.toISOString().slice(0, 16).replace(/[-:T]/g, '');
	return `${prefix}/${stamp}`;
}
export interface PushOptions {
	force?: boolean;
}
export interface PullOptions {
	strategy?: PullStrategy;
	autoStash?: boolean;
}
export interface CreatePROptions {
	title?: string;
	body?: string;
	base?: string;
	draft?: boolean;
}
export interface ShipItOptions {
	mergeMethod?: MergeMethod;
	cleanup?: boolean;
	includeUnstaged?: boolean;
	base?: string;
	draft?: boolean;
}

/** Which git action a split-button / option popover is driving. */
export type GitActionKind = 'commit' | 'push' | 'pull' | 'pr' | 'shipit';

// ── Ship It step preview ────────────────────────────────────────────────────

export interface ShipItStepInput {
	/** Uncommitted working-tree changes → the pipeline opens with a Commit step. */
	hasChanges: boolean;
	/** A pull request already exists for the branch → skip the Create PR step. */
	hasOpenPR: boolean;
	/** Commits not on origin/<branch>; unknown requires a push check. */
	unpushed?: number | null;
	prNumber?: number;
	/** User's cleanup-after-merge preference → append the branch/worktree cleanup step. */
	cleanup: boolean;
}

export interface ShipItStep {
	id: string;
	label: string;
}

/**
 * Derive the ordered Ship It pipeline preview from the current repo state.
 *
 * Commit, push, and Create PR are shown only when needed. An unknown remote
 * state keeps the push check; a confirmed up-to-date branch skips it. Cleanup
 * is appended only when the user keeps that preference on. Pure so the header
 * can render an honest preview and it can be unit-tested without the component.
 */
export function computeShipItSteps(input: ShipItStepInput): ShipItStep[] {
	const steps: ShipItStep[] = [];
	if (input.hasChanges) steps.push({ id: 'commit', label: 'Commit changes' });
	if (input.hasChanges || input.unpushed !== 0) steps.push({ id: 'push', label: 'Push to remote' });
	if (!input.hasOpenPR) steps.push({ id: 'pr', label: 'Create pull request' });
	steps.push({ id: 'merge', label: input.hasOpenPR && input.prNumber ? `Merge PR #${input.prNumber}` : 'Merge pull request' });
	if (input.cleanup) steps.push({ id: 'cleanup', label: 'Clean up branch' });
	return steps;
}

// ── Operation completion side-effects ───────────────────────────────────────

/** The origin an operation was started against (captured at START). */
export interface OperationOrigin {
	sessionId: string | null;
	projectId: string | null;
}

/** Terminal snapshot fields a completed operation reports. */
export interface OperationOutcome {
	status: 'running' | 'done' | 'error';
	worktreeDeleted?: boolean;
	prUrl?: string;
	prNumber?: number;
}

/** The side-effects the caller should apply after an operation finishes. */
export interface OperationSideEffects {
	/** Flag the ORIGINATING session's worktree deleted (never a switched-to one). */
	markWorktreeDeleted: boolean;
	/** Refresh git/PR state (only while still viewing the originating project). */
	refreshGit: boolean;
	/** Success toast to show (always safe — it just confirms the user's action). */
	toast: { label: string; prUrl?: string; prNumber?: number };
}

/**
 * Decide which completion side-effects a finished git operation should apply,
 * given the origin it was started against and the currently-active session /
 * project. Session-scoped effects (markWorktreeDeleted, git refresh) are
 * suppressed when the user has switched away, so a Finish/Ship It that resolves
 * after the user moved on never mutates the wrong session's state.
 *
 * Returns null for a non-terminal / non-`done` operation (nothing to do — a
 * failure is already pinned in the progress popover).
 */
export function computeOperationSideEffects(
	op: OperationOutcome,
	origin: OperationOrigin,
	current: OperationOrigin
): OperationSideEffects | null {
	if (op.status !== 'done') return null;

	const stillOnOrigin = current.sessionId === origin.sessionId;
	const sameProject = current.projectId === origin.projectId;

	const toast = op.prUrl
		? {
				label: op.prNumber ? `Merged pull request #${op.prNumber}` : 'Operation complete',
				prUrl: op.prUrl,
				prNumber: op.prNumber
			}
		: { label: 'Operation complete' };

	return {
		markWorktreeDeleted: !!op.worktreeDeleted && stillOnOrigin,
		refreshGit: stillOnOrigin && sameProject,
		toast
	};
}

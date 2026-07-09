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
export interface FinishOptions {
	mergeMethod?: MergeMethod;
	cleanup?: boolean;
}
export interface ShipItOptions {
	mergeMethod?: MergeMethod;
	cleanup?: boolean;
	includeUnstaged?: boolean;
	base?: string;
	draft?: boolean;
}

/** Which git action a split-button / option popover is driving. */
export type GitActionKind = 'commit' | 'push' | 'pull' | 'pr' | 'finish' | 'shipit';

// ── Ship It step preview ────────────────────────────────────────────────────

export interface ShipItStepInput {
	/** Uncommitted working-tree changes → the pipeline opens with a Commit step. */
	hasChanges: boolean;
	/** A pull request already exists for the branch → skip the Create PR step. */
	hasOpenPR: boolean;
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
 * Ship It always pushes and merges; the Commit and Create PR steps are only
 * shown when they're actually needed (dirty tree / no existing PR), and cleanup
 * is appended only when the user keeps that preference on. Pure so the header
 * can render an honest preview and it can be unit-tested without the component.
 */
export function computeShipItSteps(input: ShipItStepInput): ShipItStep[] {
	const steps: ShipItStep[] = [];
	if (input.hasChanges) steps.push({ id: 'commit', label: 'Commit changes' });
	steps.push({ id: 'push', label: 'Push to remote' });
	if (!input.hasOpenPR) steps.push({ id: 'pr', label: 'Create pull request' });
	steps.push({ id: 'merge', label: 'Merge pull request' });
	if (input.cleanup) steps.push({ id: 'cleanup', label: 'Clean up branch' });
	return steps;
}

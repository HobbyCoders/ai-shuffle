/**
 * Git action prompt builder (pure module)
 *
 * The mutating git buttons in the header (Commit / Push / Pull / Create PR /
 * Finish / Ship It) send a natural-language prompt to the active chat so the
 * agent performs the operation with its own git/gh tools. This module owns the
 * wording of those prompts, split out of MainPanel so it can be unit-tested and
 * reused by the settings UI.
 *
 * Two layers:
 * - `getDefaultGitActionPrompt` — the built-in prompt, shaped by the structured
 *   preferences (next step, pull strategy, merge method, cleanup, …).
 * - `buildGitActionPrompt` — returns a user's custom message (with placeholders
 *   substituted) when one is set, otherwise falls back to the default.
 */

import type { GitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';

export type GitActionKey = 'commit' | 'push' | 'pull' | 'createPR' | 'finish' | 'shipIt';

/** Every action key, in display order. Single source of truth for iteration/sanitization. */
export const GIT_ACTION_KEYS: readonly GitActionKey[] = [
	'commit',
	'push',
	'pull',
	'createPR',
	'finish',
	'shipIt',
];

export interface GitPromptContext {
	/** Current branch name, or null when unknown. */
	branch: string | null;
	/** Repo default branch (fallback 'main'). */
	defaultBranch: string;
	/** Local branch is behind its upstream. */
	needsPull: boolean;
	/** Current branch is the repo default branch. */
	isOnDefaultBranch: boolean;
	/** Open PR number for this branch, if any. */
	branchPRNumber: number | null;
	/** Structured preferences that shape the default prompts. */
	prefs: GitActionPreferences;
}

/** Append " (current branch: `<name>`)" if a branch is known. */
function branchSuffix(ctx: GitPromptContext): string {
	return ctx.branch ? ` (current branch: \`${ctx.branch}\`)` : '';
}

/** Turn the saved merge method into the verb used in prompts. */
function mergeVerb(prefs: GitActionPreferences): string {
	return prefs.mergeMethod === 'squash'
		? 'squash-merge'
		: prefs.mergeMethod === 'rebase'
			? 'rebase-merge'
			: 'merge';
}

// ============================================
// Default builders (one per action)
// ============================================

/** Commit prompt, honoring saved next-step + include-unstaged prefs. */
function buildCommitPrompt(ctx: GitPromptContext): string {
	const prefs = ctx.prefs;
	const staged = prefs.includeUnstaged
		? 'Stage and commit all current changes (including untracked files)'
		: 'Commit the currently staged changes';
	let prompt = `${staged} with a clear, conventional-commit-style message that summarizes what changed${branchSuffix(ctx)}.`;

	// Honor the saved "next step" preference, but guard against invalid states.
	let nextStep = prefs.nextStep;
	if (ctx.needsPull) nextStep = 'commit';
	else if (nextStep === 'pr' && ctx.isOnDefaultBranch) nextStep = 'push';

	if (nextStep === 'push') {
		prompt += ' Then push the branch to its remote.';
	} else if (nextStep === 'pr') {
		prompt += ' Then push the branch and open a pull request into the default branch with a descriptive title and summary.';
	}
	return prompt;
}

/** Push prompt. */
function buildPushPrompt(ctx: GitPromptContext): string {
	return `Push the current branch to its remote${branchSuffix(ctx)}.`;
}

/** Pull prompt, honoring saved pull strategy + auto-stash. */
function buildPullPrompt(ctx: GitPromptContext): string {
	const prefs = ctx.prefs;
	const strategy = prefs.pullStrategy === 'rebase' ? 'rebasing onto' : 'merging';
	const stash = prefs.autoStash
		? ' If there are uncommitted local changes, stash them first and restore them afterward.'
		: '';
	return `Pull the latest changes from the remote for the current branch${branchSuffix(ctx)}, ${strategy} the upstream changes into your local work.${stash} If there are conflicts, stop and tell me which files conflict.`;
}

/** Create-PR prompt. */
function buildCreatePRPrompt(ctx: GitPromptContext): string {
	return `Push the current branch${branchSuffix(ctx)} and open a pull request into the default branch with a descriptive title and a summary of what changed.`;
}

/** Finish/merge prompt, honoring saved merge method + cleanup. */
function buildFinishPrompt(ctx: GitPromptContext): string {
	const prefs = ctx.prefs;
	const pr = ctx.branchPRNumber ? `pull request #${ctx.branchPRNumber}` : 'the open pull request for this branch';
	const method = mergeVerb(prefs);
	const cleanup = prefs.cleanupAfterMerge
		? ' After it merges, delete the branch. If this is a worktree session, do not try to remove or delete the worktree folder itself — this session is running inside it and holds a lock on it, so removal will fail; leave the worktree directory cleanup to the app.'
		: '';
	return `${method.charAt(0).toUpperCase() + method.slice(1)} ${pr} into the default branch.${cleanup}`;
}

/** Ship It pipeline prompt. */
function buildShipItPrompt(ctx: GitPromptContext): string {
	const prefs = ctx.prefs;
	const method = mergeVerb(prefs);
	const cleanup = prefs.cleanupAfterMerge
		? ', then delete the branch. Do not try to remove or delete the worktree folder itself — this session is running inside it and holds a lock on it, so removal will fail; leave the worktree directory cleanup to the app'
		: '';
	const prStep = ctx.branchPRNumber
		? `then ${method} the existing pull request #${ctx.branchPRNumber} into the default branch`
		: `open a pull request into the default branch with a descriptive title and summary, then ${method} it`;
	return `Ship this branch end to end${branchSuffix(ctx)}: commit all current changes with a clear conventional-commit message, push the branch, ${prStep}${cleanup}. If anything fails along the way, stop and tell me what went wrong.`;
}

/**
 * Build the DEFAULT prompt for an action from the structured preferences.
 * Exposed so the settings UI can preview / placeholder the built-in wording.
 */
export function getDefaultGitActionPrompt(action: GitActionKey, ctx: GitPromptContext): string {
	switch (action) {
		case 'commit':
			return buildCommitPrompt(ctx);
		case 'push':
			return buildPushPrompt(ctx);
		case 'pull':
			return buildPullPrompt(ctx);
		case 'createPR':
			return buildCreatePRPrompt(ctx);
		case 'finish':
			return buildFinishPrompt(ctx);
		case 'shipIt':
			return buildShipItPrompt(ctx);
	}
}

// ============================================
// Custom message placeholders
// ============================================

/**
 * Substitute the supported placeholders in a custom git-action message.
 *
 * - `{branch}`         → current branch name, or "the current branch" if unknown
 * - `{default_branch}` → the repo default branch name
 * - `{pr}`             → "pull request #N" when a PR number is known, else
 *                        "the open pull request for this branch"
 *
 * Unknown `{...}` tokens are left untouched.
 */
export function substituteGitPromptPlaceholders(template: string, ctx: GitPromptContext): string {
	const branch = ctx.branch ?? 'the current branch';
	const pr = ctx.branchPRNumber
		? `pull request #${ctx.branchPRNumber}`
		: 'the open pull request for this branch';

	return template.replace(/\{(branch|default_branch|pr)\}/g, (_match, token: string) => {
		switch (token) {
			case 'branch':
				return branch;
			case 'default_branch':
				return ctx.defaultBranch;
			case 'pr':
				return pr;
			default:
				return _match;
		}
	});
}

/**
 * Build the prompt actually sent to chat for an action.
 *
 * When the user has set a non-empty custom message for this action, that
 * message (with placeholders substituted) replaces the default entirely.
 * Otherwise the structured-preferences default is used.
 */
export function buildGitActionPrompt(action: GitActionKey, ctx: GitPromptContext): string {
	const custom = ctx.prefs.customMessages?.[action];
	if (typeof custom === 'string' && custom.trim().length > 0) {
		return substituteGitPromptPlaceholders(custom, ctx);
	}
	return getDefaultGitActionPrompt(action, ctx);
}

import { describe, it, expect } from 'vitest';
import type { GitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';
import {
	buildGitActionPrompt,
	getDefaultGitActionPrompt,
	substituteGitPromptPlaceholders,
	type GitPromptContext,
} from './git-action-prompts';

// Mirror of the store defaults (kept local so this test doesn't import the
// runtime store module — only its type, which is erased).
const DEFAULT_PREFS: GitActionPreferences = {
	includeUnstaged: true,
	nextStep: 'push',
	pullStrategy: 'rebase',
	autoStash: false,
	mergeMethod: 'squash',
	cleanupAfterMerge: true,
	customMessages: {},
};

function makePrefs(overrides: Partial<GitActionPreferences> = {}): GitActionPreferences {
	return { ...DEFAULT_PREFS, ...overrides };
}

function makeCtx(overrides: Partial<GitPromptContext> = {}): GitPromptContext {
	return {
		branch: 'feature-x',
		defaultBranch: 'main',
		needsPull: false,
		isOnDefaultBranch: false,
		branchPRNumber: null,
		prefs: DEFAULT_PREFS,
		...overrides,
	};
}

describe('getDefaultGitActionPrompt — commit', () => {
	it('include-unstaged + push next step (default)', () => {
		const ctx = makeCtx({ prefs: makePrefs({ includeUnstaged: true, nextStep: 'push' }) });
		expect(getDefaultGitActionPrompt('commit', ctx)).toBe(
			'Stage and commit all current changes (including untracked files) with a clear, conventional-commit-style message that summarizes what changed (current branch: `feature-x`). Then push the branch to its remote.'
		);
	});

	it('staged-only + pr next step opens a PR', () => {
		const ctx = makeCtx({ prefs: makePrefs({ includeUnstaged: false, nextStep: 'pr' }) });
		expect(getDefaultGitActionPrompt('commit', ctx)).toBe(
			'Commit the currently staged changes with a clear, conventional-commit-style message that summarizes what changed (current branch: `feature-x`). Then push the branch and open a pull request into the default branch with a descriptive title and summary.'
		);
	});

	it('needsPull forces commit-only (no push/pr suffix)', () => {
		const ctx = makeCtx({ needsPull: true, prefs: makePrefs({ nextStep: 'pr' }) });
		expect(getDefaultGitActionPrompt('commit', ctx)).toBe(
			'Stage and commit all current changes (including untracked files) with a clear, conventional-commit-style message that summarizes what changed (current branch: `feature-x`).'
		);
	});

	it('pr on the default branch downgrades to push', () => {
		const ctx = makeCtx({ isOnDefaultBranch: true, prefs: makePrefs({ nextStep: 'pr' }) });
		expect(getDefaultGitActionPrompt('commit', ctx)).toBe(
			'Stage and commit all current changes (including untracked files) with a clear, conventional-commit-style message that summarizes what changed (current branch: `feature-x`). Then push the branch to its remote.'
		);
	});

	it('commit next step (no push/pr) omits the follow-on sentence', () => {
		const ctx = makeCtx({ prefs: makePrefs({ nextStep: 'commit' }) });
		expect(getDefaultGitActionPrompt('commit', ctx)).toBe(
			'Stage and commit all current changes (including untracked files) with a clear, conventional-commit-style message that summarizes what changed (current branch: `feature-x`).'
		);
	});

	it('omits the branch suffix when branch is unknown', () => {
		const ctx = makeCtx({ branch: null, prefs: makePrefs({ nextStep: 'commit' }) });
		expect(getDefaultGitActionPrompt('commit', ctx)).toBe(
			'Stage and commit all current changes (including untracked files) with a clear, conventional-commit-style message that summarizes what changed.'
		);
	});
});

describe('getDefaultGitActionPrompt — push', () => {
	it('with a known branch', () => {
		expect(getDefaultGitActionPrompt('push', makeCtx())).toBe(
			'Push the current branch to its remote (current branch: `feature-x`).'
		);
	});
	it('without a known branch', () => {
		expect(getDefaultGitActionPrompt('push', makeCtx({ branch: null }))).toBe(
			'Push the current branch to its remote.'
		);
	});
});

describe('getDefaultGitActionPrompt — pull', () => {
	it('rebase + auto-stash', () => {
		const ctx = makeCtx({ prefs: makePrefs({ pullStrategy: 'rebase', autoStash: true }) });
		expect(getDefaultGitActionPrompt('pull', ctx)).toBe(
			'Pull the latest changes from the remote for the current branch (current branch: `feature-x`), rebasing onto the upstream changes into your local work. If there are uncommitted local changes, stash them first and restore them afterward. If there are conflicts, stop and tell me which files conflict.'
		);
	});

	it('merge without stash', () => {
		const ctx = makeCtx({ prefs: makePrefs({ pullStrategy: 'merge', autoStash: false }) });
		expect(getDefaultGitActionPrompt('pull', ctx)).toBe(
			'Pull the latest changes from the remote for the current branch (current branch: `feature-x`), merging the upstream changes into your local work. If there are conflicts, stop and tell me which files conflict.'
		);
	});
});

describe('getDefaultGitActionPrompt — createPR', () => {
	it('builds the push+PR prompt', () => {
		expect(getDefaultGitActionPrompt('createPR', makeCtx())).toBe(
			'Push the current branch (current branch: `feature-x`) and open a pull request into the default branch with a descriptive title and a summary of what changed.'
		);
	});
});

describe('getDefaultGitActionPrompt — finish', () => {
	it('squash with a known PR number + cleanup', () => {
		const ctx = makeCtx({ branchPRNumber: 42, prefs: makePrefs({ mergeMethod: 'squash', cleanupAfterMerge: true }) });
		expect(getDefaultGitActionPrompt('finish', ctx)).toBe(
			'Squash-merge pull request #42 into the default branch. After it merges, delete the branch. If this is a worktree session, do not try to remove or delete the worktree folder itself — this session is running inside it and holds a lock on it, so removal will fail; leave the worktree directory cleanup to the app.'
		);
	});

	it('plain merge without a PR number, cleanup off', () => {
		const ctx = makeCtx({ branchPRNumber: null, prefs: makePrefs({ mergeMethod: 'merge', cleanupAfterMerge: false }) });
		expect(getDefaultGitActionPrompt('finish', ctx)).toBe(
			'Merge the open pull request for this branch into the default branch.'
		);
	});

	it('rebase merge method', () => {
		const ctx = makeCtx({ branchPRNumber: 3, prefs: makePrefs({ mergeMethod: 'rebase', cleanupAfterMerge: false }) });
		expect(getDefaultGitActionPrompt('finish', ctx)).toBe(
			'Rebase-merge pull request #3 into the default branch.'
		);
	});
});

describe('getDefaultGitActionPrompt — shipIt', () => {
	it('squash-merge an existing PR with cleanup', () => {
		const ctx = makeCtx({ branchPRNumber: 7, prefs: makePrefs({ mergeMethod: 'squash', cleanupAfterMerge: true }) });
		expect(getDefaultGitActionPrompt('shipIt', ctx)).toBe(
			'Ship this branch end to end (current branch: `feature-x`): commit all current changes with a clear conventional-commit message, push the branch, then squash-merge the existing pull request #7 into the default branch, then delete the branch. Do not try to remove or delete the worktree folder itself — this session is running inside it and holds a lock on it, so removal will fail; leave the worktree directory cleanup to the app. If anything fails along the way, stop and tell me what went wrong.'
		);
	});

	it('open a new PR (no PR number), rebase, cleanup off, no branch', () => {
		const ctx = makeCtx({ branch: null, branchPRNumber: null, prefs: makePrefs({ mergeMethod: 'rebase', cleanupAfterMerge: false }) });
		expect(getDefaultGitActionPrompt('shipIt', ctx)).toBe(
			'Ship this branch end to end: commit all current changes with a clear conventional-commit message, push the branch, open a pull request into the default branch with a descriptive title and summary, then rebase-merge it. If anything fails along the way, stop and tell me what went wrong.'
		);
	});
});

describe('substituteGitPromptPlaceholders', () => {
	it('substitutes {branch} and {default_branch}', () => {
		const ctx = makeCtx({ branch: 'feat', defaultBranch: 'trunk' });
		expect(substituteGitPromptPlaceholders('Merge {branch} into {default_branch}', ctx)).toBe(
			'Merge feat into trunk'
		);
	});

	it('{branch} falls back to "the current branch" when unknown', () => {
		const ctx = makeCtx({ branch: null });
		expect(substituteGitPromptPlaceholders('Push {branch}', ctx)).toBe('Push the current branch');
	});

	it('{pr} resolves to the PR number when known', () => {
		const ctx = makeCtx({ branchPRNumber: 12 });
		expect(substituteGitPromptPlaceholders('Merge {pr}', ctx)).toBe('Merge pull request #12');
	});

	it('{pr} falls back when no PR number is known', () => {
		const ctx = makeCtx({ branchPRNumber: null });
		expect(substituteGitPromptPlaceholders('Merge {pr}', ctx)).toBe(
			'Merge the open pull request for this branch'
		);
	});

	it('leaves unknown tokens untouched', () => {
		const ctx = makeCtx();
		expect(substituteGitPromptPlaceholders('Keep {foo} and {branch}', ctx)).toBe(
			'Keep {foo} and feature-x'
		);
	});

	it('substitutes every occurrence', () => {
		const ctx = makeCtx({ branch: 'b' });
		expect(substituteGitPromptPlaceholders('{branch}-{branch}', ctx)).toBe('b-b');
	});
});

describe('buildGitActionPrompt — custom overrides', () => {
	it('uses the custom message (substituted) instead of the default', () => {
		const ctx = makeCtx({
			branch: 'feat',
			branchPRNumber: 9,
			prefs: makePrefs({ customMessages: { commit: 'Commit on {branch}, then merge {pr}' } }),
		});
		expect(buildGitActionPrompt('commit', ctx)).toBe('Commit on feat, then merge pull request #9');
	});

	it('falls back to default for an empty custom message', () => {
		const ctx = makeCtx({ prefs: makePrefs({ customMessages: { push: '' } }) });
		expect(buildGitActionPrompt('push', ctx)).toBe(getDefaultGitActionPrompt('push', ctx));
	});

	it('falls back to default for a whitespace-only custom message', () => {
		const ctx = makeCtx({ prefs: makePrefs({ customMessages: { push: '   \n\t ' } }) });
		expect(buildGitActionPrompt('push', ctx)).toBe(getDefaultGitActionPrompt('push', ctx));
	});

	it('uses the default when no custom message is set', () => {
		const ctx = makeCtx();
		expect(buildGitActionPrompt('finish', ctx)).toBe(getDefaultGitActionPrompt('finish', ctx));
	});

	it('a custom message replaces the structured default entirely', () => {
		const ctx = makeCtx({
			prefs: makePrefs({ mergeMethod: 'rebase', customMessages: { finish: 'Just merge it.' } }),
		});
		expect(buildGitActionPrompt('finish', ctx)).toBe('Just merge it.');
	});
});

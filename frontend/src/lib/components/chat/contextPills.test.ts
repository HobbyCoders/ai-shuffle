/**
 * Tests for the ContextPillBar read-only context badge gating.
 *
 * Regression guard for the "worktree badge wasn't moved to the pills" fix: the
 * project/worktree badge that used to live in the page header must now appear in
 * the pill bar in exactly the case the interactive branch pill can't cover —
 * an unauthenticated session — and must stay hidden whenever the interactive
 * branch pill is showing or the chat is empty. (Cleaned-up worktree sessions
 * keep the normal branch pill, so they no longer surface a badge.)
 */

import { describe, it, expect } from 'vitest';
import {
	computeContextBadge,
	shouldShowEmptyStateBranchPill,
	type ContextBadgeInput,
	type EmptyStateBranchInput
} from './contextPills';

/** A live, authenticated worktree session with one message — branch pill case. */
function base(overrides: Partial<ContextBadgeInput> = {}): ContextBadgeInput {
	return {
		messageCount: 1,
		projectName: 'my-app',
		executionMode: 'worktree',
		selectedWorktreeBranch: null,
		worktreeBranch: 'feat/login',
		githubAuthenticated: true,
		...overrides
	};
}

describe('computeContextBadge', () => {
	it('returns null in the empty/new-session state (mode toggle owns that row)', () => {
		expect(computeContextBadge(base({ messageCount: 0 }))).toBeNull();
		// Even an unauthenticated session shows nothing before the first message.
		expect(
			computeContextBadge(base({ messageCount: 0, githubAuthenticated: false }))
		).toBeNull();
	});

	it('returns null while the interactive branch pill is shown (authenticated)', () => {
		expect(computeContextBadge(base())).toBeNull();
		// Local mode, still authenticated → branch pill, no badge.
		expect(computeContextBadge(base({ executionMode: 'local', worktreeBranch: null }))).toBeNull();
	});

	it('returns null for a cleaned-up worktree when authenticated (branch pill takes over)', () => {
		// The normal — read-only-in-worktree-mode — branch pill is shown for
		// cleaned-up worktree sessions now, so there is no separate badge.
		expect(computeContextBadge(base())).toBeNull();
	});

	it('prefers the explicitly selected worktree branch over the create branch', () => {
		// Branch preference only matters where the badge surfaces a branch — i.e.
		// the unauthenticated worktree path.
		const badge = computeContextBadge(
			base({
				githubAuthenticated: false,
				selectedWorktreeBranch: 'fix/bug',
				worktreeBranch: 'feat/login'
			})
		);
		expect(badge!.branch).toBe('fix/bug');
	});

	it('shows project + branch when GitHub CLI is not authenticated (worktree)', () => {
		const badge = computeContextBadge(base({ githubAuthenticated: false }));
		expect(badge).not.toBeNull();
		expect(badge!.branch).toBe('feat/login');
		expect(badge!.title).toBe('my-app · Worktree branch: feat/login');
	});

	it('shows project-only when unauthenticated in local mode (no branch)', () => {
		const badge = computeContextBadge(
			base({ githubAuthenticated: false, executionMode: 'local', worktreeBranch: null })
		);
		expect(badge).not.toBeNull();
		expect(badge!.branch).toBeNull();
		expect(badge!.title).toBe('Project: my-app');
	});

	it('omits the project prefix from the title when the name is unknown', () => {
		const badge = computeContextBadge(base({ githubAuthenticated: false, projectName: null }));
		expect(badge!.title).toBe('Worktree branch: feat/login');
	});

	it('returns null when authenticated, no project/branch context exists', () => {
		// Authenticated + live but somehow no project → branch pill territory, not a badge.
		expect(
			computeContextBadge(base({ executionMode: 'local', worktreeBranch: null, projectName: null }))
		).toBeNull();
	});

	it('a non-worktree (local) session never reports a worktree branch', () => {
		const badge = computeContextBadge(
			base({ githubAuthenticated: false, executionMode: 'local', worktreeBranch: 'feat/login' })
		);
		// Branch is ignored outside worktree mode → project-only badge.
		expect(badge!.branch).toBeNull();
		expect(badge!.title).toBe('Project: my-app');
	});

	it('an authenticated cleaned-up worktree shows no badge regardless of branch', () => {
		// Authentication alone hands the slot to the branch pill — even with no
		// branch resolved yet, there is no badge.
		expect(
			computeContextBadge(base({ worktreeBranch: null, selectedWorktreeBranch: null }))
		).toBeNull();
	});

	it('shows a cloud badge for an active cloud session (even when authenticated)', () => {
		// Cloud sessions run on the second install; the badge marks that clearly
		// regardless of GitHub auth, and carries no local branch.
		const badge = computeContextBadge(
			base({ executionMode: 'cloud', worktreeBranch: null, selectedWorktreeBranch: null })
		);
		expect(badge).not.toBeNull();
		expect(badge!.cloud).toBe(true);
		expect(badge!.branch).toBeNull();
		expect(badge!.title).toBe('my-app · Running on the cloud install');
	});

	it('cloud badge drops the project prefix when the name is unknown', () => {
		const badge = computeContextBadge(
			base({ executionMode: 'cloud', projectName: null })
		);
		expect(badge!.cloud).toBe(true);
		expect(badge!.title).toBe('Running on the cloud install');
	});
});

/** A fresh new-session, authenticated, local-mode state with a project. */
function branchBase(overrides: Partial<EmptyStateBranchInput> = {}): EmptyStateBranchInput {
	return {
		messageCount: 0,
		project: 'proj-1',
		githubAuthenticated: true,
		executionMode: 'local',
		...overrides
	};
}

describe('shouldShowEmptyStateBranchPill', () => {
	it('shows on the new-session screen in local mode (authenticated + project)', () => {
		expect(shouldShowEmptyStateBranchPill(branchBase())).toBe(true);
	});

	it('hides in worktree mode — the ExecutionModeBar owns the branch picker there', () => {
		expect(shouldShowEmptyStateBranchPill(branchBase({ executionMode: 'worktree' }))).toBe(false);
	});

	it('hides in cloud mode — the worker picks the branch', () => {
		expect(shouldShowEmptyStateBranchPill(branchBase({ executionMode: 'cloud' }))).toBe(false);
	});

	it('hides once a chat is active (branch pill / context badge owns that slot)', () => {
		expect(shouldShowEmptyStateBranchPill(branchBase({ messageCount: 1 }))).toBe(false);
	});

	it('hides without a project', () => {
		expect(shouldShowEmptyStateBranchPill(branchBase({ project: null }))).toBe(false);
	});

	it('hides when GitHub CLI is not authenticated', () => {
		expect(shouldShowEmptyStateBranchPill(branchBase({ githubAuthenticated: false }))).toBe(false);
	});

	it('hides when the project folder is not a git repository', () => {
		expect(shouldShowEmptyStateBranchPill(branchBase({ isGitRepo: false }))).toBe(false);
	});

	it('shows when explicitly a git repo (local + authenticated + project)', () => {
		expect(shouldShowEmptyStateBranchPill(branchBase({ isGitRepo: true }))).toBe(true);
	});

	it('defaults to showing when the git-repo signal is unknown (pre-load)', () => {
		// isGitRepo omitted ⇒ optimistic default of true, matching the brief window
		// before git status loads.
		const { isGitRepo: _omit, ...rest } = branchBase();
		void _omit;
		expect(shouldShowEmptyStateBranchPill(rest)).toBe(true);
	});
});

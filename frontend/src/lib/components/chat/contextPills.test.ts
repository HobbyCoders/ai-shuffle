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
	shouldShowBranchPill,
	shouldShowEmptyStateBranchPill,
	type ActiveBranchPillInput,
	type ContextBadgeInput,
	type EmptyStateBranchInput
} from './contextPills';

/** A live, authenticated worktree session with one message — branch pill case. */
function base(overrides: Partial<ContextBadgeInput> = {}): ContextBadgeInput {
	return {
		messageCount: 1,
		projectName: 'my-app',
		isWorktree: true,
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
		expect(computeContextBadge(base({ isWorktree: false, worktreeBranch: null }))).toBeNull();
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
			base({ githubAuthenticated: false, isWorktree: false, worktreeBranch: null })
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
			computeContextBadge(base({ isWorktree: false, worktreeBranch: null, projectName: null }))
		).toBeNull();
	});

	it('a non-worktree (local) session never reports a worktree branch', () => {
		const badge = computeContextBadge(
			base({ githubAuthenticated: false, isWorktree: false, worktreeBranch: 'feat/login' })
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

	// V2 Q7: a degraded session (queued work, no runner online) is read-only.
	it('shows the read-only degraded badge when degraded is true', () => {
		const badge = computeContextBadge(base({ degraded: true }));
		expect(badge).not.toBeNull();
		expect(badge!.degraded).toBe(true);
		expect(badge!.branch).toBeNull();
		expect(badge!.title).toBe('Read-only — no runner online for this session');
	});

	it('degraded badge takes precedence over branch context and GitHub auth', () => {
		// Authenticated worktree with a branch would normally hand the slot to the
		// interactive branch pill (null badge); degraded overrides that entirely.
		const badge = computeContextBadge(base({ degraded: true, githubAuthenticated: true }));
		expect(badge!.degraded).toBe(true);
		expect(badge!.branch).toBeNull();
	});

	it('returns null in the empty/new-session state even when degraded', () => {
		// The empty/new-session compose case is owned by the execution-mode toggle
		// and covered by the board chip — the in-chat degraded badge only surfaces
		// once a chat is active (messageCount > 0).
		expect(computeContextBadge(base({ degraded: true, messageCount: 0 }))).toBeNull();
	});

	it('is not degraded when the flag is absent/false (normal badge behavior)', () => {
		// degraded omitted → falls through to the usual gating (branch-pill case).
		expect(computeContextBadge(base())).toBeNull();
		expect(computeContextBadge(base({ degraded: false, githubAuthenticated: false }))!.title).toBe(
			'my-app · Worktree branch: feat/login'
		);
	});

	// V2 Q8: a remote-placed session runs on another install — a read-only runner
	// badge names the owning device (and whether it's online) instead of a branch.
	it('shows the runner badge for a remote-placed session on an online device', () => {
		const badge = computeContextBadge(
			base({ isRemote: true, deviceName: 'Worker', deviceOnline: true })
		);
		expect(badge).not.toBeNull();
		expect(badge!.runner).toEqual({ deviceName: 'Worker', online: true });
		expect(badge!.branch).toBeNull();
		expect(badge!.title).toContain('Running on Worker');
		expect(badge!.title).not.toContain('(offline)');
	});

	it('marks the runner badge offline when the owning device is offline', () => {
		const badge = computeContextBadge(
			base({ isRemote: true, deviceName: 'Worker', deviceOnline: false })
		);
		expect(badge!.runner).toEqual({ deviceName: 'Worker', online: false });
		expect(badge!.title).toContain('(offline)');
	});

	it('falls back to "another device" when the remote device has no name', () => {
		const badge = computeContextBadge(base({ isRemote: true, deviceOnline: true }));
		expect(badge!.runner!.deviceName).toBe('another device');
		expect(badge!.title).toContain('Running on another device');
	});

	it('degraded badge wins over the remote runner badge', () => {
		const badge = computeContextBadge(
			base({ degraded: true, isRemote: true, deviceName: 'Worker', deviceOnline: true })
		);
		expect(badge!.degraded).toBe(true);
		expect(badge!.runner).toBeUndefined();
		expect(badge!.branch).toBeNull();
	});

	it('returns null for a remote-placed session before the first message', () => {
		expect(computeContextBadge(base({ messageCount: 0, isRemote: true }))).toBeNull();
	});
});

/** A fresh new-session, authenticated, local-mode state with a project. */
function branchBase(overrides: Partial<EmptyStateBranchInput> = {}): EmptyStateBranchInput {
	return {
		messageCount: 0,
		project: 'proj-1',
		githubAuthenticated: true,
		worktreeIntent: 'local',
		...overrides
	};
}

describe('shouldShowBranchPill', () => {
	function branchPill(overrides: Partial<ActiveBranchPillInput> = {}) {
		return shouldShowBranchPill({
			messageCount: 1,
			project: 'proj-1',
			githubAuthenticated: true,
			...overrides
		});
	}

	it('shows for a local authenticated project chat', () => {
		expect(branchPill()).toBe(true);
	});

	it('hides the checkout selector in a subagent chat (read and message only)', () => {
		expect(branchPill({ isSubagent: true })).toBe(false);
		expect(branchPill({ isSubagent: false })).toBe(true);
	});

	it('hides when the session is remote so the runner menu occupies the slot', () => {
		expect(branchPill({ isRemote: true })).toBe(false);
	});

	it('hides when the session is degraded so the read-only badge occupies the slot', () => {
		expect(branchPill({ degraded: true })).toBe(false);
	});

	it('hides without a project, without GitHub auth, or in the empty state', () => {
		expect(branchPill({ project: null })).toBe(false);
		expect(branchPill({ githubAuthenticated: false })).toBe(false);
		expect(branchPill({ messageCount: 0 })).toBe(false);
	});
});

describe('shouldShowEmptyStateBranchPill', () => {
	it('shows on the new-session screen in local mode (authenticated + project)', () => {
		expect(shouldShowEmptyStateBranchPill(branchBase())).toBe(true);
	});

	it('hides in worktree mode — the ExecutionModeBar owns the branch picker there', () => {
		expect(shouldShowEmptyStateBranchPill(branchBase({ worktreeIntent: 'worktree' }))).toBe(false);
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

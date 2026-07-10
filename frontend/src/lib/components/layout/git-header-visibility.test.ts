/**
 * Tests for the chat header's local git-actions visibility gate.
 *
 * Regression guard for the "git action buttons show 'Init Repo' for cloud
 * sessions on another device" bug: a cloud/offloaded session's repo lives on the
 * owning install, so the LOCAL /git/status reports no repo and the header would
 * fall back to the misleading "Init Repo" state. The gate must suppress the local
 * git actions for cloud sessions while leaving local/worktree sessions unchanged.
 */

import { describe, it, expect } from 'vitest';
import {
	shouldShowHeaderGitActions,
	type HeaderGitActionsInput
} from './git-header-visibility';

/** A live, authenticated local chat session — git actions should show. */
function base(overrides: Partial<HeaderGitActionsInput> = {}): HeaderGitActionsInput {
	return {
		viewType: 'chat',
		hasProject: true,
		githubAuthenticated: true,
		worktreeDeleted: false,
		executionMode: 'local',
		...overrides
	};
}

describe('shouldShowHeaderGitActions', () => {
	it('shows local git actions for a local session', () => {
		expect(shouldShowHeaderGitActions(base())).toBe(true);
	});

	it('shows local git actions for a worktree session', () => {
		expect(shouldShowHeaderGitActions(base({ executionMode: 'worktree' }))).toBe(true);
	});

	it('HIDES local git actions for a cloud session (repo is on the owning install)', () => {
		// The core bug: without this, the local /git/status → is_git_repo:false
		// surfaces the "Init Repo" fallback for a remote session's repo.
		expect(shouldShowHeaderGitActions(base({ executionMode: 'cloud' }))).toBe(false);
	});

	it('hides when not a chat view', () => {
		expect(shouldShowHeaderGitActions(base({ viewType: 'board' }))).toBe(false);
		expect(shouldShowHeaderGitActions(base({ viewType: undefined }))).toBe(false);
	});

	it('hides when there is no project', () => {
		expect(shouldShowHeaderGitActions(base({ hasProject: false }))).toBe(false);
	});

	it('hides when GitHub CLI is not authenticated', () => {
		expect(shouldShowHeaderGitActions(base({ githubAuthenticated: false }))).toBe(false);
	});

	it('hides when the worktree directory is gone', () => {
		expect(shouldShowHeaderGitActions(base({ worktreeDeleted: true }))).toBe(false);
	});
});

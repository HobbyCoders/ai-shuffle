/**
 * Tests for the chat header's local git-actions visibility gate.
 *
 * Regression guard for the "git action buttons show 'Init Repo' for remote
 * sessions on another device" bug: a remote-placed session's repo lives on the
 * owning install, so the LOCAL /git/status reports no repo and the header would
 * fall back to the misleading "Init Repo" state. The gate must suppress the local
 * git actions for remote sessions while leaving local/worktree sessions unchanged.
 */

import { describe, it, expect } from 'vitest';
import {
	shouldShowHeaderGitActions,
	shouldShowGithubGitActions,
	type HeaderGitActionsInput
} from './git-header-visibility';

/** A live, authenticated local chat session — git actions should show. */
function base(overrides: Partial<HeaderGitActionsInput> = {}): HeaderGitActionsInput {
	return {
		viewType: 'chat',
		hasProject: true,
		githubAuthenticated: true,
		worktreeDeleted: false,
		isRemote: false,
		...overrides
	};
}

describe('shouldShowHeaderGitActions', () => {
	it('shows local git actions for a local session', () => {
		expect(shouldShowHeaderGitActions(base())).toBe(true);
	});

	it('shows local git actions for a local (non-remote) session', () => {
		expect(shouldShowHeaderGitActions(base({ isRemote: false }))).toBe(true);
	});

	it('HIDES local git actions for a remote session (repo is on the owning install)', () => {
		// The core bug: without this, the local /git/status → is_git_repo:false
		// surfaces the "Init Repo" fallback for a remote session's repo.
		expect(shouldShowHeaderGitActions(base({ isRemote: true }))).toBe(false);
	});

	it('hides when not a chat view', () => {
		expect(shouldShowHeaderGitActions(base({ viewType: 'board' }))).toBe(false);
		expect(shouldShowHeaderGitActions(base({ viewType: undefined }))).toBe(false);
	});

	it('hides when there is no project', () => {
		expect(shouldShowHeaderGitActions(base({ hasProject: false }))).toBe(false);
	});

	it('KEEPS local git actions when GitHub CLI auth blips (G10c)', () => {
		// Local git needs no GitHub auth. `githubAuthenticated` comes from a 120 s
		// /auth/status poll whose `gh auth status` transiently fails under load —
		// gating on it hid the whole git UI for up to two minutes.
		expect(shouldShowHeaderGitActions(base({ githubAuthenticated: false }))).toBe(true);
	});

	it('hides when the worktree directory is gone', () => {
		expect(shouldShowHeaderGitActions(base({ worktreeDeleted: true }))).toBe(false);
	});

	it('hides all git actions on mobile chat mode header', () => {
		expect(shouldShowHeaderGitActions(base({ isMobile: true }))).toBe(false);
	});

	it('keeps git actions on desktop when isMobile is false/omitted', () => {
		expect(shouldShowHeaderGitActions(base({ isMobile: false }))).toBe(true);
		expect(shouldShowHeaderGitActions(base())).toBe(true);
	});
});

describe('shouldShowGithubGitActions', () => {
	it('shows GitHub-backed actions when GitHub is available', () => {
		expect(shouldShowGithubGitActions(base({ githubAvailable: true }))).toBe(true);
	});

	it('hides GitHub-backed actions when GitHub is unavailable', () => {
		expect(shouldShowGithubGitActions(base({ githubAvailable: false }))).toBe(false);
	});

	it('prefers the debounced availability flag over the raw auth poll', () => {
		// A single failed `gh auth status` leaves githubAvailable true (hysteresis
		// in the git store), and the GitHub actions must stay put.
		expect(
			shouldShowGithubGitActions(base({ githubAuthenticated: false, githubAvailable: true }))
		).toBe(true);
	});

	it('falls back to the raw auth verdict when no availability flag is supplied', () => {
		expect(shouldShowGithubGitActions(base({ githubAuthenticated: false }))).toBe(false);
		expect(shouldShowGithubGitActions(base({ githubAuthenticated: true }))).toBe(true);
	});

	it('still respects the local gate (remote session hides everything)', () => {
		expect(
			shouldShowGithubGitActions(base({ isRemote: true, githubAvailable: true }))
		).toBe(false);
	});

	it('still respects the local gate (mobile hides everything)', () => {
		expect(
			shouldShowGithubGitActions(base({ isMobile: true, githubAvailable: true }))
		).toBe(false);
	});
});

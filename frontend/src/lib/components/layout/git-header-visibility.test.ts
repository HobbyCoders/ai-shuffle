import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
	shouldShowHeaderGitActions,
	shouldShowGithubGitActions,
	shouldShowGitPanelTrigger,
	type HeaderGitActionsInput
} from './git-header-visibility';

/** A live, authenticated local chat session — git actions should show. */
function base(overrides: Partial<HeaderGitActionsInput> = {}): HeaderGitActionsInput {
	return {
		viewType: 'session',
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

	it('shows owner-routed Git actions for a remote session', () => {
		expect(shouldShowHeaderGitActions(base({ isRemote: true }))).toBe(true);
	});

	it('hides when not a session view', () => {
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

	it('hides git actions for a chat-lane session (no repository)', () => {
		expect(shouldShowHeaderGitActions(base({ sessionMode: 'chat' }))).toBe(false);
	});

	it('keeps git actions for a code-lane session, including mobile-width', () => {
		expect(shouldShowHeaderGitActions(base({ sessionMode: 'code' }))).toBe(true);
	});

	it('keeps git actions for a voice-lane session', () => {
		expect(shouldShowHeaderGitActions(base({ sessionMode: 'voice' }))).toBe(true);
	});

	it('does not hide when sessionMode is omitted (fail open for code)', () => {
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

	it('shows Finish for a remote session with GitHub access', () => {
		expect(
			shouldShowGithubGitActions(base({ isRemote: true, githubAvailable: true }))
		).toBe(true);
	});

	it('still respects the local gate (chat lane hides everything)', () => {
		expect(
			shouldShowGithubGitActions(base({ sessionMode: 'chat', githubAvailable: true }))
		).toBe(false);
	});

	it('does not hide GitHub actions (Finish) on code or voice', () => {
		expect(
			shouldShowGithubGitActions(base({ sessionMode: 'code', githubAvailable: true }))
		).toBe(true);
		expect(
			shouldShowGithubGitActions(base({ sessionMode: 'voice', githubAvailable: true }))
		).toBe(true);
	});
});

// #1816: private access left the Git panel. The header's private-access icon is
// the one place to approve, review and revoke on every lane, so MainPanel always
// renders ChatAccess (icon + panel), and no standalone Swarm button.
describe('chat header private access and swarms', () => {
	const mainPanel = readFileSync(resolve(process.cwd(), 'src/lib/components/layout/MainPanel.svelte'), 'utf8')
		.replace(/<!--[\s\S]*?-->/g, '');

	it('gives every session its own private-access control', () => {
		expect(mainPanel).toContain('<ChatAccess sessionId={activeTab.sessionId} />');
	});

	it('renders no swarm dropdown in the header', () => {
		expect(mainPanel).not.toContain('SwarmDropdown');
	});
});

// #1615: a worktree session must keep the panel through every "not a repo" reading.
describe('shouldShowGitPanelTrigger', () => {
	it('shows the panel for a repository', () => {
		expect(shouldShowGitPanelTrigger({ isGitRepo: true, worktreeMode: false, error: null })).toBe(true);
	});

	it('keeps the panel for a worktree session whose status reads "not a repository"', () => {
		expect(shouldShowGitPanelTrigger({ isGitRepo: false, worktreeMode: true, error: null })).toBe(true);
	});

	it('leaves a plain non-repository project to Init Repo', () => {
		expect(shouldShowGitPanelTrigger({ isGitRepo: false, worktreeMode: false, error: null })).toBe(false);
	});

	it('leaves a load error to the Retry button', () => {
		expect(shouldShowGitPanelTrigger({ isGitRepo: true, worktreeMode: true, error: 'boom' })).toBe(false);
		expect(shouldShowGitPanelTrigger({ isGitRepo: true, worktreeMode: false, error: undefined })).toBe(true);
	});
});

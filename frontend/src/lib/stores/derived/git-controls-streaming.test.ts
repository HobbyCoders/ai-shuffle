/**
 * Tests for the header git-controls streaming lock.
 *
 * Regression guard for "the Ship It button must be disabled during streaming":
 * the buttons' `disabled` state derives from gitControlsStreaming, which is this
 * pure function. If it ever stops reporting `true` while the active session is
 * streaming, Ship It (and Commit/Push/PR/Finish) would become clickable mid-run
 * and sendMessage's interrupt path would abort the live generation.
 */

import { describe, it, expect } from 'vitest';
import {
	computeGitControlsStreaming,
	type GitControlsStreamingInput,
	type CrossDeviceStreamingInfo
} from './git-controls-streaming';

function input(overrides: Partial<GitControlsStreamingInput> = {}): GitControlsStreamingInput {
	return {
		activeIsStreaming: false,
		activeIsWorktree: false,
		activeProject: 'proj-1',
		crossDeviceStreaming: new Map<string, CrossDeviceStreamingInfo>(),
		...overrides
	};
}

describe('computeGitControlsStreaming', () => {
	it('locks when the active session\'s own streaming flag is set (local mode)', () => {
		expect(computeGitControlsStreaming(input({ activeIsStreaming: true }))).toBe(true);
	});

	it('locks when the active session\'s own streaming flag is set (worktree mode)', () => {
		// The Ship It scenario: a worktree session generating must lock its controls.
		expect(
			computeGitControlsStreaming(input({ activeIsWorktree: true, activeIsStreaming: true }))
		).toBe(true);
	});

	it('is idle when nothing is streaming', () => {
		expect(computeGitControlsStreaming(input())).toBe(false);
	});

	it('worktree mode ignores cross-session streaming in the same project', () => {
		// Independent working dir — another session streaming can't touch it.
		expect(
			computeGitControlsStreaming(
				input({
					activeIsWorktree: true,
					crossDeviceStreaming: new Map([['other', { projectId: 'proj-1', worktreeId: null }]])
				})
			)
		).toBe(false);
	});

	it('project mode locks when another non-worktree session streams in the same project', () => {
		expect(
			computeGitControlsStreaming(
				input({
					crossDeviceStreaming: new Map([['other', { projectId: 'proj-1', worktreeId: null }]])
				})
			)
		).toBe(true);
	});

	it('project mode ignores a worktree session streaming in the same project', () => {
		// A worktree session has its own checkout, so it doesn't lock project controls.
		expect(
			computeGitControlsStreaming(
				input({
					crossDeviceStreaming: new Map([['other', { projectId: 'proj-1', worktreeId: 'wt-9' }]])
				})
			)
		).toBe(false);
	});

	it('project mode ignores a v2 BRANCH session streaming in the same project', () => {
		// Regression: v2 branch-intake sessions have NO legacy worktree row
		// (worktree_id NULL) — their checkout is identified by sessions.branch,
		// carried on the streaming frame as worktree_branch. Before this test
		// such a session wrongly locked every main-checkout session's controls.
		expect(
			computeGitControlsStreaming(
				input({
					crossDeviceStreaming: new Map([
						['other', { projectId: 'proj-1', worktreeId: null, worktreeBranch: 'feat/login' }]
					])
				})
			)
		).toBe(false);
	});

	it('project mode ignores streaming in a *different* project', () => {
		expect(
			computeGitControlsStreaming(
				input({
					crossDeviceStreaming: new Map([['other', { projectId: 'proj-2', worktreeId: null }]])
				})
			)
		).toBe(false);
	});

	it('does not consult cross-session streaming when the active session has no project', () => {
		expect(
			computeGitControlsStreaming(
				input({
					activeProject: null,
					crossDeviceStreaming: new Map([['other', { projectId: 'proj-1', worktreeId: null }]])
				})
			)
		).toBe(false);
	});
});

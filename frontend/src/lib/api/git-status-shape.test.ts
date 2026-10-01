/**
 * Tests for the git API's canonical wire→app transforms (finding G7).
 *
 * The declared `GitStatus` used to say `staged`/`modified`/`untracked` were
 * numbers while the backend sent LISTS, so the store's "skip the update when
 * nothing changed" check compared fresh array references and never matched — a
 * store write and a full subscriber cascade fired on every 2 s poll. These tests
 * pin the canonical shape, the equality check that makes suppression real, and
 * the PR-lookup-failure flag the store depends on.
 */

import { describe, it, expect } from 'vitest';
import {
	transformGitStatus,
	gitStatusEqual,
	notARepoStatus,
	mergeStatusRepoMeta,
	transformPrState,
	prStateEqual
} from './git';

const WIRE = {
	is_git_repo: true,
	current_branch: 'feat/x',
	is_detached: false,
	head_commit: 'abc123',
	remote_url: 'git@github.com:o/r.git',
	is_clean: false,
	staged: [{ file: 'a.ts', status: 'M' }],
	modified: ['b.ts', 'c.ts'],
	untracked: ['d.ts'],
	conflicts: [],
	ahead: 2,
	behind: 0,
	differs_from_base: true,
	repository_id: 'repo-1',
	default_branch: 'master',
	github_repo_name: 'o/r'
};

describe('owner activity status', () => {
	it('notifies on activity changes even when the file/branch state is unchanged', () => {
		const idle = transformGitStatus({ ...WIRE, mutations_blocked: false });
		const busy = transformGitStatus({ ...WIRE, mutations_blocked: true });
		expect(gitStatusEqual(idle, busy)).toBe(false);
	});
	it('keeps the last activity check when an inline response omits it', () => {
		const busy = transformGitStatus({ ...WIRE, mutations_blocked: true });
		const inline = transformGitStatus({ is_git_repo: true });
		expect(mergeStatusRepoMeta(busy, inline).mutations_blocked).toBe(true);
		expect(mergeStatusRepoMeta(busy, transformGitStatus({ ...WIRE, mutations_blocked: false })).mutations_blocked).toBe(false);
	});
});

describe('transformGitStatus', () => {
	it('collapses the wire lists into counts plus a fingerprint', () => {
		const s = transformGitStatus(WIRE);
		expect(s.staged).toBe(1);
		expect(s.modified).toBe(2);
		expect(s.untracked).toBe(1);
		expect(s.conflicts).toBe(0);
		expect(s.has_changes).toBe(true);
		expect(typeof s.files_fingerprint).toBe('string');
	});

	it('carries the repository record through (default_branch is authoritative)', () => {
		expect(transformGitStatus(WIRE).default_branch).toBe('master');
	});

	it('defaults a sparse payload without throwing', () => {
		const s = transformGitStatus({ is_git_repo: false });
		expect(s.current_branch).toBeNull();
		expect(s.has_changes).toBe(false);
		expect(s.is_clean).toBe(true);
		expect(s.default_branch).toBeNull();
	});

	it('notARepoStatus has exactly the same shape as a real status', () => {
		expect(Object.keys(notARepoStatus()).sort()).toEqual(
			Object.keys(transformGitStatus(WIRE)).sort()
		);
		expect(notARepoStatus().is_git_repo).toBe(false);
	});
});

describe('gitStatusEqual', () => {
	it('is true for two independently-built statuses describing the same tree', () => {
		// The exact case reference equality could never catch: same data, fresh
		// objects and arrays on every poll.
		expect(gitStatusEqual(transformGitStatus(WIRE), transformGitStatus({ ...WIRE }))).toBe(true);
	});

	it('is false when the file lists differ but the counts do not', () => {
		const a = transformGitStatus(WIRE);
		const b = transformGitStatus({ ...WIRE, modified: ['b.ts', 'zz.ts'] });
		expect(a.modified).toBe(b.modified);
		expect(gitStatusEqual(a, b)).toBe(false);
	});

	it('is false when a scalar differs', () => {
		expect(
			gitStatusEqual(transformGitStatus(WIRE), transformGitStatus({ ...WIRE, ahead: 3 }))
		).toBe(false);
	});

	it('preserves unpushed null (never pushed) as distinct from 0 (fully pushed)', () => {
		const never = transformGitStatus({ ...WIRE, unpushed: null });
		const fully = transformGitStatus({ ...WIRE, unpushed: 0 });
		expect(never.unpushed).toBeNull();
		expect(fully.unpushed).toBe(0);
		expect(gitStatusEqual(never, fully)).toBe(false);
	});

	it('handles nulls', () => {
		expect(gitStatusEqual(null, null)).toBe(true);
		expect(gitStatusEqual(null, transformGitStatus(WIRE))).toBe(false);
	});
});

describe('mergeStatusRepoMeta', () => {
	it('carries repo-record fields onto an inline mutation payload', () => {
		const prev = transformGitStatus(WIRE);
		// What the mutation endpoints return: git_service.get_status only.
		const inline = transformGitStatus({ is_git_repo: true, current_branch: 'feat/x', ahead: 3 });
		expect(inline.default_branch).toBeNull();

		const merged = mergeStatusRepoMeta(prev, inline);
		expect(merged.default_branch).toBe('master');
		expect(merged.repository_id).toBe('repo-1');
		expect(merged.ahead).toBe(3);
	});

	it('leaves a full status alone', () => {
		const prev = transformGitStatus({ ...WIRE, default_branch: 'old' });
		const next = transformGitStatus(WIRE);
		expect(mergeStatusRepoMeta(prev, next).default_branch).toBe('master');
	});
});

describe('transformPrState', () => {
	it('accepts the backend flag under either spelling', () => {
		expect(transformPrState({ pr_lookup_failed: true }).lookup_failed).toBe(true);
		expect(transformPrState({ lookup_failed: true }).lookup_failed).toBe(true);
	});

	it('carries the backend "served a last-known-good value" companion flag', () => {
		const s = transformPrState({
			current_branch: 'feat/x',
			has_github: true,
			open_pr: { number: 5, title: 't', url: 'u', draft: false, state: 'OPEN', mergeable: null },
			pr_lookup_failed: true,
			stale: true
		});
		expect(s.lookup_failed).toBe(true);
		expect(s.served_stale).toBe(true);
		expect(s.open_pr?.number).toBe(5);
	});

	it('degrades gracefully when the backend has not shipped the flag', () => {
		// An older backend sends no flag at all — treat the lookup as completed.
		const s = transformPrState({ current_branch: 'feat/x', has_github: true, open_pr: null });
		expect(s.lookup_failed).toBe(false);
		expect(s.served_stale).toBe(false);
		expect(s.open_pr).toBeNull();
	});
});

describe('prStateEqual', () => {
	const pr = { number: 7, title: 't', url: 'u', draft: false, state: 'OPEN', mergeable: null };

	it('deep-compares the nested open_pr rebuilt on every poll', () => {
		expect(
			prStateEqual(
				transformPrState({ current_branch: 'feat/x', has_github: true, open_pr: { ...pr } }),
				transformPrState({ current_branch: 'feat/x', has_github: true, open_pr: { ...pr } })
			)
		).toBe(true);
	});

	it('notices a mergeability verdict landing', () => {
		expect(
			prStateEqual(
				transformPrState({ has_github: true, open_pr: { ...pr, mergeable: null } }),
				transformPrState({ has_github: true, open_pr: { ...pr, mergeable: true } })
			)
		).toBe(false);
	});

	it('notices the lookup-failed flag changing', () => {
		expect(
			prStateEqual(
				transformPrState({ has_github: true, open_pr: { ...pr } }),
				transformPrState({ has_github: true, open_pr: { ...pr }, lookup_failed: true })
			)
		).toBe(false);
	});
});

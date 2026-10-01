import { describe, expect, it } from 'vitest';
import {
	GIT_IDLE_DIFF_STATS_MS,
	GIT_IDLE_THRESHOLD_MS,
	GIT_POLL_ACTIVE_MS,
	GIT_POLL_IDLE_MS,
	gitPollIntervalMs,
	isGitActive,
	shouldExecuteGitPollTick,
	shouldPollDiffStats,
	shouldRunGitPoll
} from './git-poll-cadence';

const NOW = 1_000_000;

describe('git poll cadence (F3)', () => {
	it('polls responsively inside the activity window', () => {
		expect(isGitActive(NOW, NOW - 1_000)).toBe(true);
		expect(gitPollIntervalMs(NOW, NOW - 1_000)).toBe(GIT_POLL_ACTIVE_MS);
	});

	it('keeps an open menu responsive even after the activity window', () => {
		expect(gitPollIntervalMs(NOW, NOW - 10 * GIT_IDLE_THRESHOLD_MS, true)).toBe(GIT_POLL_ACTIVE_MS);
		expect(gitPollIntervalMs(NOW, NOW - 10 * GIT_IDLE_THRESHOLD_MS, false)).toBe(GIT_POLL_IDLE_MS);
	});

	it('falls back to a cheap idle cadence once activity stops', () => {
		expect(isGitActive(NOW, NOW - GIT_IDLE_THRESHOLD_MS)).toBe(false);
		expect(gitPollIntervalMs(NOW, NOW - GIT_IDLE_THRESHOLD_MS)).toBe(GIT_POLL_IDLE_MS);
	});

	it('idle cadence is long enough to stop hammering the 2s backend status cache', () => {
		// The regression this guards: a 5s idle poll against a 2s server-side
		// cache TTL is a guaranteed miss → a full worktree scan every tick.
		expect(GIT_POLL_IDLE_MS).toBeGreaterThanOrEqual(30_000);
	});

	it('never polls a hidden tab or an off-screen git UI', () => {
		expect(shouldRunGitPoll({ gitUiVisible: true, documentHidden: false })).toBe(true);
		expect(shouldRunGitPoll({ gitUiVisible: true, documentHidden: true })).toBe(false);
		expect(shouldRunGitPoll({ gitUiVisible: false, documentHidden: false })).toBe(false);
	});

	it('skips a scheduled tick during a git operation or a gone worktree', () => {
		expect(shouldExecuteGitPollTick({ operationInProgress: false, worktreeGone: false })).toBe(true);
		expect(shouldExecuteGitPollTick({ operationInProgress: true, worktreeGone: false })).toBe(false);
		expect(shouldExecuteGitPollTick({ operationInProgress: false, worktreeGone: true })).toBe(false);
	});
});

describe('idle diff-stats throttle (G8)', () => {
	it('refreshes diff stats on every tick while active', () => {
		expect(shouldPollDiffStats(NOW, NOW - 1_000, NOW - 1_000)).toBe(true);
	});

	it('does NOT run a full-tree diff on every idle tick', () => {
		const lastActivity = NOW - 10 * 60_000; // long idle
		const lastDiff = NOW - GIT_POLL_IDLE_MS; // one idle tick ago
		// The bug: the gate compared elapsed time against the tick interval
		// itself, so this was always true and two full-tree diffs ran per tick.
		expect(GIT_IDLE_DIFF_STATS_MS).toBeGreaterThan(GIT_POLL_IDLE_MS);
		expect(shouldPollDiffStats(NOW, lastActivity, lastDiff)).toBe(false);
	});

	it('still refreshes idle diff stats eventually, so values do not go stale forever', () => {
		const lastActivity = NOW - 10 * 60_000;
		expect(shouldPollDiffStats(NOW, lastActivity, NOW - GIT_IDLE_DIFF_STATS_MS)).toBe(true);
	});

	it('an hour of idling costs a bounded number of diffs', () => {
		const lastActivity = 0;
		let lastDiff = 0;
		let diffs = 0;
		for (let t = 0; t <= 3_600_000; t += GIT_POLL_IDLE_MS) {
			if (shouldPollDiffStats(t, lastActivity, lastDiff)) {
				diffs += 1;
				lastDiff = t;
			}
		}
		// Old behaviour: one per 5s tick = 720 full-tree diffs an hour.
		expect(diffs).toBeLessThanOrEqual(61);
	});
});

/**
 * The teams bar used to poll `GET /api/v1/teams/{sessionId}` every 2 s for every
 * open session, forever, with no visibility gate — an auth check and a DB
 * session-access check per hit, for a feature almost no session uses
 * (PERFORMANCE_REVIEW §3.6 F4). These pin the replacement policy.
 */
import { describe, it, expect } from 'vitest';
import {
	TEAM_POLL_IDLE_MS,
	TEAM_POLL_RUNNING_MS,
	deriveTeamFeedSignal,
	resolveTeamPollIntervalMs
} from './team-status-poll';

describe('resolveTeamPollIntervalMs', () => {
	it('does not poll at all when the session has no teams', () => {
		expect(
			resolveTeamPollIntervalMs({ visible: true, hasTeams: false, hasRunning: false })
		).toBeNull();
	});

	it('does not poll a hidden tab, even with a running team', () => {
		expect(
			resolveTeamPollIntervalMs({ visible: false, hasTeams: true, hasRunning: true })
		).toBeNull();
	});

	it('keeps a fast cadence only while a teammate is running', () => {
		expect(resolveTeamPollIntervalMs({ visible: true, hasTeams: true, hasRunning: true })).toBe(
			TEAM_POLL_RUNNING_MS
		);
	});

	it('backs off hard once the teams are idle', () => {
		expect(resolveTeamPollIntervalMs({ visible: true, hasTeams: true, hasRunning: false })).toBe(
			TEAM_POLL_IDLE_MS
		);
		expect(TEAM_POLL_IDLE_MS).toBeGreaterThanOrEqual(30000);
	});
});

describe('deriveTeamFeedSignal', () => {
	it('reports no teams for an ordinary transcript', () => {
		const signal = deriveTeamFeedSignal([
			{ type: 'text' },
			{ type: 'tool_use' },
			{ type: 'subagent' }
		]);
		expect(signal).toEqual({ hasTeams: false, hasRunning: false, signature: '' });
	});

	it('reports teams and activity from the feed’s own team cards', () => {
		const signal = deriveTeamFeedSignal([
			{ type: 'text' },
			{ type: 'team_member', teamName: 'alpha', teammateName: 'one', teammateStatus: 'running' },
			{ type: 'team_member', teamName: 'alpha', teammateName: 'two', teammateStatus: 'completed' }
		]);
		expect(signal.hasTeams).toBe(true);
		expect(signal.hasRunning).toBe(true);
	});

	it('reports teams as idle once every teammate has settled', () => {
		const signal = deriveTeamFeedSignal([
			{ type: 'team_member', teamName: 'alpha', teammateName: 'one', teammateStatus: 'completed' }
		]);
		expect(signal.hasTeams).toBe(true);
		expect(signal.hasRunning).toBe(false);
	});

	it('changes its signature only when team state changes', () => {
		const base = [
			{ type: 'text' },
			{ type: 'team_member', teamName: 'alpha', teammateName: 'one', teammateStatus: 'running' }
		];
		const unchanged = [...base, { type: 'text' }, { type: 'tool_use' }];
		expect(deriveTeamFeedSignal(unchanged).signature).toBe(deriveTeamFeedSignal(base).signature);

		const settled = [
			{ type: 'text' },
			{ type: 'team_member', teamName: 'alpha', teammateName: 'one', teammateStatus: 'completed' }
		];
		expect(deriveTeamFeedSignal(settled).signature).not.toBe(deriveTeamFeedSignal(base).signature);
	});
});

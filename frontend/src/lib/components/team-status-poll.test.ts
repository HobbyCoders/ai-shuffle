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
	isTeamAccessRefused,
	resolveTeamPollIntervalMs
} from './team-status-poll';
import { listTeams } from '$lib/api/client';

// R9-17: an API key's web user gets 403 on a session it does not own. The bar
// kept asking every 2 s while the feed showed a running teammate.
describe('a refused teams request', () => {
	it('stops the poll even with a running team', () => {
		expect(
			resolveTeamPollIntervalMs({ visible: true, hasTeams: true, hasRunning: true, refused: true })
		).toBeNull();
	});

	it('is told apart from a transient failure', () => {
		expect(isTeamAccessRefused({ status: 403 })).toBe(true);
		expect(isTeamAccessRefused({ status: 404 })).toBe(true);
		expect(isTeamAccessRefused({ status: 500 })).toBe(false);
		expect(isTeamAccessRefused(new Error('network'))).toBe(false);
		expect(isTeamAccessRefused(null)).toBe(false);
	});

	it('carries the HTTP status on the listTeams error', async () => {
		const original = globalThis.fetch;
		globalThis.fetch = (async () => new Response('{}', { status: 403 })) as typeof fetch;
		try {
			const error = await listTeams('sess-1').catch((e: unknown) => e);
			expect(isTeamAccessRefused(error)).toBe(true);
		} finally {
			globalThis.fetch = original;
		}
	});
});

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

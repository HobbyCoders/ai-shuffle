import { describe, expect, it } from 'vitest';
import type { LimitStopped, SessionLimitResume } from '$lib/api/limit-resume';
import { limitBanner } from './limit-banner';

const NOW = Date.UTC(2026, 9, 25, 18, 0) / 1000;
const RESET = NOW + 3 * 3600;

function stopped(over: Partial<LimitStopped> = {}): LimitStopped {
	return { turn_id: 't1', provider: 'claude', reset_at: RESET, reset_known: true, can_arm: true, reason: null, ...over };
}
function state(over: Partial<SessionLimitResume> = {}): SessionLimitResume {
	return { session_id: 's1', pending: null, workers: [], limit_stopped: null, armed: false, resume_at: null, ...over };
}

describe('limitBanner', () => {
	it('shows nothing for a chat no limit stopped', () => {
		expect(limitBanner(state(), null, NOW)).toBeNull();
		expect(limitBanner(null, null, NOW)).toBeNull();
	});

	it('offers Resume at reset with the reset time', () => {
		const b = limitBanner(state({ limit_stopped: stopped() }), null, NOW);
		expect(b).toMatchObject({ kind: 'stopped', canArm: true, turnId: 't1', resetAt: RESET, key: 's1:t1' });
		expect(b?.message).toMatch(/^Usage limit reached\. Resets at \S/);
	});

	it('says the reset time is unknown and offers nothing to arm', () => {
		const b = limitBanner(state({ limit_stopped: stopped({ reset_at: null, reset_known: false, can_arm: false, reason: 'reset_unknown' }) }), null, NOW);
		expect(b).toMatchObject({ kind: 'stopped', canArm: false, resetAt: null });
		expect(b?.message).toBe('Usage limit reached. Reset time unknown');
	});

	it('keeps the reset time but no button once this limit was handled', () => {
		const b = limitBanner(state({ limit_stopped: stopped({ can_arm: false, reason: 'already_handled' }) }), null, NOW);
		expect(b).toMatchObject({ kind: 'stopped', canArm: false });
		expect(b?.message).toMatch(/Resets at/);
	});

	it('does not offer to wait for a reset that already passed', () => {
		const b = limitBanner(state({ limit_stopped: stopped({ reset_at: NOW - 60 }) }), null, NOW);
		expect(b).toMatchObject({ kind: 'stopped', canArm: false });
		expect(b?.message).toMatch(/^Usage limit reset at .+ Send a message to continue$/);
	});

	it.each(['busy', 'archived', 'moved', 'newer_turn', 'settled', 'stopped', 'task_worker'])(
		'hides a stop the chat moved past (%s)',
		(reason) => {
			expect(limitBanner(state({ limit_stopped: stopped({ can_arm: false, reason }) }), null, NOW)).toBeNull();
		}
	);

	it('says when an armed chat resumes', () => {
		const at = RESET + 120;
		const b = limitBanner(state({ armed: true, resume_at: at, pending: { fire_at: at }, limit_stopped: stopped({ can_arm: false }) }), null, NOW);
		expect(b).toMatchObject({ kind: 'armed', resumeAt: at });
		expect(b?.message).toMatch(/^Usage limit reached\. Resumes at \S/);
	});

	it('reads the armed time from pending on a worker without the new fields', () => {
		const at = RESET + 60;
		const old: SessionLimitResume = { session_id: 's1', pending: { fire_at: at }, workers: [] };
		expect(limitBanner(old, null, NOW)).toMatchObject({ kind: 'armed', resumeAt: at });
	});

	it("uses the transcript's open pause until the worker answers", () => {
		expect(limitBanner(null, { resumeAt: RESET }, NOW)).toMatchObject({ kind: 'armed', resumeAt: RESET });
	});

	it("trusts the worker over the transcript", () => {
		expect(limitBanner(state(), { resumeAt: RESET }, NOW)).toBeNull();
	});
});

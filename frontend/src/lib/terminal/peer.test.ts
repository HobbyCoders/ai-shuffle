import { describe, expect, it } from 'vitest';
import { age, codeGroups, countdown, isPeerTarget, localWorkerId, openErrorMessage, peerChip, peerReason, selectable } from './peer';
import type { TerminalTarget } from '$lib/api/terminals';

const row = (over: Partial<TerminalTarget>): TerminalTarget => ({ kind: 'worker', worker_id: 'b', name: 'B', available: false, ...over });

describe('peer target rules', () => {
	it('treats only other worker rows as peers', () => {
		const targets = [row({ worker_id: 'a', available: true }), row({}), { kind: 'ssh', grant_id: 'g', name: 'H', available: true }];
		const local = localWorkerId(targets);
		expect(local).toBe('a');
		expect(targets.map((t) => isPeerTarget(t, local))).toEqual([false, true, false]);
	});

	it('lets the owner pick a usable or approvable worker only', () => {
		expect(selectable(row({ reason_code: 'recipient_approval_required' }))).toBe(true);
		expect(selectable(row({ available: true, authorization_id: 'x' }))).toBe(true);
		for (const code of ['peer_offline', 'peer_terminals_disabled', 'peer_unsupported_or_disabled', 'not_supported', 'https_required', 'peer_unavailable']) {
			expect(selectable(row({ reason_code: code }))).toBe(false);
		}
	});

	it('labels every contract reason and keeps the server reason text', () => {
		expect(peerChip(row({ reason_code: 'peer_offline' }))).toEqual({ label: 'Offline', tone: 'off' });
		expect(peerChip(row({ reason_code: 'https_required' })).label).toBe('Needs HTTPS');
		expect(peerChip(row({ reason_code: 'something_new' })).label).toBe('Unavailable');
		expect(peerChip(row({ available: true, authorization_id: 'x' })).label).toBe('Approved');
		expect(peerReason(row({ reason_code: 'peer_offline', reason: 'The peer worker is offline.' }))).toBe('The peer worker is offline.');
		expect(peerReason(row({ reason_code: 'recipient_approval_required' }))).toBe('Approve on B to open one shell.');
	});
});

describe('formatting', () => {
	it('counts down and ages requests', () => {
		expect(countdown(120)).toBe('2:00');
		expect(countdown(59.2)).toBe('1:00');
		expect(countdown(-3)).toBe('0:00');
		expect(age(1)).toBe('just now');
		expect(age(22)).toBe('22s ago');
		expect(age(64)).toBe('1m 4s ago');
	});

	it('splits the code into two groups of three', () => {
		expect(codeGroups('k7q2xm')).toEqual(['K7Q', '2XM']);
	});

	it('explains failed opens without losing the server message', () => {
		expect(openErrorMessage('peer_unavailable', 'x', 'B')).toBe('B stopped answering before the shell opened.');
		expect(openErrorMessage('recipient_approval_required', 'x', 'B')).toBe('The approval on B expired or was revoked.');
		expect(openErrorMessage('other', '', 'B')).toBe("The shell on B didn't open.");
	});
});

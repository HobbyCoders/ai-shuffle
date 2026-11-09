import { describe, expect, it } from 'vitest';
import { age, codeGroups, countdown, isPeerTarget, localWorkerId, openErrorMessage, peerChip, peerReason, peerRow, selectable } from './peer';
import type { TerminalTarget } from '$lib/api/terminals';

const row = (over: Partial<TerminalTarget>): TerminalTarget => ({ kind: 'worker', worker_id: 'b', name: 'B', available: false, ...over });

describe('peer target rules', () => {
	it('treats only other worker rows as peers', () => {
		const targets = [row({ worker_id: 'a', available: true }), row({}), { kind: 'ssh', grant_id: 'g', name: 'H', available: true }];
		const local = localWorkerId(targets);
		expect(local).toBe('a');
		expect(targets.map((t) => isPeerTarget(t, local))).toEqual([false, true, false]);
	});

	it('lets the owner pick an available worker only, and only when this browser can sign', () => {
		expect(selectable(row({ available: true }))).toBe(true);
		expect(selectable(row({ available: true }), 'No key here')).toBe(false);
		for (const code of ['peer_offline', 'peer_terminals_disabled', 'peer_unsupported_or_disabled', 'not_supported', 'https_required', 'peer_unavailable']) {
			expect(selectable(row({ reason_code: code }))).toBe(false);
		}
	});

	it('labels every contract reason and keeps the server reason text', () => {
		expect(peerChip(row({ reason_code: 'peer_offline' }))).toEqual({ label: 'Offline', tone: 'off' });
		expect(peerChip(row({ reason_code: 'https_required' })).label).toBe('Needs HTTPS');
		expect(peerChip(row({ reason_code: 'something_new' })).label).toBe('Unavailable');
		expect(peerChip(row({ available: true })).label).toBe('Ready');
		expect(peerReason(row({ reason_code: 'peer_offline', reason: 'The peer worker is offline.' }))).toBe('The peer worker is offline.');
	});

	it('says why this browser cannot sign, and shows a link in progress on the row', () => {
		const ready = row({ available: true });
		expect(peerRow(ready)).toEqual({ chip: { label: 'Ready', tone: 'ok' }, reason: null });
		expect(peerRow(ready, null, 'No key here')).toEqual({ chip: { label: 'Needs a secure connection', tone: 'warn' }, reason: 'No key here' });
		expect(peerRow(ready, 'linking')).toEqual({ chip: { label: 'Linking', tone: 'wait' }, reason: 'Waiting for the code to be entered on B.' });
		expect(peerRow(row({ reason_code: 'peer_offline', reason: 'Offline.' }), 'ended')).toEqual({ chip: { label: 'Offline', tone: 'off' }, reason: 'Offline.' });
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

	it('splits the link code into groups of four', () => {
		expect(codeGroups('abcd2345efgh')).toEqual(['ABCD', '2345', 'EFGH']);
	});

	it('explains failed opens without losing the server message', () => {
		expect(openErrorMessage('peer_unavailable', 'x', 'B')).toBe('B stopped answering before the shell opened.');
		expect(openErrorMessage('device_link_required', 'x', 'B')).toBe('This device is no longer linked to B. Open it again to link.');
		expect(openErrorMessage('device_proof_invalid', 'x', 'B')).toBe("B refused this device's signature. Try again.");
		expect(openErrorMessage('other', '', 'B')).toBe("The shell on B didn't open.");
	});
});

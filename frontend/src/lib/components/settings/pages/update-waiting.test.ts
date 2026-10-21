import { describe, expect, it } from 'vitest';
import {
	blockerView,
	formatWhen,
	offerDescribesStaged,
	operationOutcome,
	outcomeView,
	receiptRows,
	receiptText,
	waitingSummary,
	waitingView,
	LEGACY_FAILED,
	type Blocker,
	type UpdateOperation,
	type WaitingFields
} from './update-waiting';

// Fixed clock and zone so the client-timezone copy is deterministic.
const NOW = Date.parse('2026-10-21T15:00:00Z');
const T = { now: NOW, locale: 'en-US', timeZone: 'America/New_York' };
const at = (iso: string) => Date.parse(iso) / 1000;

const service: Blocker = {
	session_id: 's1',
	title: 'Fix login',
	kind: 'service',
	task_id: 't1',
	label: 'npm run preview -- --port 5173',
	port: 5173,
	started_at: '2026-10-21T13:02:00Z',
	since: '2026-10-21T13:02:00Z',
	live: true,
	liveness: 'live',
	running_turn: false,
	turn_ended_at: '2026-10-21T13:10:00Z',
	stop_will_stop: true,
	monitor_serving_idle_seconds: 1800,
	idle_stop_at: at('2026-10-21T15:40:00Z')
};
const turn: Blocker = { session_id: 's2', title: 'Write docs', kind: 'turn', since: '2026-10-21T14:55:00Z', live: true, liveness: 'live', running_turn: true };
const shell: Blocker = { session_id: 's3', title: 'Run suite', kind: 'shell', task_id: 't3', label: 'pytest -q', live: true, running_turn: true };

describe('formatWhen', () => {
	it('formats ISO and epoch times in the given zone, with the date when not today', () => {
		expect(formatWhen('2026-10-21T13:02:00Z', T)).toBe('9:02 AM');
		expect(formatWhen(at('2026-10-21T15:40:00Z'), T)).toBe('11:40 AM');
		expect(formatWhen('2026-10-19T13:02:00Z', T)).toBe('Oct 19, 9:02 AM');
	});
	it('is null for unknown times', () => {
		expect(formatWhen(null, T)).toBeNull();
		expect(formatWhen(undefined, T)).toBeNull();
		expect(formatWhen('not a time', T)).toBeNull();
	});
});

describe('blocker copy', () => {
	it('names a preview service by label, port, start and the turn end, never a raw command field', () => {
		const view = blockerView(service, 0, 1800, T);
		expect(view.text).toBe(
			"Preview server npm run preview -- --port 5173 still running for Fix login on port 5173, started 9:02 AM; the chat's turn ended 9:10 AM."
		);
		expect(view.meta).toEqual(['Stops on its own around 11:40 AM, if the chat stays idle.', 'Stopping running work stops it.']);
	});
	it('says the chat has no running turn when its end time is unknown', () => {
		expect(blockerView({ ...service, turn_ended_at: null }, 0, 1800, T).text).toContain('; chat has no running turn.');
	});
	it('says nothing about the turn when that is unknown', () => {
		expect(blockerView({ ...service, running_turn: null }, 0, 1800, T).text).not.toMatch(/turn/);
	});
	it('does not claim a service is running without live evidence', () => {
		expect(blockerView({ ...service, live: false, liveness: 'unknown' }, 0, 1800, T).text).toContain(
			'still registered (process not confirmed)'
		);
	});
	it.each([
		['no estimate', { idle_stop_at: null }, 1800],
		['a disabled idle bound', { monitor_serving_idle_seconds: 0 }, 0],
		['a disabled worker bound', { monitor_serving_idle_seconds: null }, 0]
	])('has no known automatic stop time with %s', (_name, change, bound) => {
		expect(blockerView({ ...service, ...change }, 0, bound, T).meta[0]).toBe('No known automatic stop time.');
	});
	it('names a non-preview service plainly', () => {
		expect(blockerView({ ...service, label: 'uvicorn app:app --port 9000' }, 0, 1800, T).text).toMatch(/^Service uvicorn app:app/);
	});
	it('names turns and other tasks with the chat', () => {
		expect(blockerView(turn, 0, 0, T).text).toBe('Waiting for Write docs: running turn, started 10:55 AM.');
		expect(blockerView(shell, 0, 0, T).text).toBe('Waiting for Run suite: pytest -q.');
		expect(blockerView({ session_id: 's4', title: 'Chat', kind: 'agent' }, 0, 0, T).text).toBe('Waiting for Chat: background agent.');
	});
});

const staged: WaitingFields = { phase: 'ready', version: 'a'.repeat(40), staged_version: 'a'.repeat(40), monitor_serving_idle_seconds: 1800 };

describe('waitingView', () => {
	it('is null for legacy peers, unknown and idle state', () => {
		expect(waitingView({ phase: 'ready' })).toBeNull();
		expect(waitingView({ phase: 'ready', install_state: null })).toBeNull();
		expect(waitingView({ phase: 'idle', install_state: 'idle' })).toBeNull();
	});
	it('ready', () => {
		const view = waitingView({ ...staged, install_state: 'ready', blocked_by: [] }, T)!;
		expect(view.text).toBe('Downloaded and ready to restart.');
		expect(view.controls).toEqual({ stop: false, whenIdle: false, cancel: false });
	});
	it('blocked by a service offers both restart choices and no force', () => {
		const view = waitingView({ ...staged, install_state: 'blocked', wait_reason: 'running_work', blocked_by: [service] }, T)!;
		expect(view.text).toBe('Downloaded and ready.');
		expect(view.blockers?.[0].text).toMatch(/^Preview server npm run preview/);
		expect(view.controls).toEqual({ stop: true, whenIdle: true, cancel: false });
		expect(view.locked).toBe(false);
		expect(waitingSummary(view)).not.toMatch(/fail/i);
	});
	it('blocked with unknown blockers says so instead of claiming nothing runs', () => {
		const view = waitingView({ ...staged, install_state: 'blocked', wait_reason: 'running_work', blocked_by: null }, T)!;
		expect(view.blockers).toBeNull();
		expect(view.text).toBe('Downloaded and ready. Running work details are unavailable.');
	});
	it('still busy after stop names what still runs and the stop reason', () => {
		const view = waitingView(
			{
				...staged,
				install_state: 'blocked',
				wait_reason: 'still_busy_after_stop',
				blocked_by: [service],
				stop_result: { request_id: 'r', accepted: false, reason: 'Busy with running work: Preview server still running', completed_at: at('2026-10-21T14:58:00Z') }
			},
			T
		)!;
		expect(view.text).toBe(
			'The stop attempt finished, but npm run preview -- --port 5173 is still running. Restart was not queued: Busy with running work: Preview server still running.'
		);
		expect(view.notes).toEqual(['Last stop attempt 10:58 AM.']);
		expect(view.controls.stop && view.controls.whenIdle).toBe(true);
	});
	it('does not render an old stop failure once the worker is ready', () => {
		const view = waitingView({ ...staged, install_state: 'ready', blocked_by: [], stop_result: { accepted: false, reason: 'Old failure' } }, T)!;
		expect(waitingSummary(view)).not.toContain('Old failure');
	});
	it('stopping locks out duplicates', () => {
		const view = waitingView({ ...staged, install_state: 'stopping', wait_reason: 'stopping_work', blocked_by: [service] }, T)!;
		expect(view.text).toBe('Stop requested. Stopping running work, then requesting restart…');
		expect(view.locked).toBe(true);
		expect(view.controls).toEqual({ stop: false, whenIdle: false, cancel: false });
	});
	it('waiting for idle shows the service estimate, expiry and cancel', () => {
		const view = waitingView(
			{ ...staged, install_state: 'waiting_for_idle', wait_reason: 'running_work', blocked_by: [service], restart_expires_at: at('2026-10-22T14:00:00Z') },
			T
		)!;
		expect(view.text).toBe('Will restart when running work finishes.');
		expect(view.blockers?.[0].meta[0]).toBe('Stops on its own around 11:40 AM, if the chat stays idle.');
		expect(view.notes).toEqual(['Waits until Oct 22, 10:00 AM at most; a worker restart drops the schedule.']);
		expect(view.controls).toEqual({ stop: true, whenIdle: false, cancel: true });
	});
	it('legacy deferred flag still reads as a scheduled restart', () => {
		expect(waitingView({ phase: 'ready', deferred: true })?.state).toBe('waiting_for_idle');
	});
	it('queued is a delivery wait, not startup success', () => {
		const view = waitingView({ ...staged, install_state: 'queued', wait_reason: 'updater_acknowledgement' }, T)!;
		expect(view.text).toBe('Restart requested; waiting for the updater…');
		expect(view.locked).toBe(true);
	});
	it('newer offer names the staged revision, never the offer', () => {
		const view = waitingView(
			{ phase: 'available', version: 'b'.repeat(40), staged_version: 'a'.repeat(40), install_state: 'unavailable', wait_reason: 'newer_offer' },
			T
		)!;
		expect(view.text).toBe('Downloaded aaaaaaaaaaaa is retained. A newer update is available; download it to continue.');
		expect(view.text).not.toContain('bbbb');
	});
	it.each([
		['updater_idle', {}, 'Previous download retained; the updater is idle. Check for updates before restarting.'],
		['preparing_update', { message: 'Building frontend…', percent: 40 }, 'Previous download retained. Building frontend… 40%'],
		['updater_error', { error: 'Feed unreachable' }, 'Previous download retained. Feed unreachable'],
	])('retained download: %s', (reason, extra, text) => {
		const view = waitingView({ phase: 'idle', staged_version: 'a'.repeat(40), install_state: 'unavailable', wait_reason: reason, ...extra }, T)!;
		expect(view.text).toBe(text);
		expect(view.text).not.toMatch(/No staged update/);
	});
});

describe('offerDescribesStaged', () => {
	it('keeps an offer’s name off a different staged build', () => {
		expect(offerDescribesStaged({ version: 'b'.repeat(12), staged_version: 'a'.repeat(40) })).toBe(false);
		expect(offerDescribesStaged({ version: 'a'.repeat(12), staged_version: 'a'.repeat(40) })).toBe(true);
		expect(offerDescribesStaged({ version: 'a'.repeat(12) })).toBe(true);
	});
});

describe('receipts', () => {
	it('formats a not-sent row with the worker named once', () => {
		const text = receiptText([
			{ device_id: 'local', device_name: 'Current worker', accepted: false, request_state: 'not_sent', mode: 'stop', reason: 'Not every worker is staged yet; nothing was installed' }
		]);
		expect(text).toBe('Not sent to Current worker: Not every worker is staged yet; nothing was installed.');
		expect(text.match(/Current worker/g)).toHaveLength(1);
	});
	it('groups workers that were not sent for the same reason', () => {
		expect(
			receiptText([
				{ device_name: 'Peer', accepted: false, request_state: 'not_sent', reason: 'Waiting on workers to finish the previous update' },
				{ device_name: 'Current worker', accepted: false, request_state: 'not_sent', reason: 'Waiting on workers to finish the previous update' }
			])
		).toBe('Not sent to Peer, Current worker: Waiting on workers to finish the previous update.');
	});
	it('keeps delivered refusals as the worker’s own reason and flags unknown waiting details', () => {
		const rows = [
			{ device_name: 'Peer', accepted: false, reason: 'Offline; not queued' },
			{ device_name: 'Current worker', accepted: true, reason: 'Request queued', install_state: null },
			{ device_name: 'Other', accepted: true, reason: 'Request queued', install_state: 'queued' }
		];
		expect(receiptRows(rows).map((r) => r.device_name)).toEqual(['Peer', 'Current worker']);
		expect(receiptText(receiptRows(rows))).toBe('Peer: Offline; not queued; Current worker: Waiting details unavailable; Request queued');
	});
});

const op = (extra: Partial<UpdateOperation>): UpdateOperation => ({ id: 'op', phase: 'waiting', workers: [], ...extra });
const w = (device_name: string, status: string, message: string) => ({ device_id: device_name, device_name, status, message });

describe('operation outcome', () => {
	it('refused: names the worker and reason, never startup failure', () => {
		const view = outcomeView(
			op({
				phase: 'error',
				outcome: 'refused',
				refused_workers: [{ device_id: 'local', device_name: 'Current worker', reason: 'Busy with running work: Fix login' }],
				workers: [w('Current worker', 'skipped', 'Busy with running work: Fix login')]
			})
		);
		expect(view.headline).toBe('Restart was not started');
		expect(view.detail).toBe('Restart was not started: Current worker: Busy with running work: Fix login.');
		expect(view.detail).not.toMatch(/could not be confirmed/);
		expect(view.celebrate).toBe(false);
	});
	it('partial: names the workers left behind and does not celebrate', () => {
		const view = outcomeView(
			op({
				phase: 'complete',
				outcome: 'partial',
				refused_workers: [{ device_name: 'Current worker', reason: 'Busy with running work' }],
				workers: [w('Peer', 'verified', 'Updated and verified'), w('Current worker', 'skipped', 'Busy with running work')]
			})
		);
		expect(view.detail).toBe('Update finished on some workers. Not restarted: Current worker: Busy with running work.');
		expect(view.celebrate).toBe(false);
	});
	it('complete celebrates', () => {
		expect(outcomeView(op({ phase: 'complete', outcome: 'complete', workers: [w('Peer', 'verified', 'Updated and verified')] })).celebrate).toBe(true);
	});
	it('failed shows the worker’s own message', () => {
		const view = outcomeView(op({ phase: 'error', outcome: 'failed', workers: [w('Peer', 'error', 'Download failed: disk full')] }));
		expect(view.detail).toBe('Peer: Download failed: disk full.');
		expect(view.detail).not.toMatch(/could not be confirmed/);
	});
	it('failed only says startup was not confirmed when the server says so', () => {
		const view = outcomeView(op({ phase: 'error', outcome: 'failed', workers: [w('Peer', 'error', 'Startup not confirmed. Check this worker before retrying.')] }));
		expect(view.detail).toBe('Peer: Startup not confirmed. Check this worker before retrying.');
	});
	it('waiting keeps the refused workers named', () => {
		const view = outcomeView(
			op({ outcome: 'waiting', refused_workers: [{ device_name: 'Peer', reason: 'Offline; not queued' }], workers: [w('Current worker', 'waiting', 'Verifying updated version…'), w('Peer', 'skipped', 'Offline; not queued')] })
		);
		expect(view.headline).toBe('Waiting on all workers to finish startup');
		expect(view.detail).toBe('Not restarted: Peer: Offline; not queued.');
	});
	it('prefers outcome over legacy phase', () => {
		expect(operationOutcome(op({ phase: 'complete', outcome: 'partial' }))).toBe('partial');
		expect(operationOutcome(op({ phase: 'error', outcome: 'refused' }))).toBe('refused');
	});
	it('derives an older server’s outcome: skipped is a refusal, not a failed boot', () => {
		expect(operationOutcome(op({ phase: 'error', workers: [w('A', 'skipped', 'Busy')] }))).toBe('refused');
		expect(operationOutcome(op({ phase: 'complete', workers: [w('A', 'verified', ''), w('B', 'skipped', 'Busy')] }))).toBe('partial');
		expect(operationOutcome(op({ phase: 'complete', workers: [w('A', 'verified', '')] }))).toBe('complete');
		expect(operationOutcome(op({ phase: 'error', workers: [w('A', 'waiting', '')] }))).toBe('failed');
		expect(outcomeView(op({ phase: 'error', workers: [w('A', 'waiting', '')] })).detail).toBe(LEGACY_FAILED);
		expect(outcomeView(op({ phase: 'complete', workers: [w('A', 'verified', ''), w('B', 'skipped', 'Busy')] })).detail).toBe(
			'Update finished on some workers. Not restarted: B: Busy.'
		);
	});
});

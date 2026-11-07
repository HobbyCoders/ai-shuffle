/**
 * How each worker state of a project reads: list/hero chips, the Workers
 * tab's badge, and the new-chat "Runs on" row notes. One wording table for
 * every surface (cross-worker project sync).
 */
import { describe, expect, it } from 'vitest';
import type { ProjectWorker } from '$lib/api/project-workers';
import {
	errorAction,
	failureReason,
	isOnThisWorker,
	isPending,
	isRunning,
	missingCount,
	missingOnline,
	runsOnNote,
	seenAgo,
	workerFilterSlot,
	filterGeometry,
	workerChip
} from './project-workers-view';

const w = (state: string, extra: Partial<ProjectWorker> = {}): ProjectWorker => ({
	install_id: `id-${state}`, device_name: 'pop-os', is_self: false, online: true, approved: true,
	state, setup: null, ...extra
});

describe('workerChip', () => {
	it('words every state with its own tone, and only missing is actionable', () => {
		expect(workerChip(w('ready'), 'git')).toMatchObject({ tone: 'ready', text: 'pop-os', actionable: false });
		expect(workerChip(w('missing'), 'git')).toMatchObject({ tone: 'missing', actionable: true, title: 'Not on pop-os — click to set it up' });
		expect(workerChip(w('offline_has', { online: false }), 'git')).toMatchObject({ tone: 'offline', text: 'pop-os · offline · has it' });
		expect(workerChip(w('offline_missing', { online: false }), 'git')).toMatchObject({ tone: 'offline', text: 'pop-os · offline' });
		expect(workerChip(w('queued_offline', { online: false }), 'git')).toMatchObject({ tone: 'offline', text: 'pop-os · queued' });
		expect(workerChip(w('unknown'), 'git')).toMatchObject({ tone: 'unknown', actionable: false });
	});

	it('a running setup spins with its verb and progress', () => {
		const busy = w('setting_up', { setup: { op_id: 'op', status: 'running', progress: 42.4 } });
		expect(workerChip(busy, 'git')).toMatchObject({ tone: 'busy', text: 'pop-os · cloning 42%' });
		expect(workerChip({ ...busy, setup: { op_id: 'op', status: 'queued', progress: null } }, 'folder').text).toBe('pop-os · setting up');
	});

	it('a failed chip carries the reason as its title', () => {
		const failed = w('failed', { setup: { op_id: 'op', status: 'error', progress: null, error_code: 'github_auth', error: "GitHub isn't signed in on pop-os" } });
		expect(workerChip(failed, 'git')).toMatchObject({ tone: 'failed', text: 'pop-os · failed', title: "GitHub isn't signed in on pop-os" });
	});
});

describe('pending, missing and running', () => {
	it('queued_offline is pending but not running (it waits for the worker)', () => {
		const queued = w('queued_offline', { online: false, setup: { op_id: 'op', status: 'queued_offline', progress: null } });
		expect(isRunning(queued)).toBe(false);
		expect(isPending(queued)).toBe(true);
	});

	it('counts what still needs a setup, leaving out ones already under way', () => {
		const list = [w('ready'), w('missing'), w('failed'), w('offline_missing', { online: false }), w('setting_up'), w('queued_offline', { online: false })];
		expect(missingCount(list)).toBe(3);
		expect(missingOnline(list).map((x) => x.state)).toEqual(['missing', 'failed']);
	});

	it('a project is on this worker by its local id or its self row', () => {
		expect(isOnThisWorker({ local_project_id: 'p', workers: [] })).toBe(true);
		expect(isOnThisWorker({ local_project_id: null, workers: [w('missing', { is_self: true })] })).toBe(false);
		expect(isOnThisWorker({ local_project_id: null, workers: [w('ready', { is_self: true })] })).toBe(true);
	});
});

describe('runsOnNote', () => {
	it('says ready / not set up · set up / offline', () => {
		expect(runsOnNote(w('ready'))).toBe('ready');
		expect(runsOnNote(w('missing'))).toBe('not set up · set up');
		expect(runsOnNote(w('failed'))).toBe('not set up · set up');
		expect(runsOnNote(w('offline_has', { online: false }))).toBe('offline');
		expect(runsOnNote(w('setting_up'))).toBe('setting up');
		expect(runsOnNote(w('unknown'))).toBeNull();
		expect(runsOnNote(undefined)).toBeNull();
	});
});

describe('failures', () => {
	it('offers the fix per error code', () => {
		expect(errorAction('github_auth', 'pop-os')).toEqual({ kind: 'github', label: 'Sign in to GitHub on pop-os' });
		expect(errorAction('folder_exists', 'pop-os')?.kind).toBe('folder');
		expect(errorAction('network', 'pop-os')).toBeNull();
	});

	it('prefers the server message and falls back on the code', () => {
		expect(failureReason({ device_name: 'pop-os', setup: { op_id: 'o', status: 'error', progress: null, error: 'Custom.' } })).toBe('Custom.');
		expect(failureReason({ device_name: 'pop-os', setup: { op_id: 'o', status: 'error', progress: null, error_code: 'unsupported' } }))
			.toBe('Update pop-os to set projects up from here.');
	});

	it('seenAgo reads a last-seen time', () => {
		const now = Date.parse('2026-10-31T12:00:00Z');
		expect(seenAgo('2026-10-28T12:00:00Z', now)).toBe('seen 3d ago');
		expect(seenAgo(null, now)).toBeNull();
	});
});

describe('filterGeometry (#1210: the filter row never appears or vanishes under a painted list)', () => {
	const two = [{ approved: true }, { approved: true }];
	const none = { supported: null, overviewWorkerCount: 0, installs: null, registryFailed: false, deadlinePassed: false };

	it('waits while nothing is known', () => {
		expect(filterGeometry(none)).toBeNull();
	});

	it('follows the overview when it has answered', () => {
		expect(filterGeometry({ ...none, supported: true, overviewWorkerCount: 3 })).toBe('row');
		expect(filterGeometry({ ...none, supported: true, overviewWorkerCount: 1, installs: two })).toBe('none');
		// An older worker without the overview never shows the control.
		expect(filterGeometry({ ...none, supported: false, installs: two })).toBe('none');
	});

	it('follows the registry while the overview is out', () => {
		expect(filterGeometry({ ...none, installs: two })).toBe('row');
		// An older server leaves `approved` out: that means approved.
		expect(filterGeometry({ ...none, installs: [{}, {}] })).toBe('row');
		expect(filterGeometry({ ...none, installs: [{ approved: true }] })).toBe('none');
		// A peer waiting for approval is not in the overview either.
		expect(filterGeometry({ ...none, installs: [{ approved: true }, { approved: false }] })).toBe('none');
	});

	it('holds the row when the registry fails or the deadline passes with nothing known', () => {
		// A row is the only choice a late overview can never move.
		expect(filterGeometry({ ...none, registryFailed: true })).toBe('row');
		expect(filterGeometry({ ...none, deadlinePassed: true })).toBe('row');
		// An answer that is in still decides.
		expect(filterGeometry({ ...none, deadlinePassed: true, installs: [{ approved: true }] })).toBe('none');
	});
});

describe('workerFilterSlot', () => {
	it('waits until the visit has decided', () => {
		expect(workerFilterSlot(null, true, 3)).toBe('unknown');
	});

	it('never adds the row when the visit decided against it', () => {
		expect(workerFilterSlot('none', true, 3)).toBe('hidden');
		expect(workerFilterSlot('none', null, 0)).toBe('hidden');
	});

	it('fills the held row only when the overview reports more than one worker, else keeps it blank', () => {
		expect(workerFilterSlot('row', true, 2)).toBe('shown');
		expect(workerFilterSlot('row', null, 0)).toBe('reserved');
		expect(workerFilterSlot('row', true, 1)).toBe('reserved');
		expect(workerFilterSlot('row', false, 0)).toBe('reserved');
	});
});

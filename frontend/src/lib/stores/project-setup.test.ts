/**
 * Setting a project up on a worker: when a new chat needs it, the 2 s poll
 * that waits for it, and the "Set up & send" run that holds the message
 * until ready (or reports why not).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const m = vi.hoisted(() => ({
	getProjectWorkers: vi.fn(),
	startProjectSetup: vi.fn(),
	loadProjectCatalog: vi.fn().mockResolvedValue(undefined),
	loadProjectsOverview: vi.fn().mockResolvedValue(undefined),
	patchOverviewWorker: vi.fn()
}));

vi.mock('$lib/api/project-workers', async (orig) => ({
	...(await orig<typeof import('$lib/api/project-workers')>()),
	getProjectWorkers: m.getProjectWorkers,
	startProjectSetup: m.startProjectSetup
}));
vi.mock('./app-data.svelte', () => ({ appData: { loadProjectCatalog: m.loadProjectCatalog } }));
vi.mock('./projects-overview', async () => {
	const { writable } = await import('svelte/store');
	return {
		loadProjectsOverview: m.loadProjectsOverview,
		patchOverviewWorker: m.patchOverviewWorker,
		projectsOverview: writable([]),
		projectsOverviewSupported: writable(null)
	};
});
vi.mock('./active-session', async () => {
	const { writable } = await import('svelte/store');
	return { activeSession: writable({ sessionId: null, project: '', projectV2Id: null, deviceIntent: null }) };
});

import { computeNewChatSetupNeed, newChatSetup, waitForProjectSetup, ProjectSetupError } from './project-setup';
import type { ProjectWorker } from '$lib/api/project-workers';

const worker = (state: string, extra: Partial<ProjectWorker> = {}) => ({
	install_id: 'pop', device_name: 'pop-os', is_self: false, online: true, approved: true, state, setup: null,
	folder: null, suggested_folder: '/w/ai', branch: null, dirty: null, changed_files: null, last_seen: null, ...extra
});
const answer = (w: ReturnType<typeof worker>) => ({ project_v2_id: 'pv2', name: 'ai', kind: 'git', remote: null, default_folder: 'ai', workers: [w] });

beforeEach(() => {
	vi.useFakeTimers();
	m.getProjectWorkers.mockReset();
	m.startProjectSetup.mockReset();
});
afterEach(() => {
	vi.useRealTimers();
	newChatSetup.abandon();
});

describe('computeNewChatSetupNeed', () => {
	const overview = [{
		project_v2_id: 'pv2', local_project_id: 'ai', name: 'ai-shuffle', kind: 'git' as const,
		workers: [
			{ install_id: 'self', device_name: 'Unraid', is_self: true, online: true, approved: true, state: 'ready', setup: null },
			{ install_id: 'pop', device_name: 'pop-os', is_self: false, online: true, approved: true, state: 'missing', setup: null },
			{ install_id: 'lap', device_name: 'Laptop', is_self: false, online: false, approved: true, state: 'offline_missing', setup: null }
		]
	}];
	const session = (deviceIntent: { id: string; name: string } | null, extra = {}) =>
		({ sessionId: null, project: 'ai', projectV2Id: null, deviceIntent, ...extra });

	it('needs a setup when the picked worker lacks the project', () => {
		expect(computeNewChatSetupNeed(session({ id: 'pop', name: 'pop-os' }), overview, true, null))
			.toMatchObject({ installId: 'pop', workerName: 'pop-os', projectName: 'ai-shuffle', ref: { project_v2_id: 'pv2' } });
	});

	it('not for a worker that has it, an offline one, a saved chat, or an older server', () => {
		expect(computeNewChatSetupNeed(session(null), overview, true, null)).toBeNull();
		expect(computeNewChatSetupNeed(session({ id: 'lap', name: 'Laptop' }), overview, true, null)).toBeNull();
		expect(computeNewChatSetupNeed(session({ id: 'pop', name: 'pop-os' }, { sessionId: 's1' }), overview, true, null)).toBeNull();
		expect(computeNewChatSetupNeed(session({ id: 'pop', name: 'pop-os' }), overview, false, null)).toBeNull();
	});
});

describe('waitForProjectSetup', () => {
	it('polls every 2 s while running and resolves once ready', async () => {
		m.getProjectWorkers
			.mockResolvedValueOnce(answer(worker('setting_up', { setup: { op_id: 'op', status: 'running', progress: 10 } })))
			.mockResolvedValueOnce(answer(worker('ready')));
		const updates: number[] = [];
		const done = waitForProjectSetup({ project_v2_id: 'pv2' }, 'pop', { onUpdate: (w) => updates.push(w.setup?.progress ?? -1) });
		await vi.advanceTimersByTimeAsync(0);
		expect(m.getProjectWorkers).toHaveBeenCalledTimes(1);
		await vi.advanceTimersByTimeAsync(2000);
		await expect(done).resolves.toMatchObject({ state: 'ready' });
		expect(m.getProjectWorkers).toHaveBeenCalledTimes(2);
		expect(updates[0]).toBe(10);
	});

	it('rejects with the reason and code on a failure', async () => {
		m.getProjectWorkers.mockResolvedValueOnce(answer(worker('failed', {
			setup: { op_id: 'op', status: 'error', progress: null, error_code: 'github_auth', error: 'pop-os is not signed in to GitHub.' }
		})));
		const done = waitForProjectSetup({ project_v2_id: 'pv2' }, 'pop');
		const caught = done.catch((e) => e);
		await vi.advanceTimersByTimeAsync(0);
		const err = await caught;
		expect(err).toBeInstanceOf(ProjectSetupError);
		expect(err).toMatchObject({ message: 'pop-os is not signed in to GitHub.', code: 'github_auth' });
	});
});

describe('newChatSetup.run', () => {
	const need = { ref: { project_v2_id: 'pv2' }, projectV2Id: 'pv2', installId: 'pop', workerName: 'pop-os', projectName: 'ai', kind: 'git' as const, failed: false };

	it('holds until ready, then refreshes the catalog and lets the send go', async () => {
		m.startProjectSetup.mockResolvedValue({ op_id: 'op', install_id: 'pop', status: 'queued' });
		m.getProjectWorkers.mockResolvedValueOnce(answer(worker('ready')));
		const run = newChatSetup.run(need);
		expect(newChatSetup.current.phase).toBe('running');
		await vi.advanceTimersByTimeAsync(0);
		await expect(run).resolves.toBe(true);
		expect(m.loadProjectCatalog).toHaveBeenCalled();
		expect(newChatSetup.current.phase).toBe('idle');
	});

	it('on failure keeps the reason (and the caller keeps the draft)', async () => {
		m.startProjectSetup.mockResolvedValue({ op_id: 'op', install_id: 'pop', status: 'queued' });
		m.getProjectWorkers.mockResolvedValueOnce(answer(worker('failed', {
			setup: { op_id: 'op', status: 'error', progress: null, error_code: 'network', error: "pop-os couldn't reach GitHub." }
		})));
		const run = newChatSetup.run(need);
		await vi.advanceTimersByTimeAsync(0);
		await expect(run).resolves.toBe(false);
		expect(newChatSetup.current).toMatchObject({ phase: 'failed', reason: "pop-os couldn't reach GitHub.", code: 'network', key: 'pv2:pop' });
	});

	it('a start refused by the server fails with its detail', async () => {
		m.startProjectSetup.mockRejectedValue({ status: 403, detail: 'Admin only' });
		await expect(newChatSetup.run(need)).resolves.toBe(false);
		expect(newChatSetup.current).toMatchObject({ phase: 'failed', reason: 'Admin only' });
	});
});

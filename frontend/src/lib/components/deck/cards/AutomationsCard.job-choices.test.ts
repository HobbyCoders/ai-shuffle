/**
 * Pinned generation job choices on create (#543): the automation is created
 * paused, its staged choices are written after it exists, and a failed write
 * keeps the editor open on the saved automation with the pick still staged,
 * so Save retries the write instead of creating a duplicate or enabling it.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { Automation } from '$lib/api/automations';

const automationsApi = vi.hoisted(() => ({
	getAutomations: vi.fn(),
	getPendingApprovals: vi.fn(),
	getAutomationRuns: vi.fn(),
	getAutomationStats: vi.fn(),
	createAutomation: vi.fn(),
	updateAutomation: vi.fn(),
	enableAutomation: vi.fn()
}));
vi.mock('$lib/api/automations', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/automations')>()),
	...automationsApi
}));
const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/client')>()),
	api
}));
vi.mock('$lib/api/connectors', () => ({ getConnectors: vi.fn(async () => []), getConnectorTypes: vi.fn(async () => []) }));
vi.mock('$lib/api/claude-auth', () => ({ getClaudeAuth: vi.fn(async () => ({ authenticated: true })) }));
const watchers = vi.hoisted(() => new Map<string, () => Promise<void>>());
vi.mock('$lib/services/watch-resources', () => ({
	watchResources: vi.fn((kinds: string[], fn: () => Promise<void>) => {
		for (const kind of kinds) watchers.set(kind, fn);
	})
}));
vi.mock('$lib/components/settings/ModelPicker.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/stores/worker-presence', async () => {
	const { readable } = await import('svelte/store');
	return { workerInstalls: readable([]), refreshWorkerPresence: vi.fn(async () => []) };
});

import AutomationsCard from './AutomationsCard.svelte';

const proto = Element.prototype as unknown as { animate?: unknown };
const hadAnimate = 'animate' in proto;
beforeAll(() => {
	if (hadAnimate) return;
	proto.animate = () => {
		let done: (() => void) | null = null;
		return {
			cancel() {}, finish() {}, play() {}, pause() {}, currentTime: 0, effect: null,
			get onfinish() { return done; },
			set onfinish(fn: (() => void) | null) { done = fn; if (fn) queueMicrotask(() => { if (done === fn) fn(); }); }
		};
	};
});
afterAll(() => { if (!hadAnimate) delete proto.animate; });

const JOB = '11111111-2222-4333-8444-555555555555';
const PRICE = { currency: 'USD', unit: 'item', amount: '0.025', total: '0.025', status: 'estimated' };
const created: Automation = {
	id: JOB, name: 'Poster run', description: null,
	trigger_type: 'cron', trigger_config: { expression: '0 9 * * *' }, triggers: null,
	prompt: 'Make a poster', profile_id: 'general-purpose', project_id: null,
	output_config: {}, action_steps: null, max_turns: 1, timeout_seconds: 300,
	max_cost_per_run: null, max_cost_per_day: null, max_cost_total: null, max_runs_per_day: null,
	total_cost_used: 0, is_enabled: false, status: 'idle', last_run_at: null,
	last_error: null, run_count: 0, failure_count: 0, requires_approval: false,
	model: null, provider_id: null, created_at: '', updated_at: '', budget_ok: true, message: ''
};
const FAL_OPTION = {
	source: 'fal', account: 'fal-ai', worker: 'worker-b', model: 'fal-ai/flux/dev', kind: 'image',
	options: {}, verified: false, price: PRICE, schema: null, available: true, reason: null
};

let listed: Automation[] = [];

beforeEach(() => {
	vi.clearAllMocks();
	listed = [];
	automationsApi.getAutomations.mockImplementation(async () => listed);
	automationsApi.getPendingApprovals.mockResolvedValue([]);
	automationsApi.createAutomation.mockImplementation(async () => {
		listed = [created];
		return created;
	});
	automationsApi.updateAutomation.mockResolvedValue(created);
	api.get.mockImplementation(async (path: string) => {
		if (path === '/agents') return [{ id: 'general-purpose', name: 'General Purpose' }];
		if (path === '/generation/choices') return { remembered: {}, last: {}, migrated: true, worker: 'worker-a' };
		if (path === `/generation/jobs/${JOB}/choices`) return { job_id: JOB, choices: {} };
		if (path.startsWith('/generation/options/image')) return { options: [FAL_OPTION] };
		return [];
	});
	api.post.mockResolvedValue({ ok: true, price: PRICE, priced: true, options: {} });
});
afterEach(() => cleanup());

async function stageImagePick() {
	const row = await waitFor(() => {
		const el = document.querySelector<HTMLElement>('.ajc-row[data-kind="image"]');
		expect(el).toBeTruthy();
		return el!;
	});
	await fireEvent.click(within(row).getByRole('button', { name: /Pin a choice/ }));
	await fireEvent.click(await screen.findByRole('button', { name: /Pin to this job/ }));
	await waitFor(() => expect(within(row).getByText('Unsaved')).toBeTruthy());
}

describe('creating an automation with a pinned generation choice', () => {
	it('creates it paused, keeps the pick staged when its write fails, and retries on Save', async () => {
		api.put.mockRejectedValueOnce({ detail: 'Worker offline.', status: 503 });
		render(AutomationsCard, { props: { mobile: false } });
		await fireEvent.click((await screen.findAllByRole('button', { name: /New automation/ }))[0]);
		await fireEvent.input(screen.getByPlaceholderText('e.g. Daily report generator'), { target: { value: 'Poster run' } });
		await fireEvent.input(screen.getByPlaceholderText(/Enter the prompt template/), { target: { value: 'Make a poster' } });
		await stageImagePick();
		expect(api.put).not.toHaveBeenCalled();

		await fireEvent.click(screen.getByRole('button', { name: 'Create' }));

		await waitFor(() => expect(screen.getByText(/some media generation choices were not saved/)).toBeTruthy());
		expect(screen.getByText(/created \(saved, paused\)/)).toBeTruthy();
		expect(screen.getByText(/image: Worker offline\./)).toBeTruthy();
		expect(automationsApi.createAutomation).toHaveBeenCalledTimes(1);
		expect(automationsApi.createAutomation.mock.calls[0][0]).not.toHaveProperty('enabled');
		expect(automationsApi.enableAutomation).not.toHaveBeenCalled();
		// Still editing, now the saved automation, with the pick still staged.
		const row = document.querySelector<HTMLElement>('.ajc-row[data-kind="image"]')!;
		expect(within(row).getByText('Unsaved')).toBeTruthy();

		api.put.mockResolvedValueOnce({ job_id: JOB, kind: 'image', choice: FAL_OPTION });
		await fireEvent.click(screen.getByRole('button', { name: 'Save' }));

		await waitFor(() => expect(automationsApi.updateAutomation).toHaveBeenCalledTimes(1));
		expect(automationsApi.updateAutomation.mock.calls[0][0]).toBe(JOB);
		expect(automationsApi.createAutomation).toHaveBeenCalledTimes(1);
		expect(api.put).toHaveBeenLastCalledWith(`/generation/jobs/${JOB}/choices/image`, {
			source: 'fal', account: 'fal-ai', worker: 'worker-b', model: 'fal-ai/flux/dev', options: {}
		});
		expect(automationsApi.enableAutomation).not.toHaveBeenCalled();
		await waitFor(() => expect(document.querySelector('.ajc-row')).toBeNull());
		// The save's own change notification must not reopen the editor.
		await watchers.get('automations')!();
		await waitFor(() => expect(screen.getAllByText('Poster run').length).toBeGreaterThan(0));
		expect(document.querySelector('.ajc-row')).toBeNull();
	});

	it('writes nothing to the job API when no choice was staged', async () => {
		render(AutomationsCard, { props: { mobile: false } });
		await fireEvent.click((await screen.findAllByRole('button', { name: /New automation/ }))[0]);
		await fireEvent.input(screen.getByPlaceholderText('e.g. Daily report generator'), { target: { value: 'Poster run' } });
		await fireEvent.input(screen.getByPlaceholderText(/Enter the prompt template/), { target: { value: 'Make a poster' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Create' }));

		await waitFor(() => expect(automationsApi.createAutomation).toHaveBeenCalledTimes(1));
		await waitFor(() => expect(document.querySelector('.ajc-row')).toBeNull());
		expect(api.put).not.toHaveBeenCalled();
		expect(api.delete).not.toHaveBeenCalled();
	});
});

/**
 * "Pick an agent" on an automation whose agent was removed (#1884): it opens
 * the editor AND puts keyboard focus on the agent picker, expanding the Agent
 * section first if the user had collapsed it.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { Automation } from '$lib/api/automations';

const automationsApi = vi.hoisted(() => ({
	getAutomations: vi.fn(),
	getPendingApprovals: vi.fn(),
	getAutomationRuns: vi.fn(),
	getAutomationStats: vi.fn()
}));
vi.mock('$lib/api/automations', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/automations')>()),
	...automationsApi
}));
const apiGet = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/client')>()),
	api: { get: apiGet }
}));
vi.mock('$lib/api/connectors', () => ({ getConnectors: vi.fn(async () => []), getConnectorTypes: vi.fn(async () => []) }));
vi.mock('$lib/api/claude-auth', () => ({ getClaudeAuth: vi.fn(async () => ({ authenticated: true })) }));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/components/settings/ModelPicker.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));

import AutomationsCard from './AutomationsCard.svelte';

// jsdom has no Web Animations API (the card uses svelte transitions).
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

const orphan: Automation = {
	id: 'a1', name: 'Nightly digest', description: null,
	trigger_type: 'interval', trigger_config: { seconds: 300 }, triggers: null,
	prompt: 'Summarise', profile_id: 'removed-agent', project_id: null,
	output_config: {}, action_steps: null, max_turns: null, timeout_seconds: null,
	max_cost_per_run: null, max_cost_per_day: null, max_cost_total: null, max_runs_per_day: null,
	total_cost_used: 0, is_enabled: false, status: 'idle', last_run_at: null,
	last_error: null, run_count: 0, failure_count: 0, requires_approval: false,
	model: null, provider_id: null, created_at: '', updated_at: ''
};

beforeEach(() => {
	vi.clearAllMocks();
	automationsApi.getAutomations.mockResolvedValue([orphan]);
	automationsApi.getPendingApprovals.mockResolvedValue([]);
	automationsApi.getAutomationRuns.mockResolvedValue([]);
	automationsApi.getAutomationStats.mockResolvedValue({ total_runs: 0, total_cost_usd: 0, successful_runs: 0, failed_runs: 0 });
	apiGet.mockImplementation(async (path: string) =>
		path === '/agents' ? [{ id: 'general-purpose', name: 'General Purpose' }] : []
	);
});
afterEach(cleanup);

async function openOrphanDetail() {
	await fireEvent.click(await screen.findByText('Nightly digest'));
	return screen.findByRole('button', { name: 'Pick an agent' });
}

describe('Pick an agent', () => {
	it('opens the editor with focus on the agent picker', async () => {
		render(AutomationsCard, { props: { mobile: true } });
		await fireEvent.click(await openOrphanDetail());
		const picker = await waitFor(() => {
			const el = document.querySelector<HTMLSelectElement>('select.sel[data-agent-picker]');
			expect(el).toBeTruthy();
			return el!;
		});
		await waitFor(() => expect(document.activeElement).toBe(picker));
		// The removed id is not an option, so the picker shows its placeholder.
		expect(picker.value).toBe('');
	});

	it('focuses the agent picker on the desktop layout too', async () => {
		render(AutomationsCard, { props: { mobile: false } });
		await fireEvent.click(await openOrphanDetail());
		await waitFor(() => expect(document.activeElement?.matches('select.sel[data-agent-picker]')).toBe(true));
	});

	it('does not move focus when the editor is opened with the pencil', async () => {
		render(AutomationsCard, { props: { mobile: true } });
		await openOrphanDetail();
		await fireEvent.click(screen.getByTitle('Edit'));
		await waitFor(() => expect(document.querySelector('select.sel[data-agent-picker]')).toBeTruthy());
		expect(document.activeElement?.matches('select.sel[data-agent-picker]')).toBe(false);
	});

	it('expands a collapsed Configuration section before focusing', async () => {
		render(AutomationsCard, { props: { mobile: true } });
		// Collapse Configuration (holds the Agent field) in the editor, then go back.
		await openOrphanDetail();
		await fireEvent.click(screen.getByTitle('Edit'));
		await fireEvent.click(await screen.findByRole('button', { name: /Configuration/ }));
		await waitFor(() => expect(document.querySelector('select.sel[data-agent-picker]')).toBeNull());
		await fireEvent.click(screen.getByRole('button', { name: /^Automations/ }));
		await fireEvent.click(await openOrphanDetail());
		await waitFor(() => expect(document.activeElement?.matches('select.sel[data-agent-picker]')).toBe(true));
	});
});

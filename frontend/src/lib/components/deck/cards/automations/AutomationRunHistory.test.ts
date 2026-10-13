import { afterEach, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import type { AutomationRun } from '$lib/api/automations';
import AutomationRunHistory from './AutomationRunHistory.svelte';

afterEach(cleanup);

const run = (id: string, trigger_data: AutomationRun['trigger_data']): AutomationRun => ({
	id, automation_id: 'auto-1', status: 'completed', trigger_data, session_id: null, prompt_rendered: null,
	response: null, error: null, cost_usd: 0, tokens_in: 0, tokens_out: 0, duration_ms: 0, output_status: 'none',
	output_error: null, started_at: null, completed_at: null, created_at: '2026-09-01T10:00:00Z'
});

function renderRuns(runs: AutomationRun[]) {
	return render(AutomationRunHistory, {
		runs,
		runsLoading: false,
		expandedRunIds: new Set<string>(),
		onToggleRunExpanded: () => {},
		formatTime: () => 'time',
		formatCost: () => '$0',
		formatDuration: () => '0s',
		getRunStatusBadgeClass: () => ''
	});
}

it('labels each run by the trigger key the backend writes', () => {
	renderRuns([
		run('r1', { trigger: 'webhook', payload: {} }),
		run('r2', { trigger: 'schedule', fired_at: 'x' }),
		run('r3', { trigger: 'connector_event', event_type: 'e' }),
		run('r4', { trigger: 'connector_trigger' }),
		run('r5', { trigger: 'manual' })
	]);
	for (const label of ['Webhook', 'Schedule', 'Event', 'Connector', 'Manual']) {
		expect(screen.getByText(label)).toBeTruthy();
	}
});

it('falls back to a legacy source key, then to Manual', () => {
	renderRuns([run('r1', { source: 'webhook' }), run('r2', null)]);
	expect(screen.getByText('Webhook')).toBeTruthy();
	expect(screen.getByText('Manual')).toBeTruthy();
});

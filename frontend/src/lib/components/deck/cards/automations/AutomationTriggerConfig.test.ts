import { afterEach, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import type { TriggerItem } from '$lib/api/automations';
import AutomationTriggerConfig from './AutomationTriggerConfig.svelte';

afterEach(() => {
	cleanup();
	delete window.__SHUFFLE_MOBILE__;
});

function renderWebhook(currentView: 'create' | 'edit', selectedAutomationId: string | null) {
	const triggers: TriggerItem[] = [{ type: 'webhook', config: { secret: 'shh-secret' } }];
	return render(AutomationTriggerConfig, {
		triggers,
		expandedTriggers: new Set([0]),
		connectors: [],
		connectorTypes: [],
		connectorsWithTriggers: [],
		currentView,
		selectedAutomationId,
		timezone: 'UTC',
		onUpdateTriggers: () => {},
		onToggleTriggerExpand: () => {}
	});
}

it('summarises the schedule in its header without duplicating preset copy or timezone', () => {
 const view = render(AutomationTriggerConfig, {
  triggers: [{ type: 'cron', config: { expression: '0 9 * * *' } }], expandedTriggers: new Set([0]),
  connectors: [], connectorTypes: [], connectorsWithTriggers: [], currentView: 'create',
  selectedAutomationId: null, timezone: 'UTC', onUpdateTriggers: () => {}, onToggleTriggerExpand: () => {}
 });
 expect(screen.getByRole('heading', { name: 'Every day at 9 AM' })).toBeTruthy();
 expect(view.container.textContent?.match(/UTC/g)).toHaveLength(1);
 expect(view.container.querySelector('.cron-summary-text')).toBeNull();
 expect(view.container.querySelector('.trigger-card > .st-row + .st-row')).toBeTruthy();
 expect(screen.getByRole('button', { name: 'Collapse trigger' }).classList.contains('ghost')).toBe(true);
});

it('keeps the trigger heading and collapse control inside its card', () => {
 const view = renderWebhook('edit', 'auto-1a2b');
 const collapse = screen.getByRole('button', { name: 'Collapse trigger' });
 expect(collapse.closest('.st-card')).not.toBeNull();
 expect(collapse.textContent).toBe('');
 expect(collapse.querySelector('svg')).not.toBeNull();
 expect(screen.getByRole('group', { name: 'Trigger type' }).closest('.st-row')).not.toBeNull();
 expect(view.container.querySelectorAll('.st-grp')).toHaveLength(0);
 expect(screen.queryByText(/Any trigger firing/)).toBeNull();
});

it('shows the webhook trigger URL built from the automation id and the page origin', () => {
	renderWebhook('edit', 'auto-1a2b');
	const url = screen.getByLabelText('Webhook URL') as HTMLInputElement;
	expect(url.value).toBe(`${window.location.origin}/api/v1/automations/auto-1a2b/trigger`);
	expect((screen.getByLabelText('Secret') as HTMLInputElement).value).toBe('shh-secret');
});

it('uses the paired worker origin in the bundled mobile UI', () => {
	window.__SHUFFLE_MOBILE__ = { apiOrigin: 'https://worker.example', token: 'shc_x' };
	renderWebhook('edit', 'auto-1a2b');
	expect((screen.getByLabelText('Webhook URL') as HTMLInputElement).value).toBe(
		'https://worker.example/api/v1/automations/auto-1a2b/trigger'
	);
});

it('explains the URL comes after creation on the create form', () => {
	renderWebhook('create', null);
	expect(screen.queryByLabelText('Webhook URL')).toBeNull();
	expect(screen.getByText(/generated after creation/i)).toBeTruthy();
});

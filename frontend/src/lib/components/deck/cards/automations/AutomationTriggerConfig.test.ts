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

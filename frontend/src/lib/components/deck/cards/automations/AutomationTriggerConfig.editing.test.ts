import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import type { TriggerItem } from '$lib/api/automations';
import type { Connector, ConnectorTypeInfo } from '$lib/api/connectors';
import AutomationTriggerConfig from './AutomationTriggerConfig.svelte';

afterEach(cleanup);

const connectors = [
	{ id: 'c-live', name: 'Slack', type: 'slack', is_active: true, status: 'connected' },
	{ id: 'c-off', name: 'Old Mail', type: 'email', is_active: false, status: 'error' }
] as unknown as Connector[];
const connectorTypes = [
	{ type: 'slack', triggers: [{ id: 'new_message', name: 'New message' }] }
] as unknown as ConnectorTypeInfo[];

function mount(triggers: TriggerItem[], extra: Record<string, unknown> = {}) {
	const onUpdateTriggers = vi.fn();
	const view = render(AutomationTriggerConfig, {
		triggers,
		expandedTriggers: new Set([0]),
		connectors,
		connectorTypes,
		connectorsWithTriggers: connectors.slice(0, 1),
		currentView: 'create',
		selectedAutomationId: null,
		timezone: 'Europe/Berlin',
		onUpdateTriggers,
		onToggleTriggerExpand: () => {},
		...extra
	});
	// The component is controlled: feed each emitted list back in, like AutomationForm does.
	onUpdateTriggers.mockImplementation((next: TriggerItem[]) => view.rerender({ triggers: next }));
	return { ...view, onUpdateTriggers };
}
const lastTriggers = (fn: ReturnType<typeof vi.fn>) => fn.mock.calls.at(-1)![0] as TriggerItem[];

describe('cron triggers', () => {
	it('turns the weekday preset into the standard 1-5 expression and describes it with the timezone', async () => {
		const { onUpdateTriggers } = mount([{ type: 'cron', config: { expression: '0 9 * * *' } }]);
		expect(screen.getByText('Timezone: Europe/Berlin')).toBeTruthy();
		const preset = screen.getByDisplayValue('Every day at 9 AM') as HTMLSelectElement;
		await fireEvent.change(preset, { target: { value: '0 9 * * 1-5' } });
		expect(lastTriggers(onUpdateTriggers)).toEqual([{ type: 'cron', config: { expression: '0 9 * * 1-5' } }]);
		expect(await screen.findByText('Every weekday at 9 AM', { selector: '.cron-summary-text' })).toBeTruthy();
		expect(screen.getByText('0 9 * * 1-5', { selector: 'code' })).toBeTruthy();
	});

	it('builds the custom expression from the field just changed, not the previous value', async () => {
		const { onUpdateTriggers } = mount([{ type: 'cron', config: { expression: '30 14 * * *' } }]);
		// A non-preset expression opens the custom builder with its fields filled in.
		expect((screen.getByLabelText('Minute') as HTMLSelectElement).value).toBe('30');
		expect((screen.getByLabelText('Hour') as HTMLSelectElement).value).toBe('14');
		expect(screen.getByText('At 2:30 PM', { selector: '.cron-summary-text' })).toBeTruthy();
		await fireEvent.change(screen.getByLabelText('Day of Week'), { target: { value: '1-5' } });
		expect(lastTriggers(onUpdateTriggers)[0].config.expression).toBe('30 14 * * 1-5');
		expect(await screen.findByText('At 2:30 PM, weekdays', { selector: '.cron-summary-text' })).toBeTruthy();
		await fireEvent.change(screen.getByLabelText('Month'), { target: { value: '3' } });
		expect(lastTriggers(onUpdateTriggers)[0].config.expression).toBe('30 14 * 3 1-5');
		expect(await screen.findByText('At 2:30 PM, weekdays of Mar', { selector: '.cron-summary-text' })).toBeTruthy();
	});

	it('flags a malformed raw expression and switches back to presets for a known one', async () => {
		const { onUpdateTriggers } = mount([{ type: 'cron', config: { expression: '0 9 * * *' } }]);
		const raw = screen.getByPlaceholderText('0 9 * * *') as HTMLInputElement;
		await fireEvent.input(raw, { target: { value: '0 9 * *' } });
		expect(lastTriggers(onUpdateTriggers)[0].config.expression).toBe('0 9 * *');
		expect(await screen.findByText('Invalid expression')).toBeTruthy();
		expect(screen.getByLabelText('Minute')).toBeTruthy(); // custom builder shown
		await fireEvent.input(raw, { target: { value: '*/15 * * * *' } });
		expect(await screen.findByDisplayValue('Every 15 minutes')).toBeTruthy();
		expect(screen.queryByLabelText('Minute')).toBeNull();
	});
});

describe('trigger list editing', () => {
	it('adds a default daily cron trigger, removes one only while two remain, and summarises collapsed cards', async () => {
		const { onUpdateTriggers } = mount([{ type: 'interval', config: { seconds: 900 } }], { expandedTriggers: new Set() });
		expect(screen.getByText('Every 900s')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Remove trigger' })).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: /Add trigger/ }));
		expect(lastTriggers(onUpdateTriggers)).toEqual([
			{ type: 'interval', config: { seconds: 900 } },
			{ type: 'cron', config: { expression: '0 9 * * *' } }
		]);
		expect(await screen.findByText('Every day at 9 AM')).toBeTruthy();
		const remove = screen.getAllByRole('button', { name: 'Remove trigger' });
		expect(remove).toHaveLength(2);
		await fireEvent.click(remove[0]);
		expect(lastTriggers(onUpdateTriggers)).toEqual([{ type: 'cron', config: { expression: '0 9 * * *' } }]);
	});

	it('edits a connector event: only live connectors are offered and the chosen event is kept', async () => {
		const { onUpdateTriggers } = mount([{ type: 'connector_event', config: {} }]);
		const connector = screen.getByLabelText('Connector') as HTMLSelectElement;
		expect([...connector.options].map(o => o.textContent)).toEqual(['Select a connector...', 'Slack (slack)']);
		await fireEvent.change(connector, { target: { value: 'c-live' } });
		expect(lastTriggers(onUpdateTriggers)[0].config).toEqual({ connector_id: 'c-live' });
		await fireEvent.change(screen.getByLabelText('Event Type'), { target: { value: 'session.error' } });
		expect(lastTriggers(onUpdateTriggers)[0]).toEqual({ type: 'connector_event', config: { connector_id: 'c-live', event_type: 'session.error' } });
	});

	it('resets the chosen connector trigger when the connector changes, and switching type resets config', async () => {
		const { onUpdateTriggers } = mount([{ type: 'connector_trigger', config: { connector_id: 'c-live', trigger_id: 'new_message' } }]);
		expect((screen.getByLabelText('Trigger') as HTMLSelectElement).value).toBe('new_message');
		await fireEvent.change(screen.getByLabelText('Connector'), { target: { value: '' } });
		expect(lastTriggers(onUpdateTriggers)[0].config).toEqual({ connector_id: '', trigger_id: '' });
		expect(screen.queryByLabelText('Trigger')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: /Interval/ }));
		expect(lastTriggers(onUpdateTriggers)[0]).toEqual({ type: 'interval', config: { seconds: 3600 } });
		expect(screen.getByText('Minimum 60 seconds')).toBeTruthy();
	});
});

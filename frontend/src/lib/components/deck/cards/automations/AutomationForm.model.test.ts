import { cleanup, fireEvent, render, within } from '@testing-library/svelte';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get }
}));

vi.mock('$lib/api/claude-auth', () => ({
	getClaudeAuth: vi.fn(() => Promise.resolve({ mode: 'api_key' }))
}));

import { encodeProviderModel, hydrateModelRegistry } from '$lib/constants/models';
import { automationModelLabel, type Automation } from '$lib/api/automations';
import AutomationForm from './AutomationForm.svelte';

beforeAll(async () => {
	mocks.get.mockResolvedValueOnce({
		models: [
			{ value: 'haiku', label: 'Haiku', family: 'haiku', supports_1m: false },
			{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true }
		],
		default: 'sonnet',
		legacy_map: {},
		claude: { auth_kind: 'subscription', plan_type: null },
		providers: [
			{
				id: 'openai-row',
				name: 'OpenAI',
				provider_type: 'openai',
				models: [{ id: 'gpt-5.6', label: 'GPT-5.6' }],
				default_model: 'gpt-5.6'
			}
		]
	});
	await hydrateModelRegistry();
});

afterEach(() => {
	cleanup();
	localStorage.clear();
});

function renderForm(overrides: Record<string, unknown> = {}) {
	const onUpdateFormModel = vi.fn();
	const props = {
		currentView: 'create' as const,
		selectedAutomationId: null,
		formError: '',
		formName: 'Digest',
		formDescription: '',
		formTriggers: [{ type: 'cron' as const, config: { expression: '0 9 * * *' } }],
		formPrompt: 'Summarize',
		formProfileId: 'agent-1',
		formProjectId: '',
		formOutputType: 'session' as const,
		formOutputConfig: {},
		formMaxCostPerRun: '',
		formMaxCostPerDay: '',
		formMaxCostTotal: '',
		formMaxRunsPerDay: '',
		formMaxTurns: '1',
		formTimeoutSeconds: '300',
		formRequiresApproval: false,
		formModel: '',
		formActionSteps: [],
		profiles: [{ id: 'agent-1', name: 'Agent One', config: {} }],
		projects: [],
		connectors: [],
		connectorTypes: [],
		connectorsWithTriggers: [],
		connectorsWithActions: [],
		expandedTriggers: new Set<number>(),
		expandedActions: new Set<number>([0]),
		collapsedSections: new Set<string>(['budget']),
		timezone: 'UTC',
		onUpdateFormError: vi.fn(),
		onUpdateFormName: vi.fn(),
		onUpdateFormDescription: vi.fn(),
		onUpdateFormTriggers: vi.fn(),
		onUpdateFormPrompt: vi.fn(),
		onUpdateFormProfileId: vi.fn(),
		onUpdateFormProjectId: vi.fn(),
		onUpdateFormOutputType: vi.fn(),
		onUpdateFormOutputConfig: vi.fn(),
		onUpdateFormMaxCostPerRun: vi.fn(),
		onUpdateFormMaxCostPerDay: vi.fn(),
		onUpdateFormMaxCostTotal: vi.fn(),
		onUpdateFormMaxRunsPerDay: vi.fn(),
		onUpdateFormMaxTurns: vi.fn(),
		onUpdateFormTimeoutSeconds: vi.fn(),
		onUpdateFormRequiresApproval: vi.fn(),
		onUpdateFormModel,
		onUpdateFormActionSteps: vi.fn(),
		onToggleSection: vi.fn(),
		onToggleTriggerExpand: vi.fn(),
		onToggleActionExpand: vi.fn(),
		...overrides
	};
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	const utils = render(AutomationForm, { props: props as any });
	return { ...utils, onUpdateFormModel, props };
}

function saved(fields: Partial<Automation>): Automation {
	return {
		id: 'auto-1',
		model: null,
		provider_id: null,
		effective_model: 'sonnet',
		effective_provider_id: null,
		model_source: 'default',
		model_error: null,
		...fields
	} as Automation;
}

describe('AutomationForm model picker', () => {
	it('starts on the agent default and emits the picked model', async () => {
		const { getByTestId, onUpdateFormModel } = renderForm();
		const row = within(getByTestId('automation-model-row'));
		expect(row.getByText("Runs use the agent's saved model, or the app default.")).toBeTruthy();

		await fireEvent.click(row.getByRole('button', { name: 'Agent default' }));
		await fireEvent.click(row.getByRole('tab', { name: /^Claude/ }));
		await fireEvent.click(row.getByRole('option', { name: /Haiku/ }));
		expect(onUpdateFormModel).toHaveBeenCalledWith('haiku');
	});

	it('emits a provider model as its picker value', async () => {
		const { getByTestId, onUpdateFormModel } = renderForm();
		const row = within(getByTestId('automation-model-row'));
		await fireEvent.click(row.getByRole('button', { name: 'Agent default' }));
		await fireEvent.click(row.getByRole('tab', { name: /OpenAI/ }));
		await fireEvent.click(row.getByRole('option', { name: /GPT-5\.6/ }));
		expect(onUpdateFormModel).toHaveBeenCalledWith(encodeProviderModel('openai-row', 'gpt-5.6'));
	});

	it('shows a stored choice and offers no free-text model', async () => {
		const { getByTestId, queryByText } = renderForm({
			currentView: 'edit',
			formModel: encodeProviderModel('openai-row', 'gpt-5.6'),
			savedAutomation: saved({
				model: encodeProviderModel('openai-row', 'gpt-5.6'),
				model_source: 'automation'
			})
		});
		const row = within(getByTestId('automation-model-row'));
		expect(row.getByText('Every run uses this model.')).toBeTruthy();
		await fireEvent.click(row.getByRole('button', { name: /GPT-5\.6/ }));
		expect(queryByText('Custom…')).toBeNull();
	});

	it('names the fallback an existing automation runs on', () => {
		const { getByTestId } = renderForm({ currentView: 'edit', savedAutomation: saved({}) });
		expect(
			within(getByTestId('automation-model-row')).getByText('Runs use Sonnet (default).')
		).toBeTruthy();
	});

	it('says why runs fail when the stored model is gone', () => {
		const error =
			"This automation's model is no longer available: The provider 'gone' does not exist. Pick another model in the automation's settings.";
		const { getByTestId } = renderForm({
			currentView: 'edit',
			formModel: 'prov:gone:gpt-x',
			savedAutomation: saved({ model: 'prov:gone:gpt-x', model_source: 'automation', model_error: error })
		});
		const hint = within(getByTestId('automation-model-row')).getByText(error);
		expect(hint.classList.contains('model-error')).toBe(true);
	});

	it('offers a per-step model that defaults to the automation', async () => {
		const onUpdateFormActionSteps = vi.fn();
		const { getByRole } = renderForm({
			formActionSteps: [{ type: 'send_prompt', prompt: 'next' }],
			onUpdateFormActionSteps
		});
		// The step's label names the picker; it starts on the automation's model.
		const trigger = getByRole('button', { name: 'Model (optional)' });
		expect(trigger.textContent).toContain('Same as automation');
		await fireEvent.click(trigger);
		await fireEvent.click(getByRole('tab', { name: /^Claude/ }));
		await fireEvent.click(getByRole('option', { name: /Haiku/ }));
		expect(onUpdateFormActionSteps).toHaveBeenCalledWith([
			{ type: 'send_prompt', prompt: 'next', model: 'haiku', provider_id: undefined }
		]);
	});
});

describe('automationModelLabel', () => {
	it('labels the choice, the fallback and a gone model', () => {
		expect(automationModelLabel(saved({ model: 'haiku', model_source: 'automation' }))).toBe('Haiku');
		expect(automationModelLabel(saved({}))).toBe('Sonnet (default)');
		expect(
			automationModelLabel(
				saved({ effective_model: 'gpt-5.6', effective_provider_id: 'openai-row', model_source: 'agent' })
			)
		).toBe('GPT-5.6 (agent)');
		expect(
			automationModelLabel(saved({ effective_model: null, effective_provider_id: 'openai-row', model_source: 'agent' }))
		).toBe('Provider default (agent)');
		expect(automationModelLabel(saved({ model: 'prov:x:y', model_error: 'gone' }))).toBe('Model unavailable');
	});
});

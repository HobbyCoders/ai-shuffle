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

import { getClaudeAuth } from '$lib/api/claude-auth';
import { encodeProviderModel, hydrateModelRegistry } from '$lib/constants/models';
import { automationFallbackModel, automationModelLabel, type Automation } from '$lib/api/automations';
import AutomationForm from './AutomationForm.svelte';

beforeAll(async () => {
	mocks.get.mockResolvedValueOnce({
		models: [
			{ value: 'haiku', label: 'Haiku', family: 'haiku', supports_1m: true },
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
		profile_id: 'agent-1',
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
	it('starts on the worker default without naming the chat default, and emits the picked model', async () => {
		const { getByTestId, onUpdateFormModel } = renderForm();
		const row = within(getByTestId('automation-model-row'));
		// Unattended runs do not follow the chat default, so no model is named before the worker reports one.
		expect(row.getByText("Uses the worker's default model for unattended runs.")).toBeTruthy();
		expect(row.queryByText(/Sonnet/)).toBeNull();
		expect(row.queryByText(/agent/i)).toBeNull();

		await fireEvent.click(row.getByRole('button', { name: 'Worker default' }));
		await fireEvent.click(row.getByRole('tab', { name: /^Claude/ }));
		await fireEvent.click(row.getByRole('option', { name: /Haiku/ }));
		expect(onUpdateFormModel).toHaveBeenCalledWith('haiku');
	});

	it('emits a provider model as its picker value', async () => {
		const { getByTestId, onUpdateFormModel } = renderForm();
		const row = within(getByTestId('automation-model-row'));
		await fireEvent.click(row.getByRole('button', { name: 'Worker default' }));
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

	it("names the model the worker reports for an existing automation's unattended runs", () => {
		const { getByTestId } = renderForm({
			currentView: 'edit',
			savedAutomation: saved({ effective_model: 'gpt-5.6', effective_provider_id: 'openai-row' })
		});
		const row = within(getByTestId('automation-model-row'));
		expect(row.getByText("Uses GPT-5.6, the worker's default for unattended runs.")).toBeTruthy();
		expect(row.getByRole('button', { name: 'Worker default' })).toBeTruthy();
	});

	it('says so when the agent still carries a legacy saved model', () => {
		const { getByTestId } = renderForm({
			profiles: [{ id: 'agent-1', name: 'Agent One', config: { model: 'gpt-5.6', provider: 'openai-row' } }]
		});
		const row = within(getByTestId('automation-model-row'));
		expect(row.getByText('Uses GPT-5.6, saved on this agent (legacy).')).toBeTruthy();
		expect(row.getByRole('button', { name: 'Saved on agent (legacy)' })).toBeTruthy();
	});

	it("takes the server's report for a saved automation on its own agent", () => {
		const { getByTestId } = renderForm({
			currentView: 'edit',
			savedAutomation: saved({ effective_model: 'haiku', model_source: 'agent' })
		});
		expect(
			within(getByTestId('automation-model-row')).getByText('Uses Haiku, saved on this agent (legacy).')
		).toBeTruthy();
	});

	it('re-reads the fallback when the agent changes', () => {
		const { getByTestId } = renderForm({
			currentView: 'edit',
			formProfileId: 'agent-2',
			profiles: [
				{ id: 'agent-1', name: 'Agent One', config: {} },
				{ id: 'agent-2', name: 'Agent Two', config: { model: 'haiku' } }
			],
			savedAutomation: saved({})
		});
		expect(
			within(getByTestId('automation-model-row')).getByText('Uses Haiku, saved on this agent (legacy).')
		).toBeTruthy();
	});

	it('says why runs fail when the stored model is gone', () => {
		const error =
			"This automation's model is no longer available: The AI provider for this model has been removed. Pick another model in the automation's settings.";
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
		expect(automationModelLabel(saved({}))).toBe('Sonnet (worker default)');
		expect(automationModelLabel(saved({ effective_model: null }))).toBe('Worker default');
		expect(
			automationModelLabel(
				saved({ effective_model: 'gpt-5.6', effective_provider_id: 'openai-row', model_source: 'agent' })
			)
		).toBe('GPT-5.6 (saved on agent)');
		expect(
			automationModelLabel(saved({ effective_model: null, effective_provider_id: 'openai-row', model_source: 'agent' }))
		).toBe('Provider default (saved on agent)');
		expect(automationModelLabel(saved({ model: 'prov:x:y', model_error: 'gone' }))).toBe('Model unavailable');
	});
});

describe('automationFallbackModel', () => {
	it('names no model for the worker default unless the agent carries a legacy model or provider', () => {
		expect(automationFallbackModel(undefined)).toEqual({ label: null, legacy: false });
		expect(automationFallbackModel({})).toEqual({ label: null, legacy: false });
		expect(automationFallbackModel({ model: 'haiku' })).toEqual({ label: 'Haiku', legacy: true });
		// A provider with no model runs on that provider's default.
		expect(automationFallbackModel({ provider: 'openai-row' })).toEqual({ label: 'GPT-5.6', legacy: true });
		expect(automationFallbackModel({ provider: 'gone-row' })).toEqual({ label: 'Provider default', legacy: true });
	});
});

describe('scheduled-run notice', () => {
	const signedOut = { mode: 'subscription', cli: { signed_in: false } };
	const signedIn = { mode: 'subscription', cli: { signed_in: true } };
	const claudeAuth = vi.mocked(getClaudeAuth);

	async function noticeFor(summary: unknown, overrides: Record<string, unknown> = {}) {
		claudeAuth.mockResolvedValueOnce(summary as never);
		const { queryByTestId } = renderForm(overrides);
		await Promise.resolve();
		await Promise.resolve();
		return queryByTestId('automation-api-key-notice');
	}

	it('stays hidden when Claude was never signed in (default subscription mode)', async () => {
		expect(await noticeFor(signedOut)).toBeNull();
	});

	it('stays hidden when the automation runs on another provider', async () => {
		expect(await noticeFor(signedIn, { formModel: encodeProviderModel('openai-row', 'gpt-5.6') })).toBeNull();
	});

	it('shows with no model of its own: unattended runs use Claude while Claude is connected', async () => {
		const notice = await noticeFor(signedIn);
		expect(notice?.textContent).toContain('Claude runs on your subscription');
	});

	it('shows for a Claude model on a signed-in Claude subscription', async () => {
		const notice = await noticeFor(signedIn, { formModel: 'haiku' });
		expect(notice?.textContent).toContain('Claude runs on your subscription');
	});
});

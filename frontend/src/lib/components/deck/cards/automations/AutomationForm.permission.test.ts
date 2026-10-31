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

import { hydrateModelRegistry } from '$lib/constants/models';
import {
	DEFAULT_AUTOMATION_PERMISSION_MODE,
	automationPermissionLabel,
	formPermissionMode,
	permissionModeLabel,
	permissionModePayload,
	type Automation
} from '$lib/api/automations';
import AutomationForm from './AutomationForm.svelte';

const BYPASS_HINT = 'Runs every tool, including shell commands, without asking — nobody is watching an automation.';

beforeAll(async () => {
	mocks.get.mockResolvedValueOnce({
		models: [
			{ value: 'haiku', label: 'Haiku', family: 'haiku', supports_1m: true },
			{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true, auto_permission_supported: true }
		],
		default: 'haiku',
		legacy_map: {},
		claude: { auth_kind: 'subscription', plan_type: null },
		providers: []
	});
	await hydrateModelRegistry();
});

afterEach(() => {
	cleanup();
});

function renderForm(overrides: Record<string, unknown> = {}) {
	const onUpdateFormPermissionMode = vi.fn();
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
		formPermissionMode: DEFAULT_AUTOMATION_PERMISSION_MODE,
		formActionSteps: [],
		profiles: [{ id: 'agent-1', name: 'Agent One', config: {} }],
		projects: [],
		connectors: [],
		connectorTypes: [],
		connectorsWithTriggers: [],
		connectorsWithActions: [],
		expandedTriggers: new Set<number>(),
		expandedActions: new Set<number>(),
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
		onUpdateFormModel: vi.fn(),
		onUpdateFormPermissionMode,
		onUpdateFormActionSteps: vi.fn(),
		onToggleSection: vi.fn(),
		onToggleTriggerExpand: vi.fn(),
		onToggleActionExpand: vi.fn(),
		...overrides
	};
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	const utils = render(AutomationForm, { props: props as any });
	const row = within(utils.getByTestId('automation-permission-row'));
	return { ...utils, row, onUpdateFormPermissionMode };
}

function saved(fields: Partial<Automation>): Automation {
	return {
		id: 'auto-1',
		profile_id: 'agent-1',
		model: null,
		provider_id: null,
		effective_model: 'haiku',
		model_source: 'default',
		permission_mode: null,
		effective_permission_mode: 'default',
		permission_mode_source: 'default',
		...fields
	} as Automation;
}

function checked(row: ReturnType<typeof within>) {
	return row
		.getAllByRole('radio')
		.filter((b: HTMLElement) => b.getAttribute('aria-checked') === 'true')
		.map((b: HTMLElement) => b.textContent);
}

describe('AutomationForm permission picker', () => {
	it('starts a new automation on Bypass and explains what that means', () => {
		const { row, queryByTestId } = renderForm();
		expect(checked(row)).toEqual(['Bypass']);
		expect(queryByTestId('automation-bypass-hint')?.textContent).toContain(BYPASS_HINT);
	});

	it("offers the composer's modes and emits the pick", async () => {
		const { row, onUpdateFormPermissionMode } = renderForm();
		// The run model (worker default Haiku) has no Auto, as in the composer.
		expect(row.getAllByRole('radio').map((b) => b.textContent)).toEqual(['Ask', 'Accept Edits', 'Bypass']);
		await fireEvent.click(row.getByRole('radio', { name: 'Accept Edits' }));
		expect(onUpdateFormPermissionMode).toHaveBeenCalledWith('acceptEdits');
	});

	it('offers Auto when the run model supports it', () => {
		const { row } = renderForm({ formModel: 'sonnet' });
		expect(row.getAllByRole('radio').map((b) => b.textContent)).toEqual(['Ask', 'Auto', 'Accept Edits', 'Bypass']);
	});

	it('hides the Bypass warning on another mode', () => {
		const { row, queryByTestId } = renderForm({ formPermissionMode: 'acceptEdits' });
		expect(checked(row)).toEqual(['Accept Edits']);
		expect(queryByTestId('automation-bypass-hint')).toBeNull();
	});

	it('shows Ask for an existing automation saved before the choice existed', () => {
		const automation = saved({});
		const { row, queryByTestId } = renderForm({
			currentView: 'edit',
			savedAutomation: automation,
			formPermissionMode: formPermissionMode(automation, {})
		});
		expect(checked(row)).toEqual(['Ask']);
		expect(queryByTestId('automation-bypass-hint')).toBeNull();
	});

	it("says so when a legacy automation uses its agent's saved mode", () => {
		const automation = saved({ effective_permission_mode: 'acceptEdits', permission_mode_source: 'agent' });
		const { row } = renderForm({
			currentView: 'edit',
			savedAutomation: automation,
			formPermissionMode: formPermissionMode(automation, { permission_mode: 'acceptEdits' })
		});
		expect(checked(row)).toEqual(['Accept Edits']);
		expect(row.getByText(/Saved on this agent \(legacy\)/)).toBeTruthy();
	});
});

describe('formPermissionMode', () => {
	it('defaults new automations to Bypass and existing ones to what their runs use', () => {
		expect(formPermissionMode(null)).toBe('bypassPermissions');
		expect(formPermissionMode(saved({ permission_mode: 'acceptEdits' }))).toBe('acceptEdits');
		expect(formPermissionMode(saved({}))).toBe('default');
		expect(formPermissionMode(saved({ effective_permission_mode: null }), { permission_mode: 'bypassPermissions' })).toBe(
			'bypassPermissions'
		);
		expect(formPermissionMode(saved({ effective_permission_mode: null }), {})).toBe('default');
		// The retired plan mode reads as Ask.
		expect(formPermissionMode(saved({ permission_mode: 'plan' as never }))).toBe('default');
	});
});

describe('permissionModePayload', () => {
	it('always sends the mode for a new automation', () => {
		expect(permissionModePayload(null, 'bypassPermissions')).toBe('bypassPermissions');
		expect(permissionModePayload(null, 'default')).toBe('default');
	});

	it('leaves a legacy NULL row NULL until the owner picks another mode', () => {
		const legacy = saved({});
		expect(permissionModePayload(legacy, 'default', {})).toBeUndefined();
		expect(permissionModePayload(legacy, 'bypassPermissions', {})).toBe('bypassPermissions');
	});

	it("always sends an automation's own mode", () => {
		const own = saved({ permission_mode: 'acceptEdits', effective_permission_mode: 'acceptEdits', permission_mode_source: 'automation' });
		expect(permissionModePayload(own, 'acceptEdits')).toBe('acceptEdits');
		expect(permissionModePayload(own, 'default')).toBe('default');
	});
});

describe('permission labels', () => {
	it("uses the composer's labels", () => {
		expect(permissionModeLabel('bypassPermissions')).toBe('Bypass');
		expect(permissionModeLabel('acceptEdits')).toBe('Accept Edits');
		expect(permissionModeLabel('auto')).toBe('Auto');
		expect(permissionModeLabel(null)).toBe('Ask');
		expect(automationPermissionLabel(saved({ permission_mode: 'bypassPermissions' }))).toBe('Bypass');
		expect(automationPermissionLabel(saved({}))).toBe('Ask');
		expect(
			automationPermissionLabel(saved({ effective_permission_mode: 'acceptEdits', permission_mode_source: 'agent' }))
		).toBe('Accept Edits (saved on agent)');
	});
});

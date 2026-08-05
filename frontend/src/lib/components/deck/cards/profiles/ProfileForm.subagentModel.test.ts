/**
 * Behavior tests for the optional cross-provider subagent-model leg (Chunk F).
 *
 * These mount the real ProfileForm on its "general" tab with a hydrated model
 * registry (one provider carrying a colon-bearing model id) and drive the
 * ModelPicker dropdowns end-to-end. Covered:
 *  - the "Subagent model" row is HIDDEN for a native-Claude profile (provider
 *    null) — ruling A2's UI gate — and VISIBLE once the profile runs on a
 *    provider model.
 *  - selecting "Inherit (default)" in the subagent picker clears BOTH
 *    subagent_provider and subagent_model.
 *  - selecting a provider model sets both fields, decoding the composite on the
 *    FIRST colon only (so a model id that itself contains a colon round-trips).
 *  - switching the MAIN picker to a native model clears the whole subagent leg
 *    (a hidden stale cross-provider leg must never be saved).
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, cleanup } from '@testing-library/svelte';

// jsdom lacks ResizeObserver; some lucide/layout paths probe for it.
if (!('ResizeObserver' in globalThis)) {
	(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
		observe() {}
		unobserve() {}
		disconnect() {}
	};
}

const mocks = vi.hoisted(() => ({ apiGet: vi.fn() }));

// The model registry hydrates through `api.get('/models')`; computer-use status
// is fetched in onMount — stub the latter so the mount does no network I/O.
vi.mock('$lib/api/client', () => ({ api: { get: mocks.apiGet } }));
vi.mock('$lib/api/computerUse', () => ({
	getComputerUseStatus: vi.fn().mockResolvedValue({ available: true, reason: 'ok' })
}));

import ProfileForm from './ProfileForm.svelte';
import { hydrateModelRegistry } from '$lib/constants/models';

// The subagent picker is `servableOnly` (#789): it offers ONLY providers the
// translator gateway can serve — provider_type openai / chatgpt / anthropic,
// mirroring `is_gateway_servable` in app/core/environment.py. So the provider
// carrying the pickable models must be a servable type; a second, NON-servable
// provider pins the exclusion.
const PROVIDER_ID = 'oai';
const COLON_MODEL = 'meta-llama/llama-3.1:free'; // id containing a colon
const COLON_MODEL_LABEL = 'Llama 3.1 Free';
const PLAIN_MODEL = 'openai/gpt-5.6';
const UNSERVABLE_PROVIDER_ID = 'or';
const UNSERVABLE_MODEL_LABEL = 'Qwen Coder';

beforeAll(async () => {
	mocks.apiGet.mockResolvedValue({
		models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true }],
		default: 'sonnet',
		legacy_map: {},
		providers: [
			{
				id: PROVIDER_ID,
				name: 'OpenAI Compatible',
				provider_type: 'openai',
				models: [
					{ id: COLON_MODEL, label: COLON_MODEL_LABEL },
					{ id: PLAIN_MODEL, label: 'GPT-5.6' }
				],
				default_model: PLAIN_MODEL
			},
			{
				id: UNSERVABLE_PROVIDER_ID,
				name: 'OpenRouter',
				provider_type: 'openrouter',
				models: [{ id: 'qwen/qwen-coder', label: UNSERVABLE_MODEL_LABEL }],
				default_model: 'qwen/qwen-coder'
			}
		]
	});
	await hydrateModelRegistry();
});

afterEach(() => cleanup());

function baseForm(overrides: Record<string, unknown> = {}) {
	return {
		id: '',
		name: 'Test',
		description: '',
		model: 'sonnet',
		provider: null,
		subagent_provider: null,
		subagent_model: null,
		permission_mode: 'default',
		max_turns: null,
		allowed_tools: [],
		disallowed_tools: [],
		enabled_agents: [],
		include_partial_messages: true,
		thinking_mode: 'default',
		effort: null,
		system_prompt_type: 'preset',
		system_prompt_preset: 'claude_code',
		system_prompt_append: '',
		system_prompt_content: '',
		system_prompt_inject_env: false,
		enabled_ai_tools: [],
		setting_sources: [],
		cwd: '',
		add_dirs: '',
		user: '',
		max_buffer_size: null,
		env_vars: [],
		enable_file_checkpointing: false,
		app_config_tools: false,
		strict_mcp_config: false,
		computer_use_enabled: false,
		...overrides
	};
}

function renderForm(formOverrides: Record<string, unknown> = {}, onFormChange = vi.fn()) {
	const props = {
		activeTab: 'general',
		editingProfile: null,
		profileForm: baseForm(formOverrides),
		toolSelectionMode: 'all',
		customModelMode: false,
		groups: {},
		allSubagents: [],
		builtinAgentIds: [],
		customAgents: [],
		sdkToolCategories: [],
		aiToolCategories: [],
		aiToolsLoading: false,
		enabledSdkToolCount: 0,
		aiToolCount: 0,
		effectiveToolCount: 0,
		expandedSections: {},
		envVarPresets: [],
		onFormChange,
		onToolSelectionModeChange: vi.fn(),
		onCustomModelModeChange: vi.fn(),
		onToggleTool: vi.fn(),
		onToggleCategory: vi.fn(),
		onToggleAITool: vi.fn(),
		onToggleAIToolCategory: vi.fn(),
		onToggleSection: vi.fn(),
		onToggleAgent: vi.fn(),
		onToggleSettingSource: vi.fn(),
		onAssignToGroup: vi.fn(),
		onRemoveFromGroup: vi.fn(),
		onCreateGroup: vi.fn(),
		onDeleteGroup: vi.fn(),
		getCategorySelectionState: () => 'all' as const,
		getAICategorySelectionState: () => 'all' as const,
		getModelDisplay: (m?: string) => m ?? ''
	};
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	return render(ProfileForm, { props: props as any });
}

/** All picker triggers, in DOM order: [0] = main Model, [1] = Subagent model. */
function triggers(container: HTMLElement): HTMLButtonElement[] {
	return Array.from(container.querySelectorAll<HTMLButtonElement>('.mpk-trigger'));
}

/** Click a row (by its visible label) inside the currently-open picker panel. */
async function clickRow(container: HTMLElement, label: string) {
	const panel = container.querySelector('.mpk-panel');
	if (!panel) throw new Error('no open picker panel');
	const rows = Array.from(panel.querySelectorAll<HTMLElement>('.mpk-label'));
	const target = rows.find((r) => r.textContent?.trim() === label);
	if (!target) throw new Error(`row "${label}" not found in panel`);
	await fireEvent.click(target);
}

describe('ProfileForm — subagent-model row visibility (A2 UI gate)', () => {
	it('is hidden for a native-Claude profile (provider null)', () => {
		const { container } = renderForm({ provider: null });
		expect(container.textContent).not.toContain('Subagent model');
		// Only the main Model picker is present.
		expect(triggers(container)).toHaveLength(1);
	});

	it('is visible once the profile runs on a provider model', () => {
		const { container } = renderForm({ provider: PROVIDER_ID, model: PLAIN_MODEL });
		expect(container.textContent).toContain('Subagent model');
		expect(triggers(container)).toHaveLength(2);
	});
});

describe('ProfileForm — subagent picker selection', () => {
	it('inherit selection clears both subagent fields', async () => {
		const onFormChange = vi.fn();
		const { container } = renderForm(
			{
				provider: PROVIDER_ID,
				model: PLAIN_MODEL,
				subagent_provider: PROVIDER_ID,
				subagent_model: PLAIN_MODEL
			},
			onFormChange
		);
		await fireEvent.click(triggers(container)[1]); // open subagent picker
		await clickRow(container, 'Inherit (default)');

		expect(onFormChange).toHaveBeenCalledWith('subagent_provider', null);
		expect(onFormChange).toHaveBeenCalledWith('subagent_model', null);
	});

	it('composite selection sets both fields, decoding a colon-bearing model id', async () => {
		const onFormChange = vi.fn();
		const { container } = renderForm(
			{ provider: PROVIDER_ID, model: PLAIN_MODEL },
			onFormChange
		);
		await fireEvent.click(triggers(container)[1]);
		await clickRow(container, COLON_MODEL_LABEL);

		expect(onFormChange).toHaveBeenCalledWith('subagent_provider', PROVIDER_ID);
		expect(onFormChange).toHaveBeenCalledWith('subagent_model', COLON_MODEL);
	});

	it('the subagent picker offers no Custom… entry (provider-models only)', async () => {
		const { container } = renderForm({ provider: PROVIDER_ID, model: PLAIN_MODEL });
		await fireEvent.click(triggers(container)[1]);
		const panel = container.querySelector('.mpk-panel')!;
		expect(panel.textContent).toContain('Inherit (default)');
		expect(panel.textContent).not.toContain('Custom…');
		// Native Claude models are suppressed too.
		expect(panel.textContent).not.toContain('Sonnet');
	});

	it('excludes non-gateway-servable providers (#789 servableOnly gate)', async () => {
		const { container } = renderForm({ provider: PROVIDER_ID, model: PLAIN_MODEL });
		await fireEvent.click(triggers(container)[1]);
		const panel = container.querySelector('.mpk-panel')!;
		// The servable (openai-type) provider's models are offered…
		expect(panel.textContent).toContain(COLON_MODEL_LABEL);
		// …but the openrouter-type provider is filtered out: the gateway can't
		// serve it, so a subagent leg pointing there could never route.
		expect(panel.textContent).not.toContain(UNSERVABLE_MODEL_LABEL);
	});
});

describe('ProfileForm — main picker to native clears the leg', () => {
	it('selecting a native model clears both provider and subagent fields', async () => {
		const onFormChange = vi.fn();
		const { container } = renderForm(
			{
				provider: PROVIDER_ID,
				model: PLAIN_MODEL,
				subagent_provider: PROVIDER_ID,
				subagent_model: COLON_MODEL
			},
			onFormChange
		);
		await fireEvent.click(triggers(container)[0]); // open MAIN picker
		await clickRow(container, 'Sonnet');

		expect(onFormChange).toHaveBeenCalledWith('provider', null);
		expect(onFormChange).toHaveBeenCalledWith('subagent_provider', null);
		expect(onFormChange).toHaveBeenCalledWith('subagent_model', null);
	});
});

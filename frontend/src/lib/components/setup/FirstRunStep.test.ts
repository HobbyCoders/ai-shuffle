/**
 * The first-run step names the provider that still needs a model and links
 * back to the AI provider step, instead of sending a run the worker refuses.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	status: {} as Record<string, unknown>,
	rows: [] as unknown[],
	registry: [] as unknown[],
	start: vi.fn()
}));
vi.mock('$lib/stores/auth.svelte', () => ({
	auth: { checkAuth: async () => mocks.status, get current() { return { status: mocks.status }; } }
}));
vi.mock('$lib/stores/providers.svelte', () => ({
	providers: {
		get current() {
			return { providers: mocks.rows, claude: null, anthropicKeyMasked: null, falKeySet: false, falKeyMasked: '', falEnabled: true };
		},
		refreshAll: async () => {}
	}
}));
vi.mock('$lib/constants/models.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/constants/models.svelte')>()),
	getDefaultModel: () => 'sonnet',
	getModels: () => [],
	getProviders: () => mocks.registry,
	hydrateModelRegistry: async () => true
}));
vi.mock('$lib/services/first-agent-run', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/services/first-agent-run')>()),
	ensureStarterAgent: async () => ({ id: 'agent' }),
	startFirstAgentRun: mocks.start
}));
import FirstRunStep from './FirstRunStep.svelte';

const gate = (ready: string[]) => ({
	ready: true, mode: 'subscription', claude_credential_ready: false, any_provider_ready: true, ready_providers: ready
});
const openaiRow = (models: { id: string; label: string }[]) => ({
	id: 'oa', name: 'OpenAI API key', provider_type: 'openai', base_url: 'https://api.openai.com/v1', auth_token_set: true,
	auth_token_masked: '', models, default_model: null, small_fast_model: null, extra_env: {}, context_window: null,
	max_output_tokens: null, enabled: true, created_at: null, updated_at: null
});

beforeEach(() => {
	vi.clearAllMocks();
	mocks.status = { agent_gate: gate(['openai']) };
});
afterEach(cleanup);

describe('first run with no model picked', () => {
	it('names the provider, links back to AI provider and does not run', async () => {
		mocks.rows = [openaiRow([])];
		mocks.registry = [];
		const onFixProvider = vi.fn();
		render(FirstRunStep, { onContinue: vi.fn(), onFixProvider });
		const notice = await screen.findByTestId('setup-first-run-needs-model');
		expect(notice.textContent).toContain('OpenAI needs a model');
		expect((screen.getByRole('button', { name: /Run it/ }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.click(screen.getByRole('button', { name: 'Go back to AI provider' }));
		expect(onFixProvider).toHaveBeenCalledOnce();
		expect(mocks.start).not.toHaveBeenCalled();
	});

	it('names the model and provider once a model is picked', async () => {
		const models = [{ id: 'gpt-5.5', label: 'GPT-5.5' }];
		mocks.rows = [openaiRow(models)];
		mocks.registry = [{ id: 'oa', name: 'OpenAI API key', provider_type: 'openai', models, default_model: 'gpt-5.5', auth_kind: 'api', plan_type: null }];
		render(FirstRunStep, { onContinue: vi.fn() });
		expect((await screen.findByText(/GPT-5\.5 on OpenAI/)).textContent).toContain('billed to your own account');
		expect(screen.queryByTestId('setup-first-run-needs-model')).toBeNull();
	});
});

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	status: {} as Record<string, unknown>,
	store: { providers: [] as unknown[], claude: null as unknown, anthropicKeyMasked: null, falKeySet: false, falKeyMasked: '', falEnabled: true },
	refreshAll: vi.fn(),
	checkAuth: vi.fn(),
	fetchModels: vi.fn(),
}));
vi.mock('$lib/api/model-providers', () => ({ fetchProviderModels: mocks.fetchModels }));
vi.mock('$lib/stores/auth.svelte', () => ({
	auth: { checkAuth: mocks.checkAuth, get current() { return { status: mocks.status }; } },
}));
vi.mock('$lib/stores/providers.svelte', () => ({
	providers: { get current() { return mocks.store; }, refreshAll: mocks.refreshAll, refreshProviders: vi.fn() },
}));
vi.mock('./ClaudeConnect.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('./ProviderConnect.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
import ProviderStep from './ProviderStep.svelte';
import { providerReadiness } from './provider-readiness';
import { buildProviders } from '$lib/components/settings/providers/providers-model';

/** A model_providers row; `models` empty means no model picked yet. */
const row = (id: string, provider_type: string, models: { id: string; label: string }[] = [{ id: 'm1', label: 'M1' }]) => ({
	id, name: id, provider_type, base_url: 'https://example.test/v1', auth_token_set: true, auth_token_masked: '',
	models, default_model: null, small_fast_model: null, extra_env: {}, context_window: null, max_output_tokens: null,
	enabled: true, created_at: null, updated_at: null,
});
const withRows = (...rows: unknown[]) => { mocks.store = { ...mocks.store, providers: rows }; };

const gate = (ready: string[], extra: Record<string, unknown> = {}) => ({
	ready: ready.length > 0, mode: 'subscription', claude_credential_ready: ready.includes('claude'),
	any_provider_ready: ready.length > 0, ready_providers: ready, ...extra,
});

beforeEach(() => {
	vi.resetAllMocks();
	mocks.status = { agent_gate: gate([]) };
	mocks.store = { providers: [], claude: null, anthropicKeyMasked: null, falKeySet: false, falKeyMasked: '', falEnabled: true };
	mocks.checkAuth.mockImplementation(async () => mocks.status);
	mocks.refreshAll.mockResolvedValue(undefined);
	mocks.fetchModels.mockResolvedValue({ source: 'upstream', models: [] });
});
afterEach(() => cleanup());

const continueButton = () => screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement;

describe('setup AI provider step', () => {
	it('lists every agent provider as an equal choice in the Settings order, none open, no fal.ai', async () => {
		render(ProviderStep, { isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		const list = await screen.findByRole('list', { name: 'AI providers' });
		const names = [...list.querySelectorAll('.name')].map((n) => n.textContent);
		expect(names).toEqual(['OpenAI', 'xAI Grok', 'Claude', 'OpenRouter', 'Your own gateway']);
		const settingsOrder = buildProviders({ rows: [], claude: null, anthropicKeyMasked: null, fal: { keySet: false, masked: '', enabled: true } })
			.filter((v) => !v.media).map((v) => v.name);
		expect(names.slice(0, -1)).toEqual(settingsOrder);
		expect(list.textContent).not.toMatch(/fal\.ai|recommended/i);
		expect([...list.querySelectorAll('button[aria-expanded="true"]')]).toHaveLength(0);
	});

	it('blocks Continue with zero providers ready', async () => {
		render(ProviderStep, { isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		expect((await screen.findByTestId('setup-provider-missing')).textContent).toContain('Connect at least one provider');
		expect(continueButton().disabled).toBe(true);
	});

	it('allows Continue with only a non-Claude provider ready', async () => {
		mocks.status = { agent_gate: gate(['openai']) };
		withRows(row('oa', 'openai'));
		const onContinue = vi.fn();
		render(ProviderStep, { isAdmin: true, onContinue, pollMs: 60_000 });
		expect((await screen.findByTestId('setup-provider-ready')).textContent).toContain('Connected: OpenAI.');
		expect(screen.getByTestId('setup-provider-openai-connected')).toBeTruthy();
		expect(screen.queryByTestId('setup-provider-claude-connected')).toBeNull();
		await fireEvent.click(continueButton());
		expect(onContinue).toHaveBeenCalledOnce();
	});

	it('allows Continue with only Claude ready', async () => {
		mocks.status = { agent_gate: gate(['claude']) };
		render(ProviderStep, { isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		expect((await screen.findByTestId('setup-provider-ready')).textContent).toContain('Connected: Claude.');
		expect(continueButton().disabled).toBe(false);
	});

	it('picks up a provider connected meanwhile on Check again', async () => {
		withRows(row('gx', 'xai'), row('abc', 'custom'));
		render(ProviderStep, { isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		await screen.findByTestId('setup-provider-missing');
		mocks.status = { agent_gate: gate(['grok', 'gw:abc']) };
		await fireEvent.click(screen.getByRole('button', { name: /Check again/ }));
		await waitFor(() => expect(continueButton().disabled).toBe(false));
		expect(screen.getByTestId('setup-provider-gateway-connected').textContent).toContain('Connected');
	});

	it('opens one provider at a time', async () => {
		render(ProviderStep, { isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		await fireEvent.click(await screen.findByTestId('setup-provider-openai'));
		expect(screen.getByTestId('setup-provider-openai').getAttribute('aria-expanded')).toBe('true');
		await fireEvent.click(screen.getByTestId('setup-provider-claude'));
		expect(screen.getByTestId('setup-provider-openai').getAttribute('aria-expanded')).toBe('false');
		expect(screen.getByTestId('setup-provider-claude').getAttribute('aria-expanded')).toBe('true');
	});

	it('falls back to the connected credentials on an older worker', async () => {
		mocks.status = { agent_gate: { ready: false, mode: 'subscription', claude_credential_ready: false } };
		mocks.store = { ...mocks.store, providers: [{
			id: 'k1', name: 'OpenRouter', provider_type: 'openrouter', base_url: 'https://openrouter.ai/api', auth_token_set: true,
			auth_token_masked: '', models: [{ id: 'm', label: 'M' }], default_model: null, small_fast_model: null, extra_env: {}, context_window: null,
			max_output_tokens: null, enabled: true,
		}] };
		render(ProviderStep, { isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		expect((await screen.findByTestId('setup-provider-ready')).textContent).toContain('Connected: OpenRouter.');
	});
});

describe('setup AI provider step: a model is required', () => {
	it('a provider the worker reports ready but with no model picked is not Connected', async () => {
		mocks.status = { agent_gate: gate(['openai']) };
		withRows(row('oa', 'openai', []));
		render(ProviderStep, { isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		const missing = await screen.findByTestId('setup-provider-missing');
		expect(missing.textContent).toContain('OpenAI has no model picked yet.');
		expect(screen.queryByTestId('setup-provider-openai-connected')).toBeNull();
		expect(screen.queryByTestId('setup-provider-ready')).toBeNull();
		expect(continueButton().disabled).toBe(true);
	});

	it('Check again re-runs the model fetch for a stuck key before re-reading the status', async () => {
		withRows(row('bad', 'openai', []));
		render(ProviderStep, { isAdmin: true, onContinue: vi.fn(), pollMs: 60_000 });
		await screen.findByTestId('setup-provider-missing');
		mocks.checkAuth.mockClear();
		await fireEvent.click(screen.getByRole('button', { name: /Check again/ }));
		await waitFor(() => expect(mocks.fetchModels).toHaveBeenCalledWith('bad'));
		await waitFor(() => expect(mocks.checkAuth).toHaveBeenCalled());
		expect(mocks.fetchModels.mock.invocationCallOrder[0]).toBeLessThan(mocks.checkAuth.mock.invocationCallOrder[0]);
	});
});

describe('provider readiness', () => {
	it('trusts the gate fields when present, even if the store disagrees', () => {
		expect(providerReadiness({ ready: false, mode: null, claude_credential_ready: false, any_provider_ready: false, ready_providers: [] }, []))
			.toEqual({ anyReady: false, ready: [], source: 'gate' });
		expect(providerReadiness(null, [])).toEqual({ anyReady: false, ready: [], source: 'store' });
	});
});

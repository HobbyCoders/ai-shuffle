/**
 * `getProviders()` hydration — separate file from models.test.ts so the
 * `$lib/api/client` mock and the resulting `_registry` mutation (hydration
 * replaces module-singleton state) never leak into the static-registry
 * assertions there; vitest isolates module state per test file.
 */
import { describe, it, expect, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get }
}));

import {
	getModelLabel,
	getProviders,
	getProviderDisplay,
	getClaudeDisplay,
	getClaudeAuth,
	formatProviderDisplay,
	hydrateModelRegistry,
	isRegistryHydrated
} from './models';

describe('getProviders — hydration from GET /models', () => {
	it('is empty before hydration (no static fallback for providers)', () => {
		expect(getProviders()).toEqual([]);
	});

	it('populates from the registry payload on successful hydration', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 }],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'prov-1',
					name: 'OpenRouter',
					provider_type: 'openrouter',
					models: [{ id: 'openai/gpt-5.6', label: 'GPT-5.6' }],
					default_model: 'openai/gpt-5.6'
				}
			]
		});

		await hydrateModelRegistry();

		expect(isRegistryHydrated()).toBe(true);
		expect(getProviders()).toEqual([
			{
				id: 'prov-1',
				name: 'OpenRouter',
				provider_type: 'openrouter',
				models: [{ id: 'openai/gpt-5.6', label: 'GPT-5.6' }],
				default_model: 'openai/gpt-5.6',
				// Additive display metadata — absent from this payload, so it
				// normalises to API-key auth with no plan string.
				auth_kind: 'api',
				plan_type: null
			}
		]);
	});

	it('carries auth_kind/plan_type through hydration', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 }],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'prov-chatgpt',
					name: 'ChatGPT',
					provider_type: 'chatgpt',
					models: [{ id: 'gpt-5.6', label: 'GPT-5.6' }],
					default_model: 'gpt-5.6',
					auth_kind: 'subscription',
					plan_type: 'prolite'
				},
				{
					id: 'prov-openai',
					name: 'OpenAI',
					provider_type: 'openai',
					models: [{ id: 'gpt-5.6', label: 'GPT-5.6' }],
					default_model: 'gpt-5.6',
					auth_kind: 'api',
					plan_type: null
				}
			]
		});

		await hydrateModelRegistry();

		const [chatgpt, openai] = getProviders();
		expect(chatgpt.auth_kind).toBe('subscription');
		expect(chatgpt.plan_type).toBe('prolite');
		expect(openai.auth_kind).toBe('api');
		expect(openai.plan_type).toBeNull();
		// The two rows stay SEPARATE entries even though both display as "OpenAI".
		expect(getProviders().length).toBe(2);
	});

	it('infers auth_kind from provider_type when an older payload omits it', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 }],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'prov-chatgpt',
					name: 'ChatGPT',
					provider_type: 'chatgpt',
					models: [],
					default_model: null
				}
			]
		});

		await hydrateModelRegistry();

		expect(getProviders()[0].auth_kind).toBe('subscription');
		expect(getProviders()[0].plan_type).toBeNull();
	});

	it('carries DIFFERENT per-model context_windows through hydration', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 }],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'prov-1',
					name: 'OpenRouter',
					provider_type: 'openrouter',
					models: [
						{ id: 'openai/gpt-5.6', label: 'GPT-5.6', context_window: 400000 },
						// The backend resolves per MODEL now (own value → preset → row
						// column), so two models on ONE row can disagree — the payload
						// must carry that through instead of flattening it.
						{ id: 'vendor/big', label: 'Big', context_window: 1_000_000 },
						{ id: 'vendor/plain', label: 'Plain', context_window: null }
					],
					default_model: 'openai/gpt-5.6'
				}
			]
		});

		await hydrateModelRegistry();

		const models = getProviders()[0].models;
		expect(models[0].context_window).toBe(400000);
		expect(models[1].context_window).toBe(1_000_000);
		expect(models[2].context_window).toBeNull();
	});

	it('falls back to an empty provider list when the payload omits providers', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 }],
			default: 'sonnet',
			legacy_map: {}
			// no `providers` key — mirrors an older/degraded backend response.
		});

		await hydrateModelRegistry();

		expect(getProviders()).toEqual([]);
	});
});

describe('getClaudeAuth — native-Claude auth block hydration', () => {
	/** A minimal valid payload plus whatever `claude` block the case needs. */
	function payload(claude?: unknown) {
		return {
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 }],
			default: 'sonnet',
			legacy_map: {},
			...(claude === undefined ? {} : { claude })
		};
	}

	it('is null/null before hydration', () => {
		expect(getClaudeAuth()).toEqual({ auth_kind: null, plan_type: null });
	});

	it('carries a subscription block through hydration', async () => {
		mocks.get.mockResolvedValueOnce(payload({ auth_kind: 'subscription', plan_type: 'max' }));

		await hydrateModelRegistry();

		expect(getClaudeAuth()).toEqual({ auth_kind: 'subscription', plan_type: 'max' });
		// …and the display rules read it as the group subtext.
		expect(
			getClaudeDisplay({
				planType: getClaudeAuth().plan_type,
				authKind: getClaudeAuth().auth_kind
			})
		).toEqual({ name: 'Claude', subtext: 'max' });
	});

	it('carries an API-key block through hydration', async () => {
		mocks.get.mockResolvedValueOnce(payload({ auth_kind: 'api', plan_type: null }));

		await hydrateModelRegistry();

		expect(getClaudeAuth()).toEqual({ auth_kind: 'api', plan_type: null });
		expect(
			getClaudeDisplay({
				planType: getClaudeAuth().plan_type,
				authKind: getClaudeAuth().auth_kind
			})
		).toEqual({ name: 'Claude', subtext: 'API' });
	});

	it('normalises to nulls when an older payload omits the block', async () => {
		mocks.get.mockResolvedValueOnce(payload());

		await hydrateModelRegistry();

		expect(getClaudeAuth()).toEqual({ auth_kind: null, plan_type: null });
	});

	it('rejects an unrecognised auth_kind rather than passing it through', async () => {
		mocks.get.mockResolvedValueOnce(payload({ auth_kind: 'wat', plan_type: 'max' }));

		await hydrateModelRegistry();

		// Unknown kind → null (the display rules must never see a third value);
		// the plan string is still carried.
		expect(getClaudeAuth()).toEqual({ auth_kind: null, plan_type: 'max' });
	});

	it('tolerates a null/degraded block from a backend that could not resolve it', async () => {
		mocks.get.mockResolvedValueOnce(payload({ auth_kind: null, plan_type: null }));

		await hydrateModelRegistry();

		expect(getClaudeAuth()).toEqual({ auth_kind: null, plan_type: null });
	});

	it('re-hydration replaces the block (a plan change is picked up)', async () => {
		mocks.get.mockResolvedValueOnce(payload({ auth_kind: 'subscription', plan_type: 'pro' }));
		await hydrateModelRegistry();
		expect(getClaudeAuth().plan_type).toBe('pro');

		mocks.get.mockResolvedValueOnce(payload({ auth_kind: 'subscription', plan_type: 'max' }));
		await hydrateModelRegistry();
		expect(getClaudeAuth().plan_type).toBe('max');
	});
});

describe('getProviderDisplay / getClaudeDisplay — brand + auth-kind subtext', () => {
	it('shows a ChatGPT-subscription row as OpenAI + its plan', () => {
		expect(
			getProviderDisplay({
				name: 'ChatGPT',
				provider_type: 'chatgpt',
				auth_kind: 'subscription',
				plan_type: 'prolite'
			})
		).toEqual({ name: 'OpenAI', subtext: 'prolite' });
	});

	it('shows an OpenAI API-key row as OpenAI + API', () => {
		expect(
			getProviderDisplay({ name: 'OpenAI', provider_type: 'openai', auth_kind: 'api' })
		).toEqual({ name: 'OpenAI', subtext: 'API' });
	});

	it('keeps other API-key rows on their own name + API', () => {
		expect(
			getProviderDisplay({ name: 'OpenRouter', provider_type: 'openrouter', auth_kind: 'api' })
		).toEqual({ name: 'OpenRouter', subtext: 'API' });
		expect(
			getProviderDisplay({ name: 'My Gateway', provider_type: 'custom', auth_kind: 'api' })
		).toEqual({ name: 'My Gateway', subtext: 'API' });
		expect(
			getProviderDisplay({ name: 'Anthropic', provider_type: 'anthropic', auth_kind: 'api' })
		).toEqual({ name: 'Anthropic', subtext: 'API' });
	});

	it('honours auth_kind over provider_type for a custom row on Codex auth', () => {
		expect(
			getProviderDisplay({
				name: 'My Gateway',
				provider_type: 'custom',
				auth_kind: 'subscription',
				plan_type: 'plus'
			})
		).toEqual({ name: 'My Gateway', subtext: 'plus' });
	});

	it('falls back to a generic subscription subtext when the plan is unknown', () => {
		expect(
			getProviderDisplay({ name: 'ChatGPT', provider_type: 'chatgpt', auth_kind: 'subscription' })
		).toEqual({ name: 'OpenAI', subtext: 'Subscription' });
	});

	it('infers the auth kind from provider_type when auth_kind is absent', () => {
		expect(getProviderDisplay({ name: 'ChatGPT', provider_type: 'chatgpt' }).subtext).toBe(
			'Subscription'
		);
		expect(getProviderDisplay({ name: 'OpenRouter', provider_type: 'openrouter' }).subtext).toBe(
			'API'
		);
	});

	it('falls back to the provider type, then a generic name, when the row has no name', () => {
		expect(getProviderDisplay({ provider_type: 'openrouter' }).name).toBe('openrouter');
		expect(getProviderDisplay({}).name).toBe('Provider');
	});

	it('names the native Claude group by plan, by API-key auth, or bare', () => {
		expect(getClaudeDisplay({ planType: 'max' })).toEqual({ name: 'Claude', subtext: 'max' });
		expect(getClaudeDisplay({ authKind: 'api' })).toEqual({ name: 'Claude', subtext: 'API' });
		expect(getClaudeDisplay()).toEqual({ name: 'Claude', subtext: null });
		expect(getClaudeDisplay({ planType: '  ' })).toEqual({ name: 'Claude', subtext: null });
	});

	it('formats a one-line label for titles/aria', () => {
		expect(formatProviderDisplay({ name: 'OpenAI', subtext: 'API' })).toBe('OpenAI · API');
		expect(formatProviderDisplay({ name: 'Claude', subtext: null })).toBe('Claude');
	});
});

describe('getModelLabel — provider registry fallback', () => {
	it('resolves a provider-curated model id to its label after hydration, and falls back to the raw id for an unknown id', async () => {
		mocks.get.mockResolvedValueOnce({
			models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', context_window: 1_000_000 }],
			default: 'sonnet',
			legacy_map: {},
			providers: [
				{
					id: 'prov-1',
					name: 'OpenRouter',
					provider_type: 'openrouter',
					models: [{ id: 'openai/gpt-5.6', label: 'GPT-5.6' }],
					default_model: 'openai/gpt-5.6'
				}
			]
		});

		await hydrateModelRegistry();

		expect(getModelLabel('openai/gpt-5.6')).toBe('GPT-5.6');
		expect(getModelLabel('openai/unknown-model')).toBe('openai/unknown-model');
	});
});

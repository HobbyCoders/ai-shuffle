/**
 * The composer's default model is the server's (`/api/models` `default`)
 * while its provider can run. An older worker that names none, or a server
 * default whose provider is not ready, falls back to the local choice, which
 * follows which providers can run (`agent_gate.ready_providers`).
 */
import { describe, it, expect, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));

import {
	getDefaultModel, getServerDefaultModel, hydrateModelRegistry, resolveDefaultModel, setReadyProviders,
	type ModelOption, type ProviderEntry
} from './models.svelte';

const claudeModels: ModelOption[] = [
	{ value: 'haiku', label: 'Haiku', family: 'haiku' },
	{ value: 'sonnet', label: 'Sonnet', family: 'sonnet' },
];
const entry = (over: Partial<ProviderEntry> & Pick<ProviderEntry, 'id' | 'provider_type'>): ProviderEntry => ({
	name: over.id, models: [], default_model: null, auth_kind: 'api', plan_type: null, ...over,
});
const openaiKey = entry({ id: 'oa-key', provider_type: 'openai', models: [{ id: 'gpt-5.5', label: 'GPT-5.5' }] });
const chatgpt = entry({ id: 'oa-sub', provider_type: 'chatgpt', auth_kind: 'subscription', models: [{ id: 'gpt-5.5-codex', label: 'Codex' }], default_model: 'gpt-5.5-codex' });
const gateway = entry({ id: 'gw1', provider_type: 'anthropic', models: [{ id: 'my-model', label: 'Mine' }] });

describe('resolveDefaultModel', () => {
	it('keeps the server default when readiness is unknown or nothing is ready', () => {
		expect(resolveDefaultModel('sonnet', null, [openaiKey], claudeModels)).toBe('sonnet');
		expect(resolveDefaultModel('sonnet', [], [openaiKey], claudeModels)).toBe('sonnet');
	});

	it('keeps a Claude default while Claude is ready', () => {
		expect(resolveDefaultModel('sonnet', ['claude', 'openai'], [openaiKey], claudeModels)).toBe('sonnet');
	});

	it('picks a ready provider when only OpenAI is connected and the server still says Claude', () => {
		expect(resolveDefaultModel('sonnet', ['openai'], [openaiKey], claudeModels)).toBe('prov:oa-key:gpt-5.5');
		// A subscription row comes first, with its own default model.
		expect(resolveDefaultModel('sonnet', ['openai'], [openaiKey, chatgpt], claudeModels)).toBe('prov:oa-sub:gpt-5.5-codex');
	});

	it('uses a composite server default as is when its provider is ready', () => {
		expect(resolveDefaultModel('prov:oa-key:gpt-5.5', ['openai'], [openaiKey], claudeModels)).toBe('prov:oa-key:gpt-5.5');
		expect(resolveDefaultModel('prov:gw1:my-model', ['gw:gw1'], [gateway], claudeModels)).toBe('prov:gw1:my-model');
	});

	it("uses a row's default only when it lists it", () => {
		const stale = entry({ id: 'gw2', provider_type: 'custom', models: [{ id: 'a', label: 'A' }], default_model: 'gone' });
		expect(resolveDefaultModel('sonnet', ['gw:gw2'], [stale], claudeModels)).toBe('prov:gw2:a');
	});

	it('tries provider rows before Claude, as the worker does', () => {
		expect(resolveDefaultModel('prov:gone:x', ['claude', 'openai'], [openaiKey], claudeModels)).toBe('prov:oa-key:gpt-5.5');
		expect(resolveDefaultModel('prov:gone:x', ['claude'], [openaiKey], claudeModels)).toBe('sonnet');
	});

	it('moves off a composite default whose row the worker marks unusable', () => {
		const dead = { ...openaiKey, usable: false };
		expect(resolveDefaultModel('prov:oa-key:gpt-5.5', ['claude'], [dead], claudeModels)).toBe('sonnet');
		expect(resolveDefaultModel('prov:oa-key:gpt-5.5', ['gw:gw1'], [dead, gateway], claudeModels)).toBe('prov:gw1:my-model');
	});

	it("trusts a composite default whose row exists even when its brand is not in ready_providers", () => {
		expect(resolveDefaultModel('prov:oa-key:gpt-5.5', ['claude'], [openaiKey], claudeModels)).toBe('prov:oa-key:gpt-5.5');
		expect(resolveDefaultModel('prov:oa-key:gpt-5.5', ['gw:gw1'], [openaiKey, gateway], claudeModels)).toBe('prov:oa-key:gpt-5.5');
	});

	it('the local fallback skips rows the worker marks unusable', () => {
		const reauth = { ...chatgpt, usable: false };
		expect(resolveDefaultModel('sonnet', ['openai'], [openaiKey, reauth], claudeModels)).toBe('prov:oa-key:gpt-5.5');
	});

	// An older ChatGPT/Codex sign-in is a `custom` row with auth_mode 'chatgpt';
	// the worker counts it as `openai` (credential_gate.provider_id_for_row).
	const legacyCodex = entry({
		id: 'codex-old', provider_type: 'custom', auth_mode: 'chatgpt', auth_kind: 'subscription',
		models: [{ id: 'gpt-5.5-codex', label: 'Codex' }], default_model: 'gpt-5.5-codex',
	});

	it('keeps the server default on a legacy custom+chatgpt row next to an API-key row', () => {
		expect(resolveDefaultModel('prov:codex-old:gpt-5.5-codex', ['openai'], [legacyCodex, openaiKey], claudeModels))
			.toBe('prov:codex-old:gpt-5.5-codex');
		// Locally too, the legacy row is the OpenAI subscription and comes first.
		expect(resolveDefaultModel('sonnet', ['openai'], [openaiKey, legacyCodex], claudeModels))
			.toBe('prov:codex-old:gpt-5.5-codex');
	});

	it('keeps the most used legacy Codex default when Claude is also ready', () => {
		expect(resolveDefaultModel('prov:codex-old:gpt-5.5-codex', ['claude', 'openai'], [legacyCodex], claudeModels))
			.toBe('prov:codex-old:gpt-5.5-codex');
	});

	it('maps a legacy custom+grok row to grok', () => {
		const legacyGrok = entry({ id: 'grok-old', provider_type: 'custom', auth_mode: 'grok', models: [{ id: 'grok-5', label: 'Grok 5' }] });
		expect(resolveDefaultModel('sonnet', ['grok'], [legacyGrok], claudeModels)).toBe('prov:grok-old:grok-5');
	});
});

describe('getDefaultModel', () => {
	const openaiRow = { id: 'oa-key', name: 'OpenAI', provider_type: 'openai', models: [{ id: 'gpt-5.5', label: 'GPT-5.5' }], default_model: 'gpt-5.5' };

	it("uses the server's default while its provider is ready: it is what the worker runs", async () => {
		mocks.get.mockResolvedValueOnce({ models: claudeModels, default: 'prov:oa-key:gpt-5.5', legacy_map: {}, providers: [openaiRow] });
		await hydrateModelRegistry();
		setReadyProviders(['claude', 'openai']);
		expect(getDefaultModel()).toBe('prov:oa-key:gpt-5.5');
		setReadyProviders(null); // readiness unknown: the server's default
		expect(getDefaultModel()).toBe('prov:oa-key:gpt-5.5');
		expect(getServerDefaultModel()).toBe('prov:oa-key:gpt-5.5');
	});

	it("falls back to a ready provider when the server's default provider is not ready", async () => {
		// A gateway-only owner on a worker that still names Claude.
		const gwRow = { id: 'gw1', name: 'My gateway', provider_type: 'custom', models: [{ id: 'llama-3', label: 'Llama 3' }], default_model: 'llama-3' };
		mocks.get.mockResolvedValueOnce({ models: claudeModels, default: 'sonnet', legacy_map: {}, providers: [gwRow] });
		await hydrateModelRegistry();
		setReadyProviders(['gw:gw1']);
		expect(getDefaultModel()).toBe('prov:gw1:llama-3');
		expect(getServerDefaultModel()).toBe('sonnet');
		setReadyProviders(null);
	});

	it("keeps the server's legacy custom+chatgpt default sent without a brand match", async () => {
		const legacyRow = { id: 'codex-old', name: 'Codex', provider_type: 'custom', auth_mode: 'chatgpt', auth_kind: 'subscription',
			models: [{ id: 'gpt-5.5-codex', label: 'Codex' }], default_model: 'gpt-5.5-codex', usable: true };
		mocks.get.mockResolvedValueOnce({ models: claudeModels, default: 'prov:codex-old:gpt-5.5-codex', legacy_map: {}, providers: [legacyRow, openaiRow] });
		await hydrateModelRegistry();
		setReadyProviders(['openai']);
		expect(getDefaultModel()).toBe('prov:codex-old:gpt-5.5-codex');
		setReadyProviders(['claude', 'openai']);
		expect(getDefaultModel()).toBe('prov:codex-old:gpt-5.5-codex');
		setReadyProviders(null);
	});

	it('computes a default from the ready providers only when the server gives none (older worker)', async () => {
		mocks.get.mockResolvedValueOnce({ models: claudeModels, legacy_map: {}, providers: [openaiRow] });
		await hydrateModelRegistry();
		setReadyProviders(['openai']);
		expect(getDefaultModel()).toBe('prov:oa-key:gpt-5.5');
		setReadyProviders(['claude', 'openai']);
		expect(getDefaultModel()).toBe('sonnet');
		setReadyProviders(null);
	});
});

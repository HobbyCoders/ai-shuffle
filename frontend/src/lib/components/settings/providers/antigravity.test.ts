import { describe, expect, it } from 'vitest';
import {
	addableKinds,
	ANTIGRAVITY_GUARD_KEY,
	antigravityGuarded,
	buildProviders,
	classifyRow,
	flavorOfRow,
	providerOfRow,
	type ProvidersInput
} from './providers-model';
import { claudeSummary, row } from './fixtures';
import { windowLabel } from '$lib/stores/rate-limit';
import { providerGlyphKind } from '../ProviderGlyph.svelte';

const agy = (auth_mode: string, extra_env: Record<string, string> = {}) =>
	row({
		id: `agy-${auth_mode}`,
		name: '',
		provider_type: 'antigravity',
		auth_mode,
		base_url: '',
		auth_token_set: auth_mode === 'gemini-api-key',
		auth_token_masked: auth_mode === 'gemini-api-key' ? 'AIza…test' : '',
		models: [{ id: 'gemini-3.1-pro', label: 'Gemini 3.1 Pro' }],
		extra_env
	});

const input = (rows: ProvidersInput['rows']): ProvidersInput => ({
	rows,
	claude: claudeSummary(),
	fal: { keySet: false, masked: '', enabled: true }
});

describe('Google Antigravity rows', () => {
	it('sit under their own provider, one kind per auth mode', () => {
		const key = agy('gemini-api-key');
		expect(classifyRow(key)).toBe('antigravity');
		expect(providerOfRow(key)).toBe('antigravity');
		expect(flavorOfRow(key)).toBe('agy-key');
		expect(flavorOfRow(agy('oauth-business'))).toBe('agy-business');
		expect(flavorOfRow(agy('oauth-personal'))).toBe('agy-personal');
		expect(providerGlyphKind('antigravity')).toBe('antigravity');
	});

	it('offer the API key first and the personal sign-in last', () => {
		const views = buildProviders(input([]));
		const provider = views.find((p) => p.id === 'antigravity')!;
		expect(provider.name).toBe('Google Antigravity');
		expect(addableKinds(provider).map((k) => k.flavor)).toEqual(['agy-key', 'agy-business', 'agy-personal']);
	});

	it('mark a personal sign-in interactive only until the owner lifts it', () => {
		const personal = agy('oauth-personal');
		expect(antigravityGuarded(personal)).toBe(true);
		expect(antigravityGuarded(agy('oauth-personal', { [ANTIGRAVITY_GUARD_KEY]: '1' }))).toBe(false);
		expect(antigravityGuarded(agy('gemini-api-key'))).toBe(false);
		expect(antigravityGuarded(agy('oauth-business'))).toBe(false);

		const views = buildProviders(input([personal, agy('gemini-api-key')]));
		const creds = views.find((p) => p.id === 'antigravity')!.credentials;
		const guarded = creds.find((c) => c.flavor === 'agy-personal')!;
		expect(guarded.name).toBe('Personal Google sign-in');
		expect(guarded.summary).toContain('interactive only');
		const key = creds.find((c) => c.flavor === 'agy-key')!;
		expect(key.kind).toBe('API key');
		expect(key.summary).not.toContain('interactive only');
	});

	it('label per-model quota windows from the agent', () => {
		expect(windowLabel('antigravity_gemini_3_1_pro')).toBe('Gemini 3 1 pro');
	});
});

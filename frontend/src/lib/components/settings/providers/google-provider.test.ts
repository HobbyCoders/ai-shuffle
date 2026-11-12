/** The Google Gemini API-key row reuses the native key screens: tile, key setup, check and curation. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ hydrate: vi.fn(), checkAuth: vi.fn() }));
vi.mock('$lib/constants/models.svelte', async (original) => ({
	...(await original<typeof import('$lib/constants/models.svelte')>()), hydrateModelRegistry: mocks.hydrate
}));
vi.mock('$lib/stores/auth.svelte', async (original) => ({
	...(await original<typeof import('$lib/stores/auth.svelte')>()), auth: { checkAuth: mocks.checkAuth }
}));

import { providers } from '$lib/stores/providers.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { providerDrafts } from './drafts.svelte';
import { buildProviders, kindsOf, providerOfRow, rowCredential } from './providers-model';
import { nativeOptions } from './native-options.svelte';
import { fakeWorker } from './fake-worker';
import { googleEnrollment, nativeEnrollment, nativeRow } from './fixtures';
import { choiceOf, setupProviders, setupReadiness } from '../../setup/provider-readiness';
import { getProviderDisplay, providerOfModel } from '$lib/constants/models.svelte';
import ProvidersCollection from './ProvidersCollection.svelte';
import KeySetup from './setup/KeySetup.svelte';
import ProviderConnect from '../../setup/ProviderConnect.svelte';

const FAL = { keySet: false, masked: '', enabled: false };
let worker: ReturnType<typeof fakeWorker>;

beforeEach(() => {
	vi.clearAllMocks();
	mocks.hydrate.mockResolvedValue(true);
	mocks.checkAuth.mockResolvedValue(undefined);
	providers.clear();
	providerDrafts.list = [];
	providerDrafts.gateways = [];
	settingsNav.reset();
	sessionStorage.clear();
	nativeOptions.providers = [...nativeEnrollment, googleEnrollment];
	nativeOptions.loaded = true;
	nativeOptions.loading = false;
	nativeOptions.error = '';
	worker = fakeWorker({ rows: [] });
	vi.stubGlobal('fetch', worker.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('is a built-in API-key provider with one credential kind', () => {
	const row = nativeRow(googleEnrollment, { models: [{ id: 'gemini-3.8-flash', label: 'Gemini 3.8 Flash' }] });
	const views = buildProviders({ rows: [row], claude: null, fal: FAL });
	const view = views.find((v) => v.id === 'google');
	expect(view?.name).toBe('Google Gemini');
	expect(providerOfRow(row)).toBe('google');
	expect(rowCredential(row)).toMatchObject({ flavor: 'key', kind: 'Google AI Studio API key', on: true });
	expect(kindsOf('google').map((k) => k.label)).toEqual(['Google AI Studio API key']);
	expect(choiceOf('google')).toBe('google');
	expect(setupProviders().find((p) => p.id === 'google')).toMatchObject({ name: 'Google Gemini', blurb: 'Google AI Studio API key' });
	expect(setupReadiness(undefined, views, [row]).ready).toContain('google');
	expect(providerOfModel(`prov:${row.id}:gemini-3.8-flash`, [{ id: row.id, provider_type: 'google' } as never])).toBe('google');
	expect(getProviderDisplay({ provider_type: 'google' }).name).toBe('Google Gemini');
});

it('hides the tile when the worker does not offer Google, unless a row is already saved', () => {
	nativeOptions.providers = nativeEnrollment;
	expect(buildProviders({ rows: [], claude: null, fal: FAL }).some((v) => v.id === 'google')).toBe(false);
	expect(setupProviders().some((p) => p.id === 'google')).toBe(false);
	const views = buildProviders({ rows: [nativeRow(googleEnrollment)], claude: null, fal: FAL });
	expect(views.some((v) => v.id === 'google')).toBe(true);
});

it('creates the row with only name, type and key, then opens model curation', async () => {
	worker = fakeWorker({}, { 'POST /model-providers': () => {
		const saved = nativeRow(googleEnrollment);
		worker.state.rows.push(saved);
		return saved;
	} });
	vi.stubGlobal('fetch', worker.fetch);
	const draft = providerDrafts.add('google', 'key', []);
	render(KeySetup, { draft, onFinish: vi.fn(), onCancel: vi.fn() });
	expect(screen.getByRole('link', { name: 'Google AI Studio API key ↗' }).getAttribute('href')).toBe(googleEnrollment.key_page_url);
	await fireEvent.input(screen.getByLabelText('Google AI Studio API key'), { target: { value: 'fake-google-ui-key' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
	await waitFor(() => expect(worker.sent('POST', '/model-providers')).toHaveLength(1));
	expect(worker.sent('POST', '/model-providers')[0].body).toEqual({
		name: 'Google AI Studio API key', provider_type: 'google', auth_token: 'fake-google-ui-key'
	});
	await waitFor(() => expect(providerDrafts.get(draft.item)?.step).toBe('models'));
});

it('checks the saved key and offers no usage tab', async () => {
	const row = nativeRow(googleEnrollment);
	worker = fakeWorker({ rows: [row] }, { 'POST /model-providers/:id/check-key': () => ({ status: 'valid' }) });
	vi.stubGlobal('fetch', worker.fetch);
	await providers.refreshAll();
	settingsNav.go({ category: 'providers', item: `mp:${row.id}` });
	render(ProvidersCollection);
	await fireEvent.click(await screen.findByRole('button', { name: 'Check key' }));
	await screen.findByText('Key is valid');
	expect(screen.queryByRole('tab', { name: 'Usage' })).toBeNull();
	expect(worker.sent('POST', `/model-providers/${row.id}/check-key`)).toHaveLength(1);
});

it('first-run setup goes straight to the key form and finishes after a model is picked', async () => {
	const onConnected = vi.fn();
	const saved = nativeRow(googleEnrollment, { models: [], catalog_binding: 'google-key-binding' });
	const catalog = [{ id: 'gemini-3.8-flash', label: 'Gemini 3.8 Flash' }];
	worker = fakeWorker({}, {
		'POST /model-providers': () => { worker.state.rows.push(saved); return saved; },
		'POST /model-providers/:id/fetch-models': () => ({ models: catalog, source: 'upstream', binding: saved.catalog_binding })
	});
	vi.stubGlobal('fetch', worker.fetch);
	render(ProviderConnect, { provider: 'google', connected: true, onConnected });
	await fireEvent.input(await screen.findByLabelText('Google AI Studio API key'), { target: { value: 'fake-google-ui-key' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
	await fireEvent.click(await screen.findByRole('checkbox', { name: /Gemini 3.8 Flash/ }));
	const useSelected = await screen.findByRole('button', { name: 'Use selected (1)' });
	await waitFor(() => expect((useSelected as HTMLButtonElement).disabled).toBe(false));
	await fireEvent.click(useSelected);
	await waitFor(() => expect(onConnected).toHaveBeenCalledWith('google'));
	expect(worker.calls.filter((c) => /device/.test(c.path))).toEqual([]);
});

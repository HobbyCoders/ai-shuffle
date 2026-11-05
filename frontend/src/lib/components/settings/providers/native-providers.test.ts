/** Native key enrollment uses the worker contract, never a device sign-in or a billing meter. */
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
import { buildProviders, kindsOf, rowCredential, providerOfRow } from './providers-model';
import { loadNativeOptions, nativeOptions } from './native-options.svelte';
import { fakeWorker } from './fake-worker';
import { nativeEnrollment, nativeRow } from './fixtures';
import { choiceOf, setupProviders, setupReadiness } from '../../setup/provider-readiness';
import { getModelLabel, getProviderDisplay, providerOfModel } from '$lib/constants/models.svelte';
import { searchSource } from '$lib/settings/entries/providers';
import ProvidersCollection from './ProvidersCollection.svelte';
import KeySetup from './setup/KeySetup.svelte';
import ProviderConnect from '../../setup/ProviderConnect.svelte';

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
	nativeOptions.providers = nativeEnrollment;
	nativeOptions.loaded = true;
	nativeOptions.loading = false;
	nativeOptions.error = '';
	worker = fakeWorker({ rows: [] });
	vi.stubGlobal('fetch', worker.fetch);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('loads the six options once, including simultaneous consumers', async () => {
	nativeOptions.loaded = false;
	nativeOptions.providers = [];
	await Promise.all([loadNativeOptions(), loadNativeOptions()]);
	await loadNativeOptions();
	expect(worker.sent('GET', '/model-providers/native-options')).toHaveLength(1);
	expect(nativeOptions.providers).toHaveLength(6);
});

it.each(nativeEnrollment)('$credential_label is a pasted key with a metadata link and correct create payload', async (option) => {
	worker = fakeWorker({}, { 'POST /model-providers': () => {
		const saved = nativeRow(option);
		worker.state.rows.push(saved);
		return saved;
	} });
	vi.stubGlobal('fetch', worker.fetch);
	const draft = providerDrafts.add(option.provider_type, option.auth_mode ? 'plan-key' : 'key', []);
	render(KeySetup, { draft, onFinish: vi.fn(), onCancel: vi.fn() });
	expect(screen.getByRole('link', { name: `${option.credential_label} ↗` }).getAttribute('href')).toBe(option.key_page_url);
	await fireEvent.input(screen.getByLabelText(option.credential_label), { target: { value: 'test-key-with-no-required-prefix' } });
	await fireEvent.input(screen.getByLabelText('Name'), { target: { value: 'Work account' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
	await waitFor(() => expect(worker.sent('POST', '/model-providers')).toHaveLength(1));
	const body = worker.sent('POST', '/model-providers')[0].body as Record<string, unknown>;
	expect(body).toEqual({ name: 'Work account', provider_type: option.provider_type, auth_token: 'test-key-with-no-required-prefix', ...(option.auth_mode ? { auth_mode: 'plan_key' } : {}) });
	if (!option.auth_mode) expect(body).not.toHaveProperty('auth_mode');
	expect(screen.queryByText(/sign in|device code/i)).toBeNull();
});

it.each([
	[400, 'The vendor rejected this key. Nothing was saved.'],
	[409, 'Update GamingPC and HomeLab before adding this provider.'],
	[503, 'Could not check the workers. Nothing was created. Try again.']
])('shows the exact error detail for HTTP %s and allows a retry', async (status, detail) => {
	worker = fakeWorker({}, { 'POST /model-providers': () => { throw { status, detail }; } });
	vi.stubGlobal('fetch', worker.fetch);
	const draft = providerDrafts.add('minimax', 'plan-key', []);
	render(KeySetup, { draft, onFinish: vi.fn(), onCancel: vi.fn() });
	await fireEvent.input(screen.getByLabelText('MiniMax M Plan'), { target: { value: 'fake-key' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
	expect((await screen.findByRole('alert')).textContent).toBe(detail);
	expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false);
	expect(worker.state.rows).toEqual([]);
	expect(providerDrafts.get(draft.item)?.rowId).toBeNull();
});

it.each(nativeEnrollment)('$credential_label stays built-in and connected, with a friendly model fallback', (option) => {
	const row = nativeRow(option);
	const views = buildProviders({ rows: [row], claude: null, fal: { keySet: false, masked: '', enabled: false } });
	expect(providerOfRow(row)).toBe(option.provider_type);
	expect(rowCredential(row)).toMatchObject({ flavor: option.auth_mode ? 'plan-key' : 'key', kind: option.credential_label, on: true });
	expect(kindsOf(option.provider_type).map((k) => k.label)).toHaveLength(2);
	expect(choiceOf(option.provider_type)).toBe(option.provider_type);
	expect(setupProviders().find((p) => p.id === option.provider_type)?.name).toBe(option.display_name);
	expect(setupReadiness(undefined, views, [row]).ready).toContain(option.provider_type);
	expect(providerOfModel(`prov:${row.id}:model`, [{ id: row.id, provider_type: row.provider_type } as never])).toBe(option.provider_type);
	expect(getProviderDisplay({ provider_type: row.provider_type }).name).toBe(option.display_name);
	expect(getModelLabel('prov:missing:model-name')).toBe('model-name');
});

it.each(nativeEnrollment)('$credential_label has no usage tab or meter, and an indeterminate check is not valid or failed', async (option) => {
	let status = 'unavailable';
	worker = fakeWorker({ rows: [nativeRow(option)] }, { 'POST /model-providers/:id/check-key': () => ({ status }) });
	vi.stubGlobal('fetch', worker.fetch);
	await providers.refreshAll();
	settingsNav.go({ category: 'providers', item: `mp:${nativeRow(option).id}` });
	render(ProvidersCollection);
	await screen.findByRole('button', { name: 'Check key' });
	expect(screen.queryByRole('tab', { name: 'Usage' })).toBeNull();
	expect(screen.queryByRole('meter')).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: 'Check key' }));
	await screen.findByText('Saved, but unverified. Try checking again later.');
	expect(screen.queryByText('Key is valid')).toBeNull();
	expect(screen.queryByRole('alert')).toBeNull();
	const entries = searchSource().filter((e) => e.target?.item === `mp:${nativeRow(option).id}`);
	expect(entries.map((e) => e.title)).toEqual(expect.arrayContaining(['Model curation', 'Connection']));
	status = 'valid';
	await fireEvent.click(screen.getByRole('button', { name: 'Check key' }));
	await screen.findByText('Key is valid');
});

it.each(['kimi', 'minimax', 'opencode'] as const)('%s switches only the selected credential slot', async (provider) => {
	const planOption = nativeEnrollment.find((p) => p.provider_type === provider && p.auth_mode === 'plan_key')!;
	const apiOption = nativeEnrollment.find((p) => p.provider_type === provider && p.auth_mode === null)!;
	const plan = { ...nativeRow(planOption), id: `${provider}-plan`, name: 'Plan one' };
	const sibling = { ...plan, id: `${provider}-plan-two`, name: 'Plan two', enabled: false };
	const api = { ...nativeRow(apiOption), id: `${provider}-api`, name: 'Pay as you go' };
	worker = fakeWorker({ rows: [plan, sibling, api] });
	vi.stubGlobal('fetch', worker.fetch);
	await providers.refreshAll();
	render(ProvidersCollection);
	await fireEvent.click(await screen.findByRole('switch', { name: 'Use Plan two' }));
	await waitFor(() => expect(worker.state.rows.find((r) => r.id === sibling.id)?.enabled).toBe(true));
	expect(worker.state.rows.find((r) => r.id === plan.id)?.enabled).toBe(false);
	expect(worker.state.rows.find((r) => r.id === api.id)?.enabled).toBe(true);
	expect(worker.sent('POST', `/model-providers/${sibling.id}/activate`)).toHaveLength(1);
	await fireEvent.click(await screen.findByRole('switch', { name: 'Plan two on' }));
	await waitFor(() => expect(worker.state.rows.find((r) => r.id === sibling.id)?.enabled).toBe(false));
	expect(worker.state.rows.find((r) => r.id === api.id)?.enabled).toBe(true);
});

it('metadata edits do not revalidate a native endpoint, and a rejected replacement preserves the saved key', async () => {
	const saved = nativeRow(nativeEnrollment[0]);
	worker = fakeWorker({ rows: [saved] }, { 'PATCH /model-providers/:id': (value) => {
		const body = value as Record<string, unknown>;
		if (body.auth_token) throw { status: 400, detail: 'Replacement rejected. Your saved key is unchanged.' };
		Object.assign(worker.state.rows[0], body);
		return worker.state.rows[0];
	} });
	vi.stubGlobal('fetch', worker.fetch);
	await providers.refreshAll();
	settingsNav.go({ category: 'providers', item: `mp:${saved.id}` });
	render(ProvidersCollection);
	await providers.refreshAll();
	const name = await screen.findByRole('textbox', { name: 'Name' });
	await fireEvent.focus(name);
	await fireEvent.input(name, { target: { value: 'Renamed' } });
	await fireEvent.blur(name);
	await waitFor(() => expect(worker.sent('PATCH', `/model-providers/${saved.id}`)).toHaveLength(1));
	expect(worker.sent('PATCH', `/model-providers/${saved.id}`)[0].body).toEqual({ name: 'Renamed', provider_type: 'kimi' });
	await waitFor(() => expect((screen.getByRole('button', { name: 'Replace' }) as HTMLButtonElement).disabled).toBe(false));
	await fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
	await fireEvent.input(screen.getByLabelText('New auth token'), { target: { value: 'rejected-fake-key' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
	expect((await screen.findByRole('alert')).textContent).toBe('Replacement rejected. Your saved key is unchanged.');
	expect(worker.state.rows[0].auth_token_set).toBe(true);
	expect(worker.state.rows[0].auth_token_masked).toBe('********');
});

it('uses Kimi Code’s preset without offering or attempting a model refresh', async () => {
	const option = nativeEnrollment[0];
	worker = fakeWorker({ rows: [nativeRow(option)] });
	vi.stubGlobal('fetch', worker.fetch);
	await providers.refreshAll();
	settingsNav.go({ category: 'providers', item: `mp:${nativeRow(option).id}`, tab: 'Models' });
	render(ProvidersCollection);
	await screen.findByText('Kimi Model');
	expect(screen.queryByRole('button', { name: 'Refresh model list' })).toBeNull();
	expect(worker.calls.filter((c) => /fetch-models|model-catalog/.test(c.path))).toEqual([]);
});

it('ProviderConnect offers both pasted-key kinds and completes Kimi Code from the preset', async () => {
	const onConnected = vi.fn();
	worker = fakeWorker({}, { 'POST /model-providers': () => {
		const saved = nativeRow(nativeEnrollment[0]); worker.state.rows.push(saved); return saved;
	} });
	vi.stubGlobal('fetch', worker.fetch);
	render(ProviderConnect, { provider: 'kimi', connected: true, onConnected });
	await fireEvent.click(screen.getByRole('button', { name: /Kimi Code membership/ }));
	await fireEvent.input(screen.getByLabelText('Kimi Code membership'), { target: { value: 'fake-plan' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
	await fireEvent.click(await screen.findByRole('button', { name: /Use selected/ }));
	await waitFor(() => expect(onConnected).toHaveBeenCalledWith('kimi'));
	expect(worker.calls.filter((c) => /device|fetch-models/.test(c.path))).toEqual([]);
});

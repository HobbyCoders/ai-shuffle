/**
 * The fal.ai Media key page's balance: written as the chat's usage popover
 * writes it (`-$2.50`, sign first), in the warning colour at zero or below
 * and the neutral value colour otherwise (#1583).
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ hydrate: vi.fn(), checkAuth: vi.fn() }));
vi.mock('$lib/constants/models.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/constants/models.svelte')>()),
	hydrateModelRegistry: mocks.hydrate
}));
// Each case reads its own balance; the app reuses a usage answer for a few seconds.
vi.mock('$lib/api/client', async (importOriginal) => {
	const client = await importOriginal<typeof import('$lib/api/client')>();
	return { ...client, getUsageLimits: () => client.api.get('/usage/limits') };
});
vi.mock('$lib/stores/auth.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/auth.svelte')>()),
	auth: { checkAuth: mocks.checkAuth }
}));

import { providers } from '$lib/stores/providers.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import ProvidersCollection from './ProvidersCollection.svelte';
import { providerDrafts } from './drafts.svelte';
import { fakeWorker } from './fake-worker';
import { FAL_KEY_ITEM } from './providers-model';

async function flush(times = 6) {
	for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0));
}

async function showBalance(balance_remaining: number) {
	const worker = fakeWorker(
		{},
		{
			'GET /usage/limits': () => ({
				providers: [{ provider_id: 'fal-ai', credits: { balance_remaining }, fetched_at: null }]
			})
		}
	);
	vi.stubGlobal('fetch', worker.fetch);
	await providers.refreshAll();
	settingsNav.go({ category: 'providers', item: FAL_KEY_ITEM });
	render(ProvidersCollection);
	await flush();
}

beforeEach(() => {
	vi.clearAllMocks();
	mocks.hydrate.mockResolvedValue(true);
	mocks.checkAuth.mockResolvedValue(undefined);
	providers.clear();
	providerDrafts.list = [];
	providerDrafts.gateways = [];
	settingsNav.reset();
	sessionStorage.clear();
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

it.each([
	[-2.5, '-$2.50', true],
	[0, '$0.00', true],
	[12.4, '$12.40', false]
])('balance %s reads %s (warning colour: %s) in the Balance row and the subtitle', async (raw, text, danger) => {
	await showBalance(raw);
	const row = within(screen.getByRole('region', { name: 'Balance' })).getByText(text);
	expect(row.classList.contains('danger')).toBe(danger);
	const subtitle = document.querySelector('.st-dhs')!;
	expect(subtitle.textContent).toContain(text);
	expect(within(subtitle as HTMLElement).getByText(text).classList.contains('danger')).toBe(danger);
	expect(document.body.textContent).not.toContain('$-');
});

/**
 * A stored credential never reaches the page: not in text, not in an input's
 * value or placeholder, not in a title or aria-label. Every provider detail is
 * rendered against a worker that answers with ciphertext in each masked field,
 * as workers older than the masking fix do.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ hydrate: vi.fn(), checkAuth: vi.fn() }));
vi.mock('$lib/constants/models.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/constants/models.svelte')>()),
	hydrateModelRegistry: mocks.hydrate
}));
vi.mock('$lib/stores/auth.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/auth.svelte')>()),
	auth: { checkAuth: mocks.checkAuth }
}));

import { providers } from '$lib/stores/providers.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import ProvidersCollection from './ProvidersCollection.svelte';
import { claudeKeyItem } from './providers-model';
import { providerDrafts } from './drafts.svelte';
import { safeKeyHint } from './credential-display';
import { claudeAccounts, claudeSummary, gatewayRow, row } from './fixtures';
import { fakeWorker } from './fake-worker';

const CIPHERTEXT = 'enc:Z0FBQUFBQm1hc2tlZFNlY3JldFZhbHVlU0VOVElORUxU289';
/** The old mask: the first 7 and last 4 characters of the stored blob. */
const OLD_MASK = `${CIPHERTEXT.slice(0, 7)}...${CIPHERTEXT.slice(-4)}`;
const FRAGMENTS = ['enc:', 'Z0F', 'gAAAA', 'U289'];

function page(): string {
	const attrs = [...document.querySelectorAll('*')].flatMap((el) => [
		...[...el.attributes].map((a) => a.value),
		el instanceof HTMLInputElement ? el.value : ''
	]);
	return `${document.body.textContent}\n${attrs.join('\n')}`;
}

function expectNoCiphertext() {
	const text = page();
	for (const fragment of FRAGMENTS) expect(text).not.toContain(fragment);
}

function start() {
	const worker = fakeWorker({
		rows: [
			row({ auth_token_set: true, auth_token_masked: OLD_MASK }),
			gatewayRow({ auth_token_set: true, auth_token_masked: CIPHERTEXT })
		],
		claude: claudeSummary({
			api_key: { set: true, last4: null },
			accounts: claudeAccounts({
				api_key: [{ id: 'k1', label: 'Work', active: true, masked_key: OLD_MASK, last4: null }]
			})
		})
	});
	worker.state.integrations.anthropic_api_key_set = true;
	worker.state.integrations.anthropic_api_key_masked = OLD_MASK;
	worker.state.integrations.fal_api_key_set = true;
	worker.state.integrations.fal_api_key_masked = OLD_MASK;
	vi.stubGlobal('fetch', worker.fetch);
}

async function flush() {
	for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0));
}

beforeEach(() => {
	mocks.hydrate.mockResolvedValue(true);
	mocks.checkAuth.mockResolvedValue(undefined);
	providers.clear();
	providerDrafts.list = [];
	providerDrafts.gateways = [];
	settingsNav.reset();
	sessionStorage.clear();
	start();
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

describe('stored credentials on the page', () => {
	it('the list shows no ciphertext', async () => {
		await providers.refreshAll();
		render(ProvidersCollection);
		await flush();
		expect(document.querySelector('[data-settings-item]')).not.toBeNull();
		expectNoCiphertext();
	});

	it('no provider or credential detail shows ciphertext', async () => {
		await providers.refreshAll();
		render(ProvidersCollection);
		await flush();
		const items = [...document.querySelectorAll('[data-settings-item]')].map((el) => el.getAttribute('data-settings-item') as string);
		// Each kind of stored key really is on the page, or this proves nothing.
		expect(items).toEqual(expect.arrayContaining(['mp:p1', 'mp:gw-1', claudeKeyItem('k1'), 'fal:key']));
		for (const item of items) {
			settingsNav.go({ category: 'providers', item });
			await flush();
			expectNoCiphertext();
		}
	});

	it('a stored token reads Saved with Replace, and Replace opens a blank field', async () => {
		await providers.refreshAll();
		settingsNav.go({ category: 'providers', item: 'mp:p1' });
		render(ProvidersCollection);
		await flush();
		expect(screen.getAllByText('Saved').length).toBeGreaterThan(0);
		await fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
		const field = screen.getByLabelText('New auth token') as HTMLInputElement;
		expect(field.value).toBe('');
		expect(field.type).toBe('password');
		expectNoCiphertext();
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(screen.getByRole('button', { name: 'Replace' })).toBeTruthy();
	});
});

describe('safeKeyHint', () => {
	it.each([
		[CIPHERTEXT, ''],
		[OLD_MASK, ''],
		['gAAAAABmabc...wxyz', ''],
		['Z0FBQUFB...U289', ''],
		[null, ''],
		[undefined, ''],
		['', ''],
		['sk-ant-...abcd', 'sk-ant-...abcd']
	])('%s becomes %j', (input, output) => {
		expect(safeKeyHint(input)).toBe(output);
	});
});

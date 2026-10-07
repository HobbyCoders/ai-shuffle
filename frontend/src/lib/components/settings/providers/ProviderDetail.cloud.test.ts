/**
 * Settings > Providers > Claude: the opt-in "Cloud provider or gateway"
 * method (#1862). Off by default, reached only by an explicit pick, the
 * warning shown and acknowledged before saving or turning it on, secrets
 * never shown back, and removing it deletes what was stored.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ hydrate: vi.fn(), checkAuth: vi.fn() }));
vi.mock('$lib/constants/models.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/constants/models.svelte')>()),
	hydrateModelRegistry: mocks.hydrate
}));
vi.mock('$lib/stores/auth.svelte', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/auth.svelte')>()),
	auth: { checkAuth: mocks.checkAuth }
}));

import type { ClaudeCloudConfig, ClaudeCloudMethodId } from '$lib/api/claude-auth';
import { providers } from '$lib/stores/providers.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { SETTINGS_ACCESS_CONTEXT } from '$lib/settings/access.svelte';
import ProvidersCollection from './ProvidersCollection.svelte';
import { providerDrafts } from './drafts.svelte';
import { claudeSummary, cloudConfig } from './fixtures';
import { fakeWorker } from './fake-worker';
import { CLAUDE_CLOUD_ITEM } from './providers-model';

const TOKEN = 'fake-gateway-token-000';
const WARNING = /smallest permissions that work/;
const GATEWAY_WARNING = 'All Claude traffic from this worker goes to the address you enter. The browser tool also receives this address and token.';

let worker: ReturnType<typeof fakeWorker>;
let cloud: ClaudeCloudConfig;

async function flush(times = 6) {
	for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0));
}

async function show(item = 'provider:claude', options: { readOnly?: boolean } = {}) {
	await providers.refreshAll();
	settingsNav.go({ category: 'providers', item });
	const context = options.readOnly ? new Map([[SETTINGS_ACCESS_CONTEXT, { readOnly: true }]]) : undefined;
	render(ProvidersCollection, context ? { context } : {});
	await flush();
}

const modes = () => screen.getByRole('group', { name: 'Runs on' });

/** A gateway saved with its token stored (the worker never returns the token). */
function savedGateway(): ClaudeCloudConfig {
	return cloudConfig({
		method: 'gateway',
		values: { base_url: 'https://gw.example/v1' },
		secrets: { auth_token: { set: true } },
		status: {
			method: 'gateway',
			method_label: 'LLM gateway',
			configured: true,
			ready: true,
			missing: [],
			storage_locked: false
		}
	});
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
	cloud = cloudConfig();
	worker = fakeWorker(
		{ claude: claudeSummary({ cloud: cloud.status }) },
		{
			'GET /settings/claude-auth/cloud': () => cloud,
			'PUT /settings/claude-auth/cloud': (body) => {
				const b = body as { method: ClaudeCloudMethodId; values: Record<string, string>; activate: boolean };
				cloud = savedGateway();
				const mode = b.activate ? 'cloud' : worker.state.claude.mode;
				worker.state.claude = claudeSummary({
					...worker.state.claude,
					mode,
					mode_label: mode === 'cloud' ? 'Cloud provider or gateway' : worker.state.claude.mode_label,
					cloud: cloud.status,
					gate: { ready: true, mode, claude_credential_ready: true, reason: null, license_allows_runs: true }
				});
				return { ...cloud, auth: worker.state.claude };
			},
			'DELETE /settings/claude-auth/cloud': () => {
				cloud = cloudConfig();
				worker.state.claude = { ...worker.state.claude, cloud: cloud.status };
				return { success: true, auth: worker.state.claude };
			}
		}
	);
	vi.stubGlobal('fetch', worker.fetch);
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

it('is off by default: no cloud row, nothing read or sent', async () => {
	await show();
	expect(within(modes()).getByRole('button', { name: 'Cloud' }).getAttribute('aria-pressed')).toBe('false');
	const creds = screen.getByRole('region', { name: /^Credentials/ });
	expect(within(creds).queryByText(/Cloud provider|LLM gateway/)).toBeNull();
	expect(worker.sent('GET', '/settings/claude-auth/cloud')).toHaveLength(0);
	expect(worker.sent('PUT', '/settings/claude-auth/cloud')).toHaveLength(0);
	// It is offered as a kind the owner can add.
	expect(within(screen.getByRole('region', { name: 'Add' })).getByText('Cloud provider or gateway')).toBeTruthy();
});

it('picking Cloud opens the setup with the warning instead of switching the mode', async () => {
	await show();
	await fireEvent.click(within(modes()).getByRole('button', { name: 'Cloud' }));
	await flush();
	expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(0);
	expect(screen.queryByRole('group', { name: 'Runs on' })).toBeNull(); // now on the setup
	expect(screen.getByText(WARNING)).toBeTruthy();
	expect(screen.getByRole('button', { name: 'Save and use for Claude' })).toHaveProperty('disabled', true);
});

it('the Cloud pick in the provider list header opens the same setup and never sends the acknowledgement', async () => {
	await show();
	const header = screen.getByRole('group', { name: 'Claude runs on' });
	await fireEvent.click(within(header).getByRole('button', { name: 'Cloud' }));
	await flush();
	expect(worker.sent('PUT', '/settings/claude-auth')).toHaveLength(0);
	expect(worker.sent('PUT', '/settings/claude-auth/cloud')).toHaveLength(0);
	expect(within(header).getByRole('button', { name: 'Cloud' }).getAttribute('aria-pressed')).toBe('false');
	expect(screen.getByText(WARNING)).toBeTruthy();
	expect(screen.getByRole('button', { name: 'Save and use for Claude' })).toHaveProperty('disabled', true);
});

it('saves a gateway, then phone Back recovers the removed setup draft to the list', async () => {
	await show();
	settingsNav.stacked = true;
	await fireEvent.click(within(modes()).getByRole('button', { name: 'Cloud' }));
	await flush();
	// This is the history entry made by the actual setup producer.
	const setupItem = settingsNav.selectedItem('providers')!;
	await fireEvent.change(screen.getByLabelText('Cloud method'), { target: { value: 'gateway' } });
	await flush();
	expect(screen.getByText(GATEWAY_WARNING)).toBeTruthy();
	expect(screen.getByText('ANTHROPIC_BASE_URL')).toBeTruthy();
	await fireEvent.input(screen.getByLabelText('Base URL'), { target: { value: 'https://gw.example/v1' } });
	await fireEvent.input(screen.getByLabelText('Auth token'), { target: { value: TOKEN } });
	expect((screen.getByLabelText('Auth token') as HTMLInputElement).type).toBe('password');
	const useIt = screen.getByRole('button', { name: 'Save and use for Claude' });
	expect(useIt).toHaveProperty('disabled', true);

	await fireEvent.click(screen.getByRole('checkbox', { name: /read the warning/ }));
	await waitFor(() => expect(useIt).toHaveProperty('disabled', false));
	await fireEvent.click(useIt);
	await waitFor(() => expect(worker.sent('PUT', '/settings/claude-auth/cloud')).toHaveLength(1));
	expect(worker.sent('PUT', '/settings/claude-auth/cloud')[0].body).toEqual({
		method: 'gateway',
		values: { base_url: 'https://gw.example/v1', auth_token: TOKEN },
		acknowledge_warning: true,
		activate: true
	});

	await waitFor(() => expect(settingsNav.selectedItem('providers')).toBe(CLAUDE_CLOUD_ITEM));
	await flush();
	expect(screen.getByText(WARNING)).toBeTruthy();
	expect(screen.getByText(GATEWAY_WARNING)).toBeTruthy();
	expect((screen.getByLabelText('Auth token') as HTMLInputElement).value).toBe('');
	expect(screen.getByLabelText('Auth token').getAttribute('placeholder')).toBe('Stored. Leave blank to keep.');
	expect(document.body.textContent).not.toContain(TOKEN);
	expect(mocks.hydrate).toHaveBeenCalled();
	expect(providerDrafts.get(setupItem)).toBeUndefined();
	settingsNav.followHistory({ settingsView: 'detail', settingsDepth: 2, settingsCategory: 'providers', settingsItem: setupItem });
	await waitFor(() => expect(settingsNav.view).toBe('page'));
	expect(settingsNav.selectedItem('providers')).toBeNull();
	expect(screen.getByRole('group', { name: 'Claude' })).toBeTruthy();
});

it('keeps the warning on the page while the method is on, and Remove deletes it', async () => {
	cloud = savedGateway();
	worker.state.claude = claudeSummary({
		mode: 'cloud',
		mode_label: 'Cloud provider or gateway',
		cloud: cloud.status,
		gate: { ready: true, mode: 'cloud', claude_credential_ready: true, reason: null, license_allows_runs: true }
	});
	await show();
	expect(screen.getByText('Runs on LLM gateway')).toBeTruthy();
	expect(within(modes()).getByRole('button', { name: 'Cloud' }).getAttribute('aria-pressed')).toBe('true');

	settingsNav.go({ category: 'providers', item: CLAUDE_CLOUD_ITEM });
	await flush();
	expect(screen.getByText(WARNING)).toBeTruthy();
	expect(screen.queryByRole('button', { name: 'Save and use for Claude' })).toBeNull(); // already on
	await fireEvent.click(screen.getByRole('tab', { name: 'Models' }));
	await flush();
	expect(screen.getByText(WARNING)).toBeTruthy(); // on every tab

	await fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));
	expect(await screen.findByText('Remove LLM gateway?')).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
	await waitFor(() => expect(worker.sent('DELETE', '/settings/claude-auth/cloud')).toHaveLength(1));
	await waitFor(() => expect(settingsNav.selectedItem('providers')).toBe('provider:claude'));
});

it('Remove returns to Claude when the model refresh finishes after the detail has closed', async () => {
	// In a real browser the model refresh is a network round trip, so the
	// cloud row (and its detail) is gone before Remove moves on.
	cloud = savedGateway();
	worker.state.claude = claudeSummary({ cloud: cloud.status });
	await show(CLAUDE_CLOUD_ITEM);
	mocks.hydrate.mockImplementation(() => new Promise((r) => setTimeout(() => r(true), 20)));

	await fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));
	await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
	await waitFor(() => expect(worker.sent('DELETE', '/settings/claude-auth/cloud')).toHaveLength(1));
	await waitFor(() => expect(settingsNav.selectedItem('providers')).toBe('provider:claude'));
	expect(screen.queryByText('Select a provider or credential.')).toBeNull();
});

it('warns when cloud is on but its values are missing', async () => {
	worker.state.claude = claudeSummary({
		mode: 'cloud',
		cloud: {
			method: 'bedrock',
			method_label: 'Amazon Bedrock',
			configured: true,
			ready: false,
			missing: ['AWS access keys or Bedrock API key'],
			storage_locked: false
		},
		gate: { ready: false, mode: 'cloud', claude_credential_ready: false, reason: 'x', license_allows_runs: true }
	});
	await show();
	expect(screen.getByText('Not ready')).toBeTruthy();
	expect(
		screen.getByText('Claude cannot run: Amazon Bedrock needs AWS access keys or Bedrock API key.')
	).toBeTruthy();
	expect(within(screen.getByRole('region', { name: /^Credentials/ })).getByText('Values missing')).toBeTruthy();
});

it('is read-only for other devices: secret fields disabled and no way to save', async () => {
	cloud = savedGateway();
	worker.state.claude = claudeSummary({ cloud: cloud.status });
	await show(CLAUDE_CLOUD_ITEM, { readOnly: true });
	expect(screen.getByText(WARNING)).toBeTruthy();
	expect((screen.getByLabelText('Auth token') as HTMLInputElement).disabled).toBe(true);
	expect((screen.getByLabelText('Base URL') as HTMLInputElement).disabled).toBe(true);
	expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
	expect(screen.queryByRole('button', { name: 'Save and use for Claude' })).toBeNull();
	expect(worker.sent('PUT', '/settings/claude-auth/cloud')).toHaveLength(0);
});

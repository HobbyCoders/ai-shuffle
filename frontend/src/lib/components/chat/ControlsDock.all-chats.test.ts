/**
 * Every chat's controls row carries every control: the retired assistant home
 * (#371 step 5) was the only chat that dropped access, Away, effort, the
 * context ring and the browser toggle.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { activeSession } from '$lib/stores/active-session';
import { appMode } from '$lib/stores/sessionMode.svelte';
import { vaultSummary } from '$lib/stores/vault';
import ControlsDock from './ControlsDock.svelte';
import '../../test-stubs/composer-dom';
import { api } from '$lib/api/client';
import { getModels, hydrateModelRegistry } from '$lib/constants/models';

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: vi.fn().mockResolvedValue({}) }));
});

vi.mock('$lib/api/client', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, getUsageLimits: vi.fn().mockResolvedValue({ available: false, buckets: {}, provider_buckets: {} }) };
});

beforeEach(async () => {
	appMode.set('code');
	activeSession.newChat();
	const registryGet = vi.spyOn(api, 'get').mockResolvedValue({ models: getModels(), providers: [], claude: { auth_kind: 'subscription', plan_type: null } });
	await hydrateModelRegistry();
	registryGet.mockRestore();
});
afterEach(cleanup);
afterAll(() => vi.unstubAllGlobals());

function renderDock() {
	return render(ControlsDock, {
		props: { onBrowserToggle: vi.fn() }
	});
}

describe('ControlsDock in every chat', () => {
	it('keeps every control', async () => {
		renderDock();
		await waitFor(() => expect(screen.getByRole('button', { name: /^Model:/ })).toBeTruthy());
		expect(screen.getByRole('button', { name: /^Permission mode:/ })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^Effort:/ })).toBeTruthy();
		expect(screen.getAllByRole('button', { name: /^Context and usage/ }).length).toBeGreaterThan(0);
		expect(screen.getByRole('button', { name: 'Toggle live browser view' })).toBeTruthy();
	});

	it('shows the Vault only in a chat under way, like the browser and terminal', async () => {
		vaultSummary.set({ sessionId: 'chat-1', pending: 1, active: 0, saved: 0, error: false, manageable: true, panelId: 'vault-panel', open: vi.fn(), toggle: vi.fn() });
		const view = render(ControlsDock, { props: { onBrowserToggle: vi.fn(), newChat: true } });
		await waitFor(() => expect(screen.getByRole('button', { name: /^Model:/ })).toBeTruthy());
		expect(screen.queryByRole('button', { name: /^Vault/ })).toBeNull();
		await view.rerender({ onBrowserToggle: vi.fn(), newChat: false });
		expect(screen.getByRole('button', { name: 'Vault: 1 waiting for you' })).toBeTruthy();
		vaultSummary.set(null);
	});
});

/**
 * Every chat's controls row carries every control: the retired assistant home
 * (#371 step 5) was the only chat that dropped access, Away, effort, the
 * context ring and the browser toggle.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { activeSession } from '$lib/stores/active-session';
import { appMode } from '$lib/stores/sessionMode.svelte';
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

const voiceActions = createRawSnippet(() => ({ render: () => `<span data-testid="voice-snippet">voice</span>` }));

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
		props: { isUploading: false, onAttachClick: vi.fn(), voiceActions, onBrowserToggle: vi.fn() }
	});
}

describe('ControlsDock in every chat', () => {
	it('keeps every control', async () => {
		renderDock();
		await waitFor(() => expect(screen.getByRole('button', { name: /^Model:/ })).toBeTruthy());
		expect(screen.getByTestId('voice-snippet')).toBeTruthy();
		expect(screen.getByRole('button', { name: /^Permission mode:/ })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^Effort:/ })).toBeTruthy();
		expect(screen.getAllByRole('button', { name: /^Context and usage/ }).length).toBeGreaterThan(0);
		expect(screen.getByRole('button', { name: 'Toggle live browser view' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Attach file' })).toBeTruthy();
	});
});

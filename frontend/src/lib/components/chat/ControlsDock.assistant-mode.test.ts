/**
 * Assistant mode in the composer's controls row: voice, attach and the model
 * picker stay; the permission and Away menu, effort tag and context ring go.
 * An ordinary chat keeps every control.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
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

function renderDock(assistantMode: boolean) {
	return render(ControlsDock, {
		props: { isUploading: false, onAttachClick: vi.fn(), voiceActions, onBrowserToggle: assistantMode ? undefined : vi.fn(), assistantMode }
	});
}

describe('ControlsDock assistant mode', () => {
	it('keeps every control in an ordinary chat', async () => {
		renderDock(false);
		await waitFor(() => expect(screen.getByRole('button', { name: /^Model:/ })).toBeTruthy());
		expect(screen.getByRole('button', { name: /^Permission mode:/ })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^Effort:/ })).toBeTruthy();
		expect(screen.getAllByRole('button', { name: /^Context and usage/ }).length).toBeGreaterThan(0);
		expect(screen.getByRole('button', { name: 'Toggle live browser view' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Attach file' })).toBeTruthy();
	});

	it('keeps voice, attach and a working model picker in the assistant\'s home', async () => {
		renderDock(true);
		const model = await screen.findByRole('button', { name: /^Model:/ });
		expect(screen.getByTestId('voice-snippet')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Attach file' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /^Permission mode:/ })).toBeNull();
		expect(screen.queryByRole('menuitemcheckbox', { name: /Away/ })).toBeNull();
		expect(screen.queryByRole('button', { name: /^Effort:/ })).toBeNull();
		expect(screen.queryAllByRole('button', { name: /^Context and usage/ })).toHaveLength(0);
		expect(screen.queryByRole('button', { name: 'Toggle live browser view' })).toBeNull();
		await fireEvent.click(model);
		expect(await screen.findByRole('dialog', { name: 'Choose model' })).toBeTruthy();
	});
});

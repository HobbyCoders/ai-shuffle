import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('$lib/components/GitHubConnection.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('./ManageMediaModelsDrawer.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: () => () => {} }));
vi.mock('$lib/api/runtime', () => ({ apiBase: () => '/api/v1', withAuth: (init: RequestInit) => init }));

import MediaToolsSection from './MediaToolsSection.svelte';

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

it('offers only the ordinary fal.ai key, with no separate Admin billing key', async () => {
	const fetchMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({
		fal_api_key_set: true,
		fal_api_key_masked: 'fal-abc...wxyz',
		// An older backend may still report this; the UI must ignore it.
		fal_billing_api_key_set: true
	})));
	vi.stubGlobal('fetch', fetchMock);
	const { container } = render(MediaToolsSection);
	expect(await screen.findByText('fal-abc...wxyz')).toBeTruthy();
	expect(container.textContent).not.toMatch(/Admin|billing/i);
	expect(container.querySelector('[aria-label="fal.ai billing access"]')).toBeNull();
	expect(container.querySelectorAll('input[type="password"]')).toHaveLength(0);
	expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual(['/api/v1/settings/integrations']);
});

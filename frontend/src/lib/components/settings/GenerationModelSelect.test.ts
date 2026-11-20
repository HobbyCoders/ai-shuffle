import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';

const get = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: { get } }));
import { hydrateModelRegistry } from '$lib/constants/models';
import GenerationModelSelect from './GenerationModelSelect.svelte';

afterEach(cleanup);

function payload(auth: string | null = 'api') {
	return {
		models: [{ value: 'opus', label: 'Opus', family: 'opus' }], default: 'opus',
		claude: { auth_kind: auth, plan_type: null },
		providers: [{ id: 'strong', name: 'OpenRouter', provider_type: 'openrouter',
			models: [{ id: 'vendor/astra:latest', label: 'Astra', effort_metadata_version: 1, effort_levels: [] }] }]
	};
}

describe('Generation model catalog', () => {
	it('renders the hydrated native and provider choices with routable values, not Custom', async () => {
		get.mockResolvedValueOnce(payload());
		await hydrateModelRegistry();
		render(GenerationModelSelect);
		const select = screen.getByRole('button', { name: 'Generation model' });
		expect(select.textContent).toContain('Use Settings default');
		await fireEvent.click(select);
		expect(screen.getAllByRole('option').map((option) => option.dataset.value)).toEqual(['', 'prov:strong:vendor/astra:latest', 'opus']);
		expect(screen.getByRole('option', { name: 'Astra', description: /OpenRouter/ }).querySelector('[data-provider-logo="openrouter"]')).toBeTruthy();
		expect(screen.getByRole('option', { name: 'Opus', description: /Claude/ }).querySelector('[data-provider-logo="claude"]')).toBeTruthy();
		expect(screen.queryByRole('option', { name: /Custom/ })).toBeNull();
	});

	it('updates reactively after registry hydration and hides native Claude when not configured', async () => {
		get.mockResolvedValueOnce({ ...payload(null), providers: [] });
		await hydrateModelRegistry();
		render(GenerationModelSelect);
		await fireEvent.click(screen.getByRole('button', { name: 'Generation model' }));
		expect(screen.getAllByRole('option').map((option) => option.dataset.value)).toEqual(['']);
		get.mockResolvedValueOnce(payload(null));
		await hydrateModelRegistry();
		await tick();
		expect(screen.getAllByRole('option').map((option) => option.dataset.value)).toEqual(['', 'prov:strong:vendor/astra:latest']);
	});
});

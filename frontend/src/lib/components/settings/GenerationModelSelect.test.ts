import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
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
		const select = screen.getByRole('combobox', { name: /Generation model/ }) as HTMLSelectElement;
		expect(Array.from(select.options, (option) => option.value)).toEqual(['', 'opus', 'prov:strong:vendor/astra:latest']);
		expect(select.value).toBe('');
		expect(screen.queryByRole('option', { name: /Custom/ })).toBeNull();
	});

	it('updates reactively after registry hydration and hides native Claude when not configured', async () => {
		get.mockResolvedValueOnce({ ...payload(null), providers: [] });
		await hydrateModelRegistry();
		render(GenerationModelSelect);
		const select = screen.getByRole('combobox', { name: /Generation model/ }) as HTMLSelectElement;
		expect(Array.from(select.options, (option) => option.value)).toEqual(['']);
		get.mockResolvedValueOnce(payload(null));
		await hydrateModelRegistry();
		await tick();
		expect(Array.from(select.options, (option) => option.value)).toEqual(['', 'prov:strong:vendor/astra:latest']);
	});
});

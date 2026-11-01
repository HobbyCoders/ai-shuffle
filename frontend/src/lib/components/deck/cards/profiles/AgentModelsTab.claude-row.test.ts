/** The Claude caller row offers every provider's models, like the other rows: a Claude caller may pin an agent onto GPT. */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get } }));
vi.mock('$lib/stores/providers.svelte', () => ({
	providers: { current: { claude: null, usage: null }, loadClaude: vi.fn() }
}));

import { encodeProviderModel, hydrateModelRegistry } from '$lib/constants/models';
import AgentModelsTab from './AgentModelsTab.svelte';

beforeAll(async () => {
	mocks.get.mockResolvedValueOnce({
		models: [{ value: 'opus', label: 'Opus', family: 'opus', supports_1m: true }],
		default: 'opus',
		legacy_map: {},
		claude: { auth_kind: 'subscription', plan_type: null },
		providers: [
			{
				id: 'chatgpt-row',
				name: 'ChatGPT',
				provider_type: 'openai',
				models: [{ id: 'gpt-6.1-sol', label: 'GPT 6.1 Sol' }],
				default_model: 'gpt-6.1-sol'
			}
		]
	});
	await hydrateModelRegistry();
});

afterEach(() => {
	cleanup();
	localStorage.clear();
});

describe('the Claude caller row', () => {
	it('pins a Claude caller onto another provider’s model', async () => {
		const onModelsChange = vi.fn();
		render(AgentModelsTab, {
			providerModels: { __native__: 'opus' },
			subagentDefault: { model: 'opus' },
			onModelsChange,
			onDefaultModel: vi.fn()
		});

		const row = screen.getByText('Callers on native Claude.').closest('.st-row') as HTMLElement;
		await fireEvent.click(within(row).getByRole('button', { name: /Opus/ }));
		const tab = screen.queryByRole('tab', { name: /OpenAI|ChatGPT/ });
		if (tab) await fireEvent.click(tab);
		const list = within(screen.getByRole('listbox', { name: 'Model' }));
		await fireEvent.click(list.getByRole('option', { name: /GPT 6\.1 Sol/ }));

		expect(onModelsChange).toHaveBeenCalledWith({
			__native__: encodeProviderModel('chatgpt-row', 'gpt-6.1-sol')
		});
	});
});

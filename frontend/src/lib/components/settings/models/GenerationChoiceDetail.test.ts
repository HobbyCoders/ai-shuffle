import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { GenerationChoice, GenerationOption } from '$lib/utils/generation-choice';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, put: mocks.put, post: mocks.post, delete: mocks.del },
	getErrorMessage: (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)
}));
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	const store = writable({ messages: [] as unknown[], sessionId: 's1' });
	return { activeSession: { subscribe: store.subscribe, sendMessage: vi.fn() } };
});

import { generationChoices } from '$lib/stores/generation-choices.svelte';
import GenerationChoiceDetail from './GenerationChoiceDetail.svelte';

const PRICE = { currency: 'USD', unit: 'item', amount: '0.04', total: '0.04', status: 'estimated' };
const FLUX: GenerationChoice = { source: 'fal', account: 'fal-ai', worker: 'worker-a', model: 'fal-ai/flux/dev', options: {} };
const NANO: GenerationChoice = { source: 'fal', account: 'fal-ai', worker: 'worker-a', model: 'fal-ai/nano-banana-2', options: {} };
const option = (choice: GenerationChoice): GenerationOption => ({
	...choice,
	source: choice.source ?? 'fal',
	account: choice.account ?? 'fal-ai',
	worker: choice.worker ?? 'worker-a',
	kind: 'image',
	options: {},
	verified: false,
	price: PRICE,
	schema: null,
	available: true,
	reason: null
});
const WARNING = 'This saved choice has no verified price. Unattended runs will skip it until a price is available.';
const UNPRICED = {
	price: { currency: 'USD', unit: 'item', amount: null, total: null, status: 'unknown' },
	priced: false,
	quote_error: null,
	unattended_skip_reason: 'stored_choice_unpriced',
	warning: WARNING
};
const PRICED = { price: PRICE, priced: true, quote_error: null, unattended_skip_reason: null, warning: null };

beforeEach(() => {
	vi.clearAllMocks();
	generationChoices.remembered = {};
	generationChoices.last = {};
	mocks.get.mockImplementation(async (url: string) => {
		if (url === '/generation/choices') return { remembered: { image: FLUX }, last: { image: NANO }, migrated: true, worker: 'worker-a' };
		if (url.startsWith('/generation/options/image')) return { options: [option(FLUX), option(NANO)] };
		return {};
	});
});
afterEach(cleanup);

const banner = () => screen.queryByText(/^Remembered\. /);

describe('GenerationChoiceDetail', () => {
	it('clears the Remember warning when a later save of the same model succeeds', async () => {
		render(GenerationChoiceDetail, { props: { kind: 'image' } });
		mocks.put.mockImplementation(async (_url: string, body: { choice: GenerationChoice; remember: boolean }) => ({
			kind: 'image',
			choice: { ...body.choice, origin: 'settings' },
			remembered: body.remember,
			...UNPRICED
		}));
		await fireEvent.click(await screen.findByRole('button', { name: 'Remember' }));
		await waitFor(() => expect(banner()?.textContent).toContain(WARNING));

		// The same model, saved again through the picker, now has a price.
		mocks.put.mockImplementation(async (_url: string, body: { choice: GenerationChoice; remember: boolean }) => ({
			kind: 'image',
			choice: { ...body.choice, origin: 'settings' },
			remembered: body.remember,
			...PRICED
		}));
		const card = document.querySelector('section.gcc') as HTMLElement;
		await fireEvent.click(within(card).getAllByRole('radio')[1]);
		await fireEvent.click(within(card).getByRole('button', { name: /Remember this/ }));
		await waitFor(() => expect(within(card).getByRole('status').textContent).toContain('Unattended runs can use it.'));
		expect(banner()).toBeNull();
		expect(mocks.put).toHaveBeenCalledTimes(2);
		expect(generationChoices.remembered.image?.model).toBe('fal-ai/nano-banana-2');
	});

	it('clears the Remember warning when the choice is forgotten', async () => {
		render(GenerationChoiceDetail, { props: { kind: 'image' } });
		mocks.put.mockImplementation(async (_url: string, body: { choice: GenerationChoice }) => ({
			kind: 'image',
			choice: { ...body.choice, origin: 'settings' },
			remembered: true,
			...UNPRICED
		}));
		mocks.del.mockResolvedValue({ kind: 'image', forgotten: true });
		await fireEvent.click(await screen.findByRole('button', { name: 'Remember' }));
		await waitFor(() => expect(banner()).not.toBeNull());
		await fireEvent.click(screen.getByRole('button', { name: 'Ask every time' }));
		await waitFor(() => expect(banner()).toBeNull());
	});
});

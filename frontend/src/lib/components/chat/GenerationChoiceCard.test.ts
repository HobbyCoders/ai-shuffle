import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { GenerationOption } from '$lib/utils/generation-choice';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn(), send: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, put: mocks.put, post: mocks.post, delete: mocks.del },
	getErrorMessage: (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)
}));
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	const store = writable({ messages: [] as unknown[], sessionId: 's1' });
	return { activeSession: { subscribe: store.subscribe, sendMessage: mocks.send, __set: store.set } };
});

import { generationChoices } from '$lib/stores/generation-choices.svelte';
import GenerationChoiceCard from './GenerationChoiceCard.svelte';

const PRICE = { currency: 'USD', unit: 'item', amount: '0.025', total: '0.025', status: 'estimated' };
const FAL: GenerationOption = {
	source: 'fal',
	account: 'fal-ai',
	worker: 'worker-a',
	model: 'fal-ai/flux/dev',
	kind: 'image',
	options: { image_size: 'square', num_images: 1 },
	verified: false,
	price: PRICE,
	quick_settings: { image_size: 'square', num_images: 1 },
	schema: {
		fields: [
			{ name: 'prompt', type: 'string', is_primary_text: true },
			{ name: 'image_size', type: 'enum', title: 'Image size', enum: ['square', 'landscape_16_9'], default: 'square' },
			{ name: 'num_images', type: 'integer', title: 'Images', minimum: 1, maximum: 4, default: 1 },
			{ name: 'enable_safety_checker', type: 'boolean', default: true },
			{ name: 'seed', type: 'integer' }
		]
	},
	available: true,
	reason: null,
	label: 'Same as last time'
};
const GROK: GenerationOption = {
	source: 'supergrok',
	account: 'grok-1',
	worker: 'worker-b',
	model: 'grok-imagine-image',
	kind: 'image',
	options: { size: '1k', quality: null, format: 'jpeg' },
	verified: true,
	price: { currency: 'USD', unit: 'item', amount: '0', total: '0', status: 'included' },
	quick_settings: { size: '1k', format: 'jpeg' },
	schema: {
		fields: {
			model: [{ value: 'grok-imagine-image', verified: true, selectable: true }],
			size: [
				{ value: '1k', verified: true, selectable: true },
				{ value: '2k', verified: false, selectable: false, reason: 'not yet verified' }
			],
			format: [{ value: 'jpeg', verified: true, selectable: true }]
		}
	},
	available: true,
	reason: null
};
const OPENAI: GenerationOption = {
	...GROK,
	source: 'openai',
	account: 'api-a',
	model: 'unavailable',
	verified: false,
	price: { currency: 'USD', unit: 'item', amount: null, total: null, status: 'unknown' },
	available: false,
	reason: 'No verified media schema is available for this account.',
	schema: null
};

function card(props: Record<string, unknown> = {}) {
	return render(GenerationChoiceCard, {
		props: { kind: 'image', options: [FAL, GROK, OPENAI], tool: 'generateImage', prompt: 'a lighthouse at dusk', toolId: 'tool-1', answeredBy: null, ...props }
	});
}

beforeEach(() => {
	vi.useRealTimers();
	vi.clearAllMocks();
	generationChoices.remembered = {};
	generationChoices.last = {};
	mocks.get.mockResolvedValue({ remembered: {}, last: {}, migrated: true, worker: 'worker-a' });
	mocks.put.mockImplementation(async (_url: string, body: { choice: unknown; origin: string }) => ({ choice: { ...(body.choice as object), origin: body.origin } }));
	mocks.send.mockResolvedValue(true);
	mocks.post.mockImplementation(async (_url: string, body: { choice: { options: Record<string, unknown> } }) => {
		const wide = body.choice.options.image_size === 'landscape_16_9';
		const count = Number(body.choice.options.num_images ?? 1);
		const amount = wide ? 0.04 : 0.025;
		return {
			ok: true,
			priced: true,
			options: body.choice.options,
			price: { ...PRICE, amount: String(amount), total: String(Number((amount * count).toFixed(3))) }
		};
	});
});
afterEach(cleanup);

describe('GenerationChoiceCard', () => {
	it('offers each account with its model, quick settings and price; same as last time first and selected', () => {
		card();
		const radios = screen.getAllByRole('radio');
		expect(radios).toHaveLength(3);
		expect(radios[0].getAttribute('aria-checked')).toBe('true');
		expect(within(radios[0]).getByText('Same as last time')).toBeTruthy();
		expect(within(radios[0]).getByText('Flux Dev')).toBeTruthy();
		expect(within(radios[0]).getByText('$0.025')).toBeTruthy();
		expect(within(radios[0]).getByText(/image size square/)).toBeTruthy();
		expect(within(radios[1]).getByText('Included')).toBeTruthy();
		expect((radios[2] as HTMLButtonElement).disabled).toBe(true);
		expect(within(radios[2]).getByText(/No verified media schema/)).toBeTruthy();
		expect(screen.getByRole('heading', { name: 'How should I make this image?' })).toBeTruthy();
	});

	it('moves the selection with the arrow keys, skipping unavailable options', async () => {
		card();
		const radios = screen.getAllByRole('radio');
		await fireEvent.keyDown(radios[0], { key: 'ArrowRight' });
		expect(radios[1].getAttribute('aria-checked')).toBe('true');
		await fireEvent.keyDown(radios[1], { key: 'ArrowRight' });
		expect(radios[0].getAttribute('aria-checked')).toBe('true');
	});

	it('lists exactly the selected model’s declared options and re-prices as they change', async () => {
		card();
		await fireEvent.click(screen.getByRole('button', { name: /Advanced/ }));
		expect(screen.getByRole('button', { name: /Advanced/ }).textContent).toContain('4 options for Flux Dev');
		expect(screen.getByLabelText(/Image size/)).toBeTruthy();
		expect(screen.getByLabelText(/^Images/)).toBeTruthy();
		expect(screen.getByLabelText(/Enable safety checker/)).toBeTruthy();
		expect(screen.getByLabelText(/Seed/)).toBeTruthy();
		expect(screen.queryByLabelText(/Prompt/)).toBeNull();
		await fireEvent.change(screen.getByLabelText(/Image size/), { target: { value: 'landscape_16_9' } });
		await fireEvent.input(screen.getByLabelText(/^Images/), { target: { value: '2' } });
		await waitFor(() => expect(mocks.post).toHaveBeenCalledTimes(1), { timeout: 2000 });
		expect(mocks.post).toHaveBeenCalledWith('/generation/quote', {
			kind: 'image',
			choice: { source: 'fal', account: 'fal-ai', worker: 'worker-a', model: 'fal-ai/flux/dev', options: { image_size: 'landscape_16_9', num_images: 2 } },
			prompt: 'a lighthouse at dusk'
		});
		await waitFor(() => expect(document.querySelector('.gcc-total')?.textContent).toBe('$0.08'));
		expect(screen.getByRole('button', { name: /Advanced/ }).textContent).toContain('2 changed');
	});

	it('steps a decimal range finely enough to keep its default', async () => {
		const fields = [
			{ name: 'guidance_scale', type: 'number', title: 'Guidance', minimum: 1, maximum: 20, default: 3.5 },
			{ name: 'strength', type: 'number', title: 'Strength', minimum: 0, maximum: 1, default: 0.85 },
			{ name: 'steps', type: 'integer', title: 'Steps', minimum: 1, maximum: 50, default: 28 }
		];
		card({ options: [{ ...FAL, options: {}, schema: { fields } }] });
		await fireEvent.click(screen.getByRole('button', { name: /Advanced/ }));
		const step = (label: RegExp) => (screen.getByLabelText(label) as HTMLInputElement).step;
		expect(step(/Guidance/)).toBe('0.1');
		expect(step(/Strength/)).toBe('0.05');
		expect(step(/Steps/)).toBe('1');
		expect((screen.getByLabelText(/Guidance/) as HTMLInputElement).value).toBe('3.5');
	});

	it('shows only verified values for a plan route, locked', async () => {
		card();
		await fireEvent.click(screen.getAllByRole('radio')[1]);
		await fireEvent.click(screen.getByRole('button', { name: /Advanced/ }));
		const size = screen.getByLabelText(/Size/) as HTMLSelectElement;
		expect(size.disabled).toBe(true);
		const options = [...size.options];
		expect(options.find((o) => o.value === '2k')?.disabled).toBe(true);
		expect(options.find((o) => o.value === '2k')?.textContent).toContain('not yet verified');
		expect(screen.queryByLabelText(/^Model/)).toBeNull();
	});

	it('saves the pick as remembered and replies with the exact arguments', async () => {
		card();
		await fireEvent.click(screen.getAllByRole('radio')[1]);
		await fireEvent.click(screen.getByRole('button', { name: 'Use this' }));
		await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
		expect(mocks.put).toHaveBeenCalledWith('/generation/choices/image', {
			choice: { source: 'supergrok', account: 'grok-1', worker: 'worker-b', model: 'grok-imagine-image', options: GROK.options },
			remember: true,
			origin: 'card'
		});
		const message = mocks.send.mock.calls[0][0] as string;
		expect(message).toContain('Use SuperGrok · Grok Imagine Image for this image, included in my plan.');
		expect(message).toContain('"source":"grok-1","model":"grok-imagine-image","worker":"worker-b"');
		await waitFor(() => expect(screen.getByRole('heading', { name: 'Using SuperGrok · Grok Imagine Image' })).toBeTruthy());
		expect(screen.getByText(/remembered for images/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Ask me next time' }));
		expect(mocks.del).toHaveBeenCalledWith('/generation/choices/image');
	});

	it('ticks "use this every time" only for an option that costs nothing, until the box is touched', async () => {
		card();
		const box = () => screen.getByRole('checkbox', { name: /Use this every time for images/ }) as HTMLInputElement;
		const radios = screen.getAllByRole('radio');
		expect(box().checked).toBe(false); // fal.ai, priced
		await fireEvent.click(radios[1]);
		expect(box().checked).toBe(true); // SuperGrok, included in the plan
		await fireEvent.click(radios[0]);
		expect(box().checked).toBe(false);
		// Once the user sets it, changing the option keeps their setting.
		await fireEvent.click(box());
		expect(box().checked).toBe(true);
		await fireEvent.click(radios[1]);
		await fireEvent.click(radios[0]);
		expect(box().checked).toBe(true);
	});

	it('keeps a priced pick out of memory by default', async () => {
		card();
		await fireEvent.click(screen.getByRole('button', { name: 'Use this' }));
		await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
		expect(mocks.put.mock.calls[0][1]).toMatchObject({ remember: false });
		expect(mocks.send.mock.calls[0][0]).not.toContain('saved');
	});

	it('keeps an included pick out of memory when the box is unticked', async () => {
		card();
		await fireEvent.click(screen.getAllByRole('radio')[1]);
		await fireEvent.click(screen.getByRole('checkbox', { name: /Use this every time for images/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Use this' }));
		await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
		expect(mocks.put.mock.calls[0][1]).toMatchObject({ remember: false });
	});

	it('cannot run a choice whose options cannot be priced', async () => {
		mocks.post.mockResolvedValueOnce({ ok: false, error: 'Model options do not match the declared input schema.' });
		card();
		await fireEvent.click(screen.getByRole('button', { name: /Advanced/ }));
		await fireEvent.input(screen.getByLabelText(/Seed/), { target: { value: '7' } });
		expect(await screen.findByText(/do not match the declared input schema/, {}, { timeout: 2000 })).toBeTruthy();
		expect((screen.getByRole('button', { name: 'Use this' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('warns before an above-threshold price', () => {
		card({ options: [{ ...FAL, price: { ...PRICE, amount: '0.25', total: '0.25' } }] });
		expect(screen.getByText(/ask again before spending/)).toBeTruthy();
	});

	it('collapses once answered in the chat and names the picked option', async () => {
		const reply = 'Use fal.ai · Flux Dev for this image.\nCall generateImage again with the same prompt and {"source":"fal-ai","model":"fal-ai/flux/dev","worker":"worker-a","options":{}}.';
		card({ answeredBy: reply });
		expect(screen.getByRole('heading', { name: 'Using fal.ai · Flux Dev' })).toBeTruthy();
		expect(screen.queryAllByRole('radio')).toHaveLength(0);
		await fireEvent.click(screen.getByRole('button', { name: 'Show options' }));
		expect(screen.getAllByRole('radio')).toHaveLength(3);
		expect((screen.getByRole('button', { name: 'Use this' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('says when the remembered choice is unavailable', () => {
		card({ rememberedUnavailable: true });
		expect(screen.getByText(/remembered choice for images is not available/)).toBeTruthy();
	});

	it('says when the remembered choice has no known price', () => {
		card({ rememberedUnavailable: false, rememberedUnpriced: true });
		expect(screen.getByText(/remembered choice for images has no known price right now, so it did not run/)).toBeTruthy();
	});

	it('remembers a pick from Settings without replying in a chat', async () => {
		const onsaved = vi.fn();
		card({ variant: 'settings', onsaved, answeredBy: undefined, toolId: '' });
		expect(screen.queryByRole('checkbox')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: /Remember this/ }));
		await waitFor(() => expect(onsaved).toHaveBeenCalledOnce());
		expect(mocks.put.mock.calls[0][1]).toMatchObject({ remember: true, origin: 'settings' });
		expect(mocks.send).not.toHaveBeenCalled();
	});
});

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
	return { activeSession: { subscribe: store.subscribe, sendMessage: mocks.send, reserveSend: () => ({ cancel() {} }), __set: store.set } };
});

import { generationChoices } from '$lib/stores/generation-choices.svelte';
import { refreshWorkerPresence } from '$lib/stores/worker-presence';
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

const UNKNOWN_PRICE_FIXTURE = { currency: 'USD', unit: 'item', amount: null, total: null, status: 'unknown' };

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

	it('names the worker holding an option that is not on this worker', async () => {
		mocks.get.mockImplementation(async (url: string) =>
			url === '/cluster/installs'
				? { installs: [{ id: 'worker-b', device_id: 'worker-b', device_name: 'DesktopWorker', online: true }], self_instance_id: 'worker-a' }
				: { remembered: {}, last: {}, migrated: true, worker: 'worker-a' }
		);
		await refreshWorkerPresence();
		await generationChoices.load(true);
		const sameOnPeer: GenerationOption = { ...FAL, worker: 'worker-b', label: undefined };
		card({ options: [FAL, sameOnPeer, GROK] });
		const radios = screen.getAllByRole('radio');
		expect(radios[0].querySelector('.gcc-where')).toBeNull();
		await waitFor(() => expect(radios[1].querySelector('.gcc-where')?.textContent).toBe('on DesktopWorker'));
		expect(radios[2].querySelector('.gcc-where')?.textContent).toBe('on DesktopWorker');
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

	it('replies to a native alias card by calling that alias again with the exact pick (#550)', async () => {
		card({ options: [GROK], tool: 'GenerateNativeImage' });
		await fireEvent.click(screen.getByRole('button', { name: 'Use this' }));
		await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
		expect(mocks.put.mock.calls[0][1]).toMatchObject({
			choice: { source: 'supergrok', account: 'grok-1', worker: 'worker-b', model: 'grok-imagine-image', options: GROK.options }
		});
		const message = mocks.send.mock.calls[0][0] as string;
		expect(message).toContain(
			`Call GenerateNativeImage again with the same prompt and ${JSON.stringify({ source: 'grok-1', model: 'grok-imagine-image', worker: 'worker-b', options: GROK.options })}.`
		);
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

	it('lets the owner approve one unbounded fal run, showing why and fal’s raw rate', async () => {
		const unbounded = {
			...UNKNOWN_PRICE_FIXTURE,
			reason: 'billed_by_usage',
			detail: 'fal bills this model by what each run uses, which is not known in advance.',
			rate: { amount: '0.30', unit: 'seconds' }
		};
		mocks.post.mockImplementation(async (_url: string, body: { choice: { options: Record<string, unknown> } }) => ({
			ok: true, priced: false, options: body.choice.options, price: unbounded
		}));
		card({ options: [{ ...FAL, price: unbounded }] });
		const note = await screen.findByTestId('gcc-unbounded');
		expect(note.textContent).toContain('$0.30 per second');
		expect(note.textContent).toContain('not bounded');
		expect(note.textContent).toContain('bills this model by what each run uses');
		expect((screen.getByRole('checkbox', { name: /Use this every time/ }) as HTMLInputElement).disabled).toBe(true);
		const use = screen.getByRole('button', { name: 'Use this' }) as HTMLButtonElement;
		await waitFor(() => expect(use.disabled).toBe(false));
		await fireEvent.click(use);
		await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
		expect(String(mocks.send.mock.calls[0][0])).toContain('no bounded price, $0.30 per second');
		expect(mocks.put.mock.calls[0][1]).toMatchObject({ remember: false });
	});

	it('cannot run an unknown price fal lists no rate for, and says why', async () => {
		const missing = { ...UNKNOWN_PRICE_FIXTURE, reason: 'no_pricing_row', detail: 'fal lists no price for this model.' };
		mocks.post.mockImplementation(async (_url: string, body: { choice: { options: Record<string, unknown> } }) => ({
			ok: true, priced: false, options: body.choice.options, price: missing
		}));
		card({ options: [{ ...FAL, price: missing }] });
		expect(await screen.findByText(/fal lists no price for this model\. It cannot run/)).toBeTruthy();
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

// The save response (#518), with the exact strings app/api/generation_choices.py returns.
const UNKNOWN = { currency: 'USD', unit: 'item', amount: null, total: null, status: 'unknown' };
const SAVED_PRICED = {
	price: { ...PRICE, amount: '0.03', total: '0.03', status: 'quoted' },
	priced: true,
	quote_error: null,
	unattended_skip_reason: null,
	warning: null
};
const SAVED_UNPRICED = {
	price: UNKNOWN,
	priced: false,
	quote_error: null,
	unattended_skip_reason: 'stored_choice_unpriced',
	warning: 'This saved choice has no verified price. Unattended runs will skip it until a price is available.'
};
const unavailable = (error: string) => ({
	price: UNKNOWN,
	priced: false,
	quote_error: error,
	unattended_skip_reason: 'stored_choice_unavailable',
	warning: `${error} Unattended runs will skip it.`
});
const SAVED_UNAVAILABLE = unavailable('This account or model is not available right now.');
const SAVED_TIMEOUT = unavailable("The saved choice's quote timed out. Its worker or provider may be unavailable.");

function saveReplies(quote: Record<string, unknown>) {
	mocks.put.mockImplementation(async (_url: string, body: { choice: object; remember: boolean; origin: string }) => ({
		kind: 'image',
		choice: { ...body.choice, origin: body.origin },
		remembered: body.remember,
		...quote
	}));
}

const notice = () => document.querySelector('.gcc-saved') as HTMLElement | null;

describe('GenerationChoiceCard save-time quote', () => {
	it('collapses a priced chat pick on the fresh server price, without quoting again', async () => {
		saveReplies(SAVED_PRICED);
		card();
		await fireEvent.click(screen.getByRole('button', { name: 'Use this' }));
		await waitFor(() => expect(screen.getByRole('heading', { name: 'Using fal.ai · Flux Dev' })).toBeTruthy());
		// The card priced it at $0.025; the server's fresh quote is what it shows now.
		expect(document.querySelector('.gcc-sub')?.textContent).toContain('$0.03');
		expect(document.querySelector('.gcc-sub-warn')).toBeNull();
		expect(mocks.post).not.toHaveBeenCalled();
		expect(mocks.send).toHaveBeenCalledOnce();
	});

	it('keeps an unpriced chat pick open with the server warning, and on the collapsed line', async () => {
		saveReplies(SAVED_UNPRICED);
		card();
		await fireEvent.click(screen.getAllByRole('radio')[1]);
		await fireEvent.click(screen.getByRole('button', { name: 'Use this' }));
		const alert = await screen.findByRole('alert');
		expect(alert.textContent).toContain('Saved and remembered:');
		expect(alert.textContent).toContain('SuperGrok · Grok Imagine Image');
		expect(alert.textContent).toContain('No verified price');
		expect(alert.textContent).toContain(SAVED_UNPRICED.warning);
		expect(within(alert).getByText('Unknown')).toBeTruthy();
		expect(notice()?.dataset.state).toBe('unpriced');
		// Still expanded: the pick went out once, nothing was quoted or resent.
		expect(screen.getAllByRole('radio')).toHaveLength(3);
		expect(mocks.send).toHaveBeenCalledOnce();
		expect(mocks.post).not.toHaveBeenCalled();
		expect(generationChoices.remembered.image?.model).toBe('grok-imagine-image');
		await fireEvent.click(screen.getByRole('button', { name: 'Hide' }));
		expect(screen.getByRole('heading', { name: 'Using SuperGrok · Grok Imagine Image' })).toBeTruthy();
		expect(document.querySelector('.gcc-sub')?.textContent).toContain('Unknown');
		expect(document.querySelector('.gcc-sub-warn')?.textContent).toContain('Unattended runs will skip it');
	});

	it.each([
		['an unavailable route', SAVED_UNAVAILABLE],
		['a 2-second quote timeout', SAVED_TIMEOUT],
		['another quote failure', unavailable('A fresh quote for this saved choice is unavailable right now.')]
	])('keeps the pick and names %s as unavailable, not unpriced', async (_name, quote) => {
		saveReplies(quote);
		card();
		await fireEvent.click(screen.getByRole('button', { name: 'Use this' }));
		const alert = await screen.findByRole('alert');
		expect(notice()?.dataset.state).toBe('unavailable');
		expect(alert.textContent).toContain('Unavailable now');
		expect(alert.textContent).toContain(quote.quote_error);
		expect(alert.textContent).toContain('Unattended runs will skip it.');
		expect(alert.textContent).not.toContain('No verified price');
		expect(within(alert).getByText('No quote')).toBeTruthy();
		expect(generationChoices.last.image?.model).toBe('fal-ai/flux/dev');
		expect(mocks.post).not.toHaveBeenCalled();
	});

	it('still shows what was saved when the chat reply then fails to send', async () => {
		saveReplies(SAVED_UNPRICED);
		mocks.send.mockResolvedValue(false);
		card();
		await fireEvent.click(screen.getByRole('button', { name: 'Use this' }));
		await waitFor(() => expect(screen.getByText(/Your pick was saved, but the reply could not be sent/)).toBeTruthy());
		expect(notice()?.textContent).toContain(SAVED_UNPRICED.warning);
		expect(notice()?.textContent).toContain('Saved as your last choice:');
		expect(generationChoices.last.image?.model).toBe('fal-ai/flux/dev');
		// Not answered: the card stays open and can be used again.
		expect(screen.queryByRole('button', { name: 'Hide' })).toBeNull();
		expect((screen.getByRole('button', { name: 'Use this' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('shows the fresh price in Settings after remembering a priced pick', async () => {
		saveReplies(SAVED_PRICED);
		card({ variant: 'settings', answeredBy: undefined, toolId: '' });
		await fireEvent.click(screen.getByRole('button', { name: /Remember this/ }));
		const status = await screen.findByRole('status');
		expect(status.textContent).toContain('Remembered:');
		expect(status.textContent).toContain('Unattended runs can use it.');
		expect(within(status).getByText('$0.03')).toBeTruthy();
		expect(screen.queryByRole('alert')).toBeNull();
	});

	it('warns in Settings when the remembered pick has no verified price', async () => {
		saveReplies(SAVED_UNPRICED);
		card({ variant: 'settings', answeredBy: undefined, toolId: '' });
		await fireEvent.click(screen.getByRole('button', { name: /Remember this/ }));
		const alert = await screen.findByRole('alert');
		expect(alert.textContent).toContain('Remembered:');
		expect(alert.textContent).toContain(SAVED_UNPRICED.warning);
		expect(generationChoices.remembered.image?.model).toBe('fal-ai/flux/dev');
	});

	it('shows a handed-off pin warning, and nothing for a staged pick', async () => {
		const onpick = vi.fn().mockResolvedValue({ choice: {}, ...SAVED_TIMEOUT });
		card({ variant: 'settings', answeredBy: undefined, toolId: '', onpick, pickLabel: 'Pin to this job' });
		await fireEvent.click(screen.getByRole('button', { name: /Pin to this job/ }));
		const alert = await screen.findByRole('alert');
		expect(alert.textContent).toContain('Pinned to this job:');
		expect(alert.textContent).toContain('timed out');
		expect(mocks.put).not.toHaveBeenCalled();
		cleanup();
		card({ variant: 'settings', answeredBy: undefined, toolId: '', onpick: vi.fn().mockResolvedValue(undefined), pickLabel: 'Pin to this job' });
		await fireEvent.click(screen.getByRole('button', { name: /Pin to this job/ }));
		await waitFor(() => expect((screen.getByRole('button', { name: /Pin to this job/ }) as HTMLButtonElement).disabled).toBe(false));
		expect(notice()).toBeNull();
	});
});

describe('GenerationChoiceCard: provider, then any model, then its options', () => {
	const RECRAFT: GenerationOption = {
		...FAL,
		model: 'fal-ai/recraft/v3',
		label: undefined,
		options: { style: 'realistic' },
		quick_settings: { style: 'realistic' },
		price: { ...PRICE, amount: '0.04', total: '0.04' },
		schema: { fields: [{ name: 'prompt', type: 'string', is_primary_text: true }, { name: 'style', type: 'enum', title: 'Style', enum: ['realistic', 'vector'], default: 'realistic' }] }
	};
	const UNQUOTED = { ...UNKNOWN_PRICE_FIXTURE, reason: 'not_quoted', detail: 'Pick this model to price its options.', rate: { amount: '0.04', unit: 'images' } };
	const row = (model: string, name: string, extra: Record<string, unknown> = {}) => ({
		source: 'fal', account: 'fal-ai', worker: 'worker-a', model, kind: 'image', options: {}, verified: false,
		price: UNQUOTED, schema: null, available: true, reason: null, status: 'unpriced', name, ...extra
	});

	beforeEach(() => {
		mocks.get.mockImplementation(async (url: string) => {
			if (url.startsWith('/generation/choices')) return { remembered: {}, last: {}, migrated: true, worker: 'worker-a' };
			if (url.startsWith('/generation/providers/image')) {
				return {
					kind: 'image', operation: 'text-to-image', operation_label: 'Text to image',
					providers: [
						{ source: 'fal', account: 'fal-ai', workers: ['worker-a'], available: true, reason: null, native: false, model_count: 3 },
						{ source: 'supergrok', account: 'grok-9', workers: ['worker-c'], available: false, reason: 'Update that worker.', native: true }
					]
				};
			}
			if (url.startsWith('/generation/models/image')) {
				const q = new URLSearchParams(url.split('?')[1]);
				if (q.get('query') === 'nothing') return { models: [], total: 0, offset: 0, limit: 20, has_more: false };
				if (q.get('offset') === '2') return { models: [row('fal-ai/sdxl', 'SDXL', { available: false, reason: 'fal is down.' , status: 'unavailable'})], total: 3, offset: 2, limit: 20, has_more: false };
				return { models: [row('fal-ai/recraft/v3', 'Recraft V3'), row('fal-ai/flux/dev', 'FLUX dev')], total: 3, offset: 0, limit: 20, has_more: true };
			}
			if (url.startsWith('/generation/catalog')) return { entries: [{ ...RECRAFT, status: 'available' }], favourites: {}, kinds: ['image'], limit: 1 };
			throw new Error(`unexpected ${url}`);
		});
	});

	it('lists providers with unavailable reasons, pages and searches on the server, and never shows unknown as free', async () => {
		card();
		await fireEvent.click(screen.getByRole('button', { name: /All models/ }));
		const providers = await screen.findByRole('radiogroup', { name: 'Provider' });
		expect(within(providers).getByRole('radio', { name: /fal\.ai/ }).getAttribute('aria-checked')).toBe('true');
		expect(screen.getByText(/Update that worker\./)).toBeTruthy();
		const recraft = await screen.findByRole('button', { name: /Recraft V3/ });
		expect(recraft.textContent).toContain('$0.04 per image');
		expect(recraft.textContent).not.toMatch(/Free|Included/);
		expect(screen.getByText('2 of 3')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
		expect((await screen.findByRole('button', { name: /SDXL/ }) as HTMLButtonElement).disabled).toBe(true);
		expect(screen.getByText('fal is down.')).toBeTruthy();
		expect(mocks.get).toHaveBeenCalledWith(expect.stringMatching(/\/generation\/models\/image\?source=fal&offset=2&limit=20/));
		await fireEvent.input(screen.getByPlaceholderText('Search 3 models'), { target: { value: 'nothing' } });
		expect(await screen.findByText('No model matches “nothing”.')).toBeTruthy();
		expect(mocks.get).toHaveBeenCalledWith(expect.stringContaining('query=nothing'));
	});

	it('loads a picked model with its declared options, quotes them live and sends that model back', async () => {
		card();
		await fireEvent.click(screen.getByRole('button', { name: /All models/ }));
		await fireEvent.click(await screen.findByRole('button', { name: /Recraft V3/ }));
		const picked = await screen.findByRole('radio', { name: /Picked/ });
		expect(picked.getAttribute('aria-checked')).toBe('true');
		expect(picked.textContent).toMatch(/recraft/i);
		expect(mocks.get).toHaveBeenCalledWith(expect.stringMatching(/\/generation\/catalog\?kind=image&source=fal&model=fal-ai%2Frecraft%2Fv3/));
		const style = await screen.findByLabelText('Style');
		await fireEvent.change(style, { target: { value: 'vector' } });
		await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/generation/quote', expect.objectContaining({ choice: expect.objectContaining({ model: 'fal-ai/recraft/v3', options: { style: 'vector' } }) })));
		await waitFor(() => expect((screen.getByRole('button', { name: /^Use / }) as HTMLButtonElement).disabled).toBe(false));
		await fireEvent.click(screen.getByRole('button', { name: /^Use / }));
		await waitFor(() => expect(mocks.send).toHaveBeenCalled());
		expect(mocks.send.mock.calls[0][0]).toContain('"model":"fal-ai/recraft/v3"');
		expect(mocks.send.mock.calls[0][0]).toContain('"style":"vector"');
	});

	it('keeps an unselected option showing the options its price was quoted for', async () => {
		card();
		await fireEvent.click(screen.getByRole('button', { name: /Advanced/ }));
		await fireEvent.change(await screen.findByLabelText('Image size'), { target: { value: 'landscape_16_9' } });
		await waitFor(() => expect(mocks.post).toHaveBeenCalled());
		await fireEvent.click(screen.getByRole('radio', { name: /SuperGrok/ }));
		const fal = screen.getByRole('radio', { name: /flux/i });
		expect(fal.getAttribute('aria-checked')).toBe('false');
		expect(fal.textContent).toContain('image size landscape_16_9');
		expect(fal.textContent).not.toContain('image size square');
	});
});

import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { get: mocks.get, put: mocks.put, post: mocks.post, delete: mocks.del } }));

import { generationChoices } from './generation-choices.svelte';
import { saveOutcome } from '$lib/utils/generation-choice';

const PICK = { source: 'fal', account: 'fal-ai', worker: 'worker-a', model: 'fal-ai/flux/dev', options: {} };
const UNKNOWN = { currency: 'USD', unit: 'item', amount: null, total: null, status: 'unknown' };

function reply(quote: Record<string, unknown>) {
	mocks.put.mockImplementation(async (_url: string, body: { choice: object; remember: boolean; origin: string }) => ({
		kind: 'image',
		choice: { ...body.choice, origin: body.origin },
		remembered: body.remember,
		...quote
	}));
}

beforeEach(() => {
	vi.clearAllMocks();
	generationChoices.remembered = {};
	generationChoices.last = {};
});

describe('generationChoices.save', () => {
	it('returns the full save response and keeps the last/remembered semantics', async () => {
		const quote = {
			price: UNKNOWN,
			priced: false,
			quote_error: null,
			unattended_skip_reason: 'stored_choice_unpriced',
			warning: 'This saved choice has no verified price. Unattended runs will skip it until a price is available.'
		};
		reply(quote);
		const saved = await generationChoices.save('image', { ...PICK, origin: 'card' }, false);
		expect(mocks.put).toHaveBeenCalledWith('/generation/choices/image', { choice: PICK, remember: false, origin: 'card' });
		expect(saved).toEqual({ kind: 'image', choice: { ...PICK, origin: 'card' }, remembered: false, ...quote });
		expect(generationChoices.last.image).toEqual({ ...PICK, origin: 'card' });
		expect(generationChoices.remembered.image).toBeUndefined();

		reply({ ...quote, priced: true, unattended_skip_reason: null, warning: null });
		await generationChoices.save('image', PICK, true, 'settings');
		expect(generationChoices.remembered.image).toEqual({ ...PICK, origin: 'settings' });
		expect(generationChoices.last.image).toEqual({ ...PICK, origin: 'settings' });
		// Saving never quotes or submits anything by itself.
		expect(mocks.post).not.toHaveBeenCalled();
	});
});

describe('saveOutcome', () => {
	it('presents each save response the server returns', () => {
		const priced = saveOutcome(
			{ price: { currency: 'USD', unit: 'item', amount: '0.04', total: '0.04', status: 'quoted' }, priced: true, quote_error: null, unattended_skip_reason: null, warning: null },
			'image'
		);
		expect(priced).toEqual({ state: 'priced', tag: 'Priced', figure: '$0.04', note: 'per image', warning: null });
		const included = saveOutcome({ price: { currency: 'USD', unit: 'item', amount: '0', total: '0', status: 'included' }, priced: true, warning: null }, 'video');
		expect(included?.figure).toBe('Included');
		const unpriced = saveOutcome({ price: UNKNOWN, priced: false, unattended_skip_reason: 'stored_choice_unpriced', warning: 'No price. Unattended runs will skip it.' }, 'image');
		expect(unpriced).toMatchObject({ state: 'unpriced', figure: 'Unknown', warning: 'No price. Unattended runs will skip it.' });
		const failed = saveOutcome({ price: UNKNOWN, priced: false, quote_error: 'Timed out.', unattended_skip_reason: 'stored_choice_unavailable', warning: null }, 'image');
		expect(failed).toMatchObject({ state: 'unavailable', figure: 'No quote', warning: 'Timed out. Unattended runs will skip it.' });
		// A staged draft or an older server: no quote, so nothing is claimed.
		expect(saveOutcome(undefined, 'image')).toBeNull();
		expect(saveOutcome({}, 'image')).toBeNull();
	});
});

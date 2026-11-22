import { describe, expect, it } from 'vitest';
import { effortSpeedLabel, nextSpeed, pickedSpeed, shownSpeed, SPEED_OPTIONS, ultrafastCost } from './speed-tier';

describe('speed tier', () => {
	const on = { fastSupported: true, fastEnabled: true, ultrafastSupported: true, ultrafastEnabled: true };

	it('shows a tier only when the next turn asks for it', () => {
		expect(shownSpeed(on)).toBe('ultrafast');
		expect(shownSpeed({ ...on, ultrafastEnabled: false })).toBe('fast');
		// The model lost the tier: the label falls back with it.
		expect(shownSpeed({ ...on, ultrafastSupported: false })).toBe('fast');
		expect(shownSpeed({ ...on, fastSupported: false })).toBe('standard');
		expect(shownSpeed({ ...on, fastEnabled: false })).toBe('standard');
		// The chat's own setting has not loaded yet.
		expect(shownSpeed({ ...on, fastPending: true })).toBe('standard');
	});

	it('picks the chat\'s own tier for the picker', () => {
		expect(pickedSpeed(on)).toBe('ultrafast');
		expect(pickedSpeed({ ...on, ultrafastEnabled: false })).toBe('fast');
		expect(pickedSpeed({ ...on, fastEnabled: false })).toBe('standard');
	});

	it('moves the corner bolt one tier on and wraps to Standard', () => {
		expect(nextSpeed('standard', true)).toBe('fast');
		expect(nextSpeed('fast', true)).toBe('ultrafast');
		expect(nextSpeed('ultrafast', true)).toBe('standard');
		// Without Ultrafast the bolt is a plain toggle.
		expect(nextSpeed('standard', false)).toBe('fast');
		expect(nextSpeed('fast', false)).toBe('standard');
		expect(nextSpeed('ultrafast', false)).toBe('standard');
	});

	it('names the tier in the accessible label', () => {
		expect(effortSpeedLabel('Effort: ', 'High', 'standard')).toBe('Effort: High');
		expect(effortSpeedLabel('Effort: ', 'High', 'fast')).toBe('Effort: High, Fast');
		expect(effortSpeedLabel('Effort: ', 'High', 'ultrafast')).toBe('Effort: High, Ultrafast');
	});

	it('states the Ultrafast price in the terms the row bills in', () => {
		const ultra = SPEED_OPTIONS.find((o) => o.value === 'ultrafast')!;
		expect(ultra.title).toMatch(/fastest tier.*much faster/);
		// A ChatGPT sign-in pays with its plan allowance; an API key pays a price.
		expect(ultrafastCost('plan')).toBe('');
		expect(ultrafastCost('plan', true)).toMatch(/8x the plan allowance/);
		expect(ultrafastCost('api')).toMatch(/fastest tier.*several times the Standard API price/);
		expect(ultrafastCost('api', true)).toMatch(/several times the Standard price/);
		for (const short of [false, true]) {
			expect(ultrafastCost('plan', short)).not.toMatch(/price/);
			expect(ultrafastCost('api', short)).not.toMatch(/allowance/);
		}
	});
});

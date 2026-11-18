import { describe, expect, it } from 'vitest';
import { effortSpeedLabel, pickedSpeed, shownSpeed, SPEED_OPTIONS, ULTRAFAST_COST } from './speed-tier';

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

	it('names the tier in the accessible label', () => {
		expect(effortSpeedLabel('Effort: ', 'High', 'standard')).toBe('Effort: High');
		expect(effortSpeedLabel('Effort: ', 'High', 'fast')).toBe('Effort: High, Fast');
		expect(effortSpeedLabel('Effort: ', 'High', 'ultrafast')).toBe('Effort: High, Ultrafast');
	});

	it('states the Ultrafast price up front', () => {
		const ultra = SPEED_OPTIONS.find((o) => o.value === 'ultrafast')!;
		expect(ultra.title).toContain(ULTRAFAST_COST);
		expect(ULTRAFAST_COST).toMatch(/fastest tier/);
		expect(ULTRAFAST_COST).toMatch(/8x on ChatGPT plans/);
		expect(ULTRAFAST_COST).toMatch(/several times the API price/);
	});
});

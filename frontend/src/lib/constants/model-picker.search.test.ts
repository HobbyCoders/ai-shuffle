import { describe, expect, it } from 'vitest';
import { matchesPickerQuery, type PickerOption } from './model-picker';

const option = (label: string, description: string) =>
	({ value: label, label, description, group: 'Claude', groupKey: 'c', groupSubtext: null }) as unknown as PickerOption;

describe('matchesPickerQuery (#1220)', () => {
	const opus = option('Opus', 'Deep reasoning for hard problems');
	const sonnet = option('Sonnet', 'Balanced speed and depth');

	it('finds a model by part of its name', () => {
		expect(matchesPickerQuery(sonnet, 'son')).toBe(true);
	});

	it('does not match the middle of a word in a description', () => {
		expect(matchesPickerQuery(opus, 'son')).toBe(false);
	});

	it('still finds a description word, including a phrase', () => {
		expect(matchesPickerQuery(opus, 'reason')).toBe(true);
		expect(matchesPickerQuery(opus, 'hard prob')).toBe(true);
	});

	it('still matches the provider group anywhere', () => {
		expect(matchesPickerQuery(opus, 'laud')).toBe(true);
	});
});

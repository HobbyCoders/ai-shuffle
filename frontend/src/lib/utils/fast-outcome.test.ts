import { describe, expect, it } from 'vitest';
import { fastAppliesNotice, fastOutcomeNoteView } from './fast-outcome';

describe('fastOutcomeNoteView', () => {
	it('is only for fast_outcome rows', () => {
		expect(fastOutcomeNoteView('model_rerouted', { content: 'x', outcome: 'fast' })).toBeNull();
		expect(fastOutcomeNoteView(undefined, {})).toBeNull();
	});
	it('shows the worker sentence and its outcome', () => {
		expect(fastOutcomeNoteView('fast_outcome', { content: 'Ran on Fast.', outcome: 'fast' })).toEqual({
			outcome: 'fast',
			text: 'Ran on Fast.'
		});
	});
	it('falls back to the row content, and never renders an empty note', () => {
		expect(fastOutcomeNoteView('fast_outcome', { outcome: 'standard' }, 'Ran at standard speed.')?.text).toBe('Ran at standard speed.');
		expect(fastOutcomeNoteView('fast_outcome', { outcome: 'standard' })).toBeNull();
	});
	it('treats an outcome it does not know as unknown', () => {
		expect(fastOutcomeNoteView('fast_outcome', { content: 'x', outcome: 'warp' })?.outcome).toBe('unknown');
	});
});

describe('fastAppliesNotice', () => {
	it('says when the setting starts or stops', () => {
		expect(fastAppliesNotice(true)).toMatch(/starts with your next message/);
		expect(fastAppliesNotice(false)).toMatch(/stops with your next message/);
	});
});

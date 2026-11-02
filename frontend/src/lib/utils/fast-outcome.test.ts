import { describe, expect, it } from 'vitest';
import { fastAppliesNotice, fastChangeWaits, fastOutcomeNoteView } from './fast-outcome';

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
	it('says when the setting changes and claims no speed for the turn', () => {
		expect(fastAppliesNotice(true)).toBe('Fast mode turns on with your next message. This turn finishes at its current speed.');
		expect(fastAppliesNotice(false)).toBe('Fast mode turns off with your next message. This turn finishes at its current speed.');
	});
});

describe('fastChangeWaits', () => {
	const state = (over = {}) => ({ isStreaming: true, liveTurnId: 't1', fastAppliesTurnId: 't1', ...over });

	it('holds while the turn the worker named is the one running', () => {
		expect(fastChangeWaits(state())).toBe(true);
	});

	it('ends when that turn is over or another is running', () => {
		expect(fastChangeWaits(state({ isStreaming: false }))).toBe(false);
		expect(fastChangeWaits(state({ liveTurnId: 't2' }))).toBe(false);
		expect(fastChangeWaits(state({ liveTurnId: null }))).toBe(false);
	});

	it('is false when the worker named nothing', () => {
		expect(fastChangeWaits(state({ fastAppliesTurnId: null, liveTurnId: null }))).toBe(false);
		expect(fastChangeWaits({ isStreaming: true, liveTurnId: 't1' })).toBe(false);
	});
});

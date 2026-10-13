import { describe, expect, it } from 'vitest';
import { SttTranscript } from './sttTranscript';

describe('live transcript reconciliation', () => {
	it('shows actual deltas before a final and replaces the provisional turn', () => {
		const transcript = new SttTranscript();
		transcript.delta('Hello wor', 'a');
		expect(transcript.getText()).toBe('Hello wor');
		transcript.final('Hello world.', 'a');
		expect(transcript.getText()).toBe('Hello world.');
	});
	it('keeps overlapping turns in speech order even when finals arrive backwards', () => {
		const transcript = new SttTranscript();
		transcript.start('a');
		transcript.start('b');
		transcript.delta('First', 'a');
		transcript.delta('Second', 'b');
		transcript.final('Second sentence.', 'b');
		expect(transcript.getText()).toBe('First Second sentence.');
		transcript.final('First sentence.', 'a');
		expect(transcript.getText()).toBe('First sentence. Second sentence.');
	});
	it('uses commit predecessor order and tolerates duplicate finals or late deltas', () => {
		const transcript = new SttTranscript();
		transcript.final('Second.', 'b');
		transcript.start('b', 'a');
		transcript.final('First.', 'a');
		transcript.final('Second.', 'b');
		transcript.delta(' ignored', 'b');
		expect(transcript.getText()).toBe('First. Second.');
	});
	it('clears an empty final without deleting another in-flight turn', () => {
		const transcript = new SttTranscript();
		transcript.delta('Noise', 'a');
		transcript.delta('Keep this', 'b');
		transcript.final('', 'a');
		expect(transcript.getText()).toBe('Keep this');
	});
	it('supports older proxies without item IDs', () => {
		const transcript = new SttTranscript();
		transcript.delta('Hello');
		transcript.final('Hello.');
		transcript.delta('World');
		expect(transcript.getText()).toBe('Hello. World');
	});
});

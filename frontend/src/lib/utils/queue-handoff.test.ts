import { describe, expect, it } from 'vitest';
import { landedFromQueue } from './queue-handoff';

const mark = (messages: number, queued: number, sessionId: string | null = 's1') => ({ sessionId, messages, queued });

describe('landedFromQueue (#1052)', () => {
	it('is true when a row lands and a bubble leaves in one commit', () => {
		expect(landedFromQueue(mark(2, 3), mark(3, 2))).toBe(true);
		expect(landedFromQueue(mark(6, 1), mark(7, 0))).toBe(true);
	});

	it('is false for the two halves seen in separate commits', () => {
		expect(landedFromQueue(mark(2, 3), mark(2, 2))).toBe(false);
		expect(landedFromQueue(mark(2, 2), mark(3, 2))).toBe(false);
	});

	it('is false for a send, a reply, a chat switch or the first commit', () => {
		expect(landedFromQueue(mark(2, 2), mark(2, 3))).toBe(false);
		expect(landedFromQueue(mark(3, 2), mark(4, 2))).toBe(false);
		expect(landedFromQueue(mark(2, 3), mark(9, 0, 's2'))).toBe(false);
		expect(landedFromQueue(null, mark(3, 2))).toBe(false);
	});
});

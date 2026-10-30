import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '../../types';
import {
	dismissCapturedProse,
	reduceAll,
	rewindCapturedMessage,
	type WireEvent
} from './reducer';

const prose: ChatMessage = {
	id: 'prose', role: 'assistant', type: 'text', content: 'May I proceed?', seq: 4
};

function event(seq: number, type: string, fields: Record<string, unknown> = {}): WireEvent {
	return {
		id: `event-${seq}`, seq, type, session_id: 'session',
		created_at: '2026-10-30T00:00:00.000Z', ...fields
	};
}

describe('shared captured-message effects', () => {
	it('uses the captured origin, falling back to the row sequence only for dismissal', () => {
		expect(dismissCapturedProse(prose, 2, 3, 8).proseQuestionDismissedAt).toBe(8);
		expect(dismissCapturedProse(prose, undefined, 3, 8)).toBe(prose);
		expect(dismissCapturedProse(prose, undefined, 4, 8).proseQuestionDismissedAt).toBe(8);
		const optimistic = { ...prose, seq: undefined };
		expect(dismissCapturedProse(optimistic, undefined, 100, 101)).toBe(optimistic);
	});

	it('preserves the first qualifying dismissal without mutating a captured row', () => {
		const first = dismissCapturedProse(prose, 4, 5, 6);
		expect(dismissCapturedProse(first, 4, 9, 10).proseQuestionDismissedAt).toBe(6);
		expect(prose.proseQuestionDismissedAt).toBeUndefined();
		expect(dismissCapturedProse(prose, 12, 9, 10)).toBe(prose);
	});

	it('does not dismiss user messages or explicit assistant cards', () => {
		for (const row of [
			{ ...prose, role: 'user' as const },
			{ ...prose, type: 'tool_use' as const },
			{ ...prose, type: 'thinking' as const },
			{ ...prose, type: 'subagent' as const }
		]) {
			expect(dismissCapturedProse(row, 4, 9, 10)).toBe(row);
		}
	});

	it('drops only captured rows at or after the rewind, never optimistic rows', () => {
		expect(rewindCapturedMessage(prose, 6, 6)).toBeNull();
		expect(rewindCapturedMessage(prose, 7, 6)).toBeNull();
		expect(rewindCapturedMessage(prose, 4, 6)).toBe(prose);
		expect(rewindCapturedMessage(prose, undefined, 1)).toBe(prose);
	});

	it('undoes later dismissals and allows a new first dismissal after rewind', () => {
		const dismissed = dismissCapturedProse(prose, 4, 5, 8);
		const restored = rewindCapturedMessage(dismissed, 4, 6)!;
		expect(restored).toEqual(prose);
		expect(dismissed.proseQuestionDismissedAt).toBe(8);
		expect(dismissCapturedProse(restored, 4, 9, 10).proseQuestionDismissedAt).toBe(10);
		const early = { ...prose, proseQuestionDismissedAt: 5 };
		expect(rewindCapturedMessage(early, 4, 6)).toBe(early);
	});

	it('hydrates detached rows identically to chronological full replay across dismissal and rewind', () => {
		const prefix = [
			event(1, 'message.user', { content: 'First' }),
			event(2, 'assistant.text_delta', { text: 'May I proceed?' }),
			event(3, 'tool.use', { tool_use_id: 'tool', name: 'Bash', input: { command: 'pwd' } }),
			event(4, 'tool.result', { tool_use_id: 'tool', content: 'done' }),
			event(5, 'message.user', { content: 'Second' }),
			event(6, 'assistant.text_delta', { text: 'Later question?' }),
			event(7, 'turn.completed')
		];
		const captured = reduceAll(prefix);
		const tail = [
			event(8, 'system.note', { subtype: 'session_settled', through_seq: 7 }),
			event(9, 'transcript.rewound', { from_seq: 6 }),
			event(10, 'system.note', { subtype: 'session_settled', through_seq: 9 })
		];
		const full = reduceAll([...prefix, ...tail]);
		const carry = reduceAll(tail, { ...captured, messages: [], messageOriginSeq: new Map() });
		const rows = captured.messages.flatMap((row) => {
			const origin = captured.messageOriginSeq.get(row.id);
			const dismissed = dismissCapturedProse(row, origin, 7, 8);
			const kept = rewindCapturedMessage(dismissed, origin, 6);
			return kept ? [dismissCapturedProse(kept, origin, 9, 10)] : [];
		});
		expect([...rows, ...carry.messages]).toEqual(full.messages);
		expect(carry.messages.map((row) => row.id)).toEqual(['event-10']);
		expect(new Map([
			...rows.map((row): [string, number] => [row.id, captured.messageOriginSeq.get(row.id)!]),
			...carry.messageOriginSeq
		])).toEqual(full.messageOriginSeq);
		expect(carry.lastSeq).toBe(full.lastSeq);
		expect(rows.find((row) => row.content === 'May I proceed?')?.proseQuestionDismissedAt).toBe(10);
		expect(rows.some((row) => row.content === 'Later question?')).toBe(false);
		expect(captured.messages.every((row) => row.proseQuestionDismissedAt === undefined)).toBe(true);
	});
});

import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { pausedChat, pausedLabel, pausedWorkers, resumeTime } from './limit-resume';

let n = 0;
function row(data: Record<string, unknown>): ChatMessage {
	return { id: `m${n++}`, role: 'system', type: 'system', content: '', systemSubtype: 'limit_resume', systemData: data };
}
function user(text = 'hi'): ChatMessage {
	return { id: `m${n++}`, role: 'user', type: 'text', content: text };
}
function assistant(): ChatMessage {
	return { id: `m${n++}`, role: 'assistant', type: 'text', content: 'ok' };
}

const AT = Date.UTC(2026, 9, 5, 3, 10) / 1000;

describe('pausedChat', () => {
	it('reads the newest scheduled row', () => {
		expect(pausedChat([user(), assistant(), row({ status: 'scheduled', resume_at: AT })])).toEqual({ resumeAt: AT });
	});

	it('follows a deferral to its new time', () => {
		const later = AT + 3600;
		const messages = [row({ status: 'scheduled', resume_at: AT }), row({ status: 'deferred', resume_at: later })];
		expect(pausedChat(messages)?.resumeAt).toBe(later);
	});

	it.each(['resumed', 'cancelled', 'dropped', 'no_reset'])('ends on %s', (status) => {
		expect(pausedChat([row({ status: 'scheduled', resume_at: AT }), row({ status })])).toBeNull();
	});

	it('ends when the owner sends a message after it', () => {
		expect(pausedChat([row({ status: 'scheduled', resume_at: AT }), user('back now')])).toBeNull();
	});

	it("ignores a worker's rows", () => {
		expect(pausedChat([row({ status: 'scheduled', resume_at: AT, task_id: 'w1' })])).toBeNull();
	});
});

describe('pausedWorkers', () => {
	it('keeps each worker’s newest open row', () => {
		const messages = [
			row({ status: 'scheduled', resume_at: AT, task_id: 'w1' }),
			row({ status: 'scheduled', resume_at: AT, task_id: 'w2' }),
			row({ status: 'resumed', task_id: 'w2' }),
			user()
		];
		expect([...pausedWorkers(messages)]).toEqual([['w1', AT]]);
	});
});

describe('labels', () => {
	it('says the time plainly, with the weekday when it is not today', () => {
		const at = new Date(2026, 9, 4, 23, 10);
		const today = new Date(2026, 9, 4, 18, 0);
		expect(resumeTime(at.getTime() / 1000, today)).toMatch(/^11:10\s?pm$|^23:10$/);
		expect(resumeTime(at.getTime() / 1000, new Date(2026, 9, 3, 18, 0))).toMatch(/\S+ (11:10\s?pm|23:10)$/);
		expect(pausedLabel(at.getTime() / 1000, today)).toMatch(/^Paused: usage limit\. Resumes at /);
	});
});

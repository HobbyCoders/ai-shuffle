import { describe, expect, it } from 'vitest';
import { chatChangedDuringBoot } from './boot-landing';

describe('chatChangedDuringBoot (#811)', () => {
	it('is a change when a send during the loads created a chat', () => {
		expect(chatChangedDuringBoot(null, { sessionId: 'created-by-send', messages: [{}] })).toBe(true);
	});

	it('is a change while a first send waits for its chat to be created', () => {
		expect(chatChangedDuringBoot(null, { sessionId: null, messages: [{ role: 'user' }] })).toBe(true);
	});

	it('is a change when the user opened another chat during the loads', () => {
		expect(chatChangedDuringBoot('restored', { sessionId: 'opened', messages: [] })).toBe(true);
	});

	it('is no change for the session init restored, or an untouched new chat', () => {
		expect(chatChangedDuringBoot('restored', { sessionId: 'restored', messages: [{}, {}] })).toBe(false);
		expect(chatChangedDuringBoot(null, { sessionId: null, messages: [] })).toBe(false);
		expect(chatChangedDuringBoot(null, { sessionId: null })).toBe(false);
		expect(chatChangedDuringBoot(null, null)).toBe(false);
	});
});

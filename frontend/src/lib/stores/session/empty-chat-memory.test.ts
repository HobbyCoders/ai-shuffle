import { beforeEach, describe, expect, it, vi } from 'vitest';
import { rememberEmptyChat, wasLastSeenEmpty } from './empty-chat-memory';

describe('empty-chat memory', () => {
	beforeEach(() => sessionStorage.clear());

	it('remembers the one chat last seen empty, until a settled chat replaces or clears it', () => {
		expect(wasLastSeenEmpty('a')).toBe(false);
		rememberEmptyChat('a');
		expect(wasLastSeenEmpty('a')).toBe(true);
		expect(wasLastSeenEmpty('b')).toBe(false);
		rememberEmptyChat('b');
		expect(wasLastSeenEmpty('a')).toBe(false);
		rememberEmptyChat(null);
		expect(wasLastSeenEmpty('b')).toBe(false);
	});

	it('has no answer for an unbound chat', () => {
		rememberEmptyChat('a');
		expect(wasLastSeenEmpty(null)).toBe(false);
		expect(wasLastSeenEmpty(undefined)).toBe(false);
	});

	it('falls back to the docked first layout when storage fails', () => {
		const read = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied'); });
		const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
		expect(() => rememberEmptyChat('a')).not.toThrow();
		expect(wasLastSeenEmpty('a')).toBe(false);
		read.mockRestore();
		write.mockRestore();
	});
});

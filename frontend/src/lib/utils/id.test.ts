import { afterEach, describe, expect, it, vi } from 'vitest';
import { randomUUID } from './id';

afterEach(() => vi.unstubAllGlobals());

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe('randomUUID', () => {
	it('uses the native method with its crypto receiver when available', () => {
		const native = vi.fn(function (this: unknown) {
			expect(this).toBe(globalThis.crypto);
			return 'native-uuid';
		});
		vi.stubGlobal('crypto', { randomUUID: native });
		expect(randomUUID()).toBe('native-uuid');
		expect(native).toHaveBeenCalledOnce();
	});

	it.each(['missing', 'throwing'])('makes UUID v4 from fresh random bytes with %s randomUUID', (mode) => {
		let value = 0;
		const getRandomValues = vi.fn((bytes: Uint8Array) => {
			for (let i = 0; i < bytes.length; i++) bytes[i] = value++;
			return bytes;
		});
		vi.stubGlobal('crypto', {
			getRandomValues,
			randomUUID: mode === 'missing' ? undefined : () => { throw new Error('Insecure context'); }
		});
		const first = randomUUID();
		const second = randomUUID();
		expect(first).toBe('00010203-0405-4607-8809-0a0b0c0d0e0f');
		expect(second).toBe('10111213-1415-4617-9819-1a1b1c1d1e1f');
		expect(first).toMatch(UUID_V4);
		expect(second).toMatch(UUID_V4);
		expect(getRandomValues).toHaveBeenCalledTimes(2);
	});
});

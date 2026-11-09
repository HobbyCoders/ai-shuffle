/** The device key's two promises: it never exists in a weaker form, and its code matches the worker's. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { deviceKeyProblem, linkCode, NO_DEVICE_KEY } from './device-key';

afterEach(() => vi.unstubAllGlobals());

describe('deviceKeyProblem', () => {
	it('refuses plain HTTP, with no fallback', () => {
		vi.stubGlobal('isSecureContext', false);
		expect(deviceKeyProblem()).toBe(NO_DEVICE_KEY);
	});

	it('refuses a secure page with no key store', () => {
		vi.stubGlobal('isSecureContext', true);
		vi.stubGlobal('indexedDB', undefined);
		expect(deviceKeyProblem()).toBe(NO_DEVICE_KEY);
	});

	it('allows a secure page with WebCrypto and IndexedDB', () => {
		vi.stubGlobal('isSecureContext', true);
		vi.stubGlobal('indexedDB', {});
		expect(deviceKeyProblem()).toBeNull();
	});
});

describe('linkCode', () => {
	it('matches the code app/network/terminal_device_links.py works out for the same key', async () => {
		// base32(sha256(b"ai-shuffle-terminal-link-code-v1:" + bytes(range(65)))[:8])[:12]
		expect(await linkCode(Uint8Array.from({ length: 65 }, (_, i) => i))).toBe('RBGUKOSK5R4L');
	});
});

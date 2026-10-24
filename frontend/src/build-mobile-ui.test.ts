import { describe, expect, it, vi } from 'vitest';
import { buildUIs } from '../scripts/build-mobile-ui.mjs';

function deferred() {
	let resolve!: () => void;
	const promise = new Promise<void>((done) => { resolve = done; });
	return { promise, resolve };
}

describe('UI build ordering', () => {
	it('waits for the entire desktop build, including late syncs, before starting mobile', async () => {
		const desktop = deferred();
		const phone = deferred();
		const mobile = vi.fn(() => phone.promise);
		const result = buildUIs(() => desktop.promise, mobile);
		await Promise.resolve();
		expect(mobile).not.toHaveBeenCalled();
		desktop.resolve();
		await vi.waitFor(() => expect(mobile).toHaveBeenCalledOnce());
		let finished = false;
		void result.then(() => { finished = true; });
		await Promise.resolve();
		expect(finished).toBe(false);
		phone.resolve();
		await result;
	});

	it('fails immediately without starting mobile when desktop fails', async () => {
		const mobile = vi.fn();
		await expect(buildUIs(() => Promise.reject(new Error('desktop failed')), mobile))
			.rejects.toThrow('desktop failed');
		expect(mobile).not.toHaveBeenCalled();
	});

	it('propagates a mobile build failure', async () => {
		await expect(buildUIs(() => Promise.resolve(), () => Promise.reject(new Error('mobile failed'))))
			.rejects.toThrow('mobile failed');
	});
});

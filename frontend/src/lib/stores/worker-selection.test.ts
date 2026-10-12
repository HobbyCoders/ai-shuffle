import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getPersistedDeviceIntent, setPersistedDeviceIntent } from './worker-selection';

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe('worker selection persistence', () => {
	it('defaults to Local and remembers a worker through a module reload', async () => {
		expect(getPersistedDeviceIntent()).toBeNull();
		const pick = { id: 'runner-1', name: 'ServerWorker' };
		setPersistedDeviceIntent(pick);
		vi.resetModules();
		const reloaded = await import('./worker-selection');
		expect(reloaded.getPersistedDeviceIntent()).toEqual(pick);
	});

	it('remembers an explicit return to Local', () => {
		setPersistedDeviceIntent({ id: 'runner-1', name: 'ServerWorker' });
		setPersistedDeviceIntent(null);
		expect(getPersistedDeviceIntent()).toBeNull();
	});

	it.each(['broken', '[]', '{}', '42', '{"id":4,"name":"runner"}', '{"id":"","name":"runner"}'])
	('ignores malformed stored selection %s', value => {
		localStorage.setItem('aihub_workerSelection', value);
		expect(getPersistedDeviceIntent()).toBeNull();
	});

	it('tolerates blocked storage', () => {
		vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
		vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
		expect(getPersistedDeviceIntent()).toBeNull();
		expect(() => setPersistedDeviceIntent({ id: 'runner', name: 'Runner' })).not.toThrow();
	});
});

import { describe, expect, it } from 'vitest';
import type { Install } from '$lib/api/installs';
import { selfWorkerName, workerDisplayName } from './worker-name';

const install = (device_name: string, is_self: boolean) => ({ device_name, is_self }) as Install;

describe('worker names', () => {
	it('keeps a name somebody chose', () => {
		expect(workerDisplayName('Unraid')).toBe('Unraid');
		expect(selfWorkerName([install('Unraid', true)])).toBe('Unraid');
	});

	it('never shows a Docker container ID', () => {
		expect(workerDisplayName('75c733a3514a')).toBe('Worker 75c7');
		expect(selfWorkerName([install('75c733a3514a', true)])).toBe('Local');
	});

	it('calls this worker Local until it knows better', () => {
		expect(selfWorkerName(null)).toBe('Local');
		expect(selfWorkerName([install('Other', false)])).toBe('Local');
	});
});

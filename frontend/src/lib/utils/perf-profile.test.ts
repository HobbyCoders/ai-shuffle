import { describe, it, expect } from 'vitest';
import { LOW_END_CORES, LOW_END_MEMORY_GB, isLowEndDevice } from './perf-profile';

describe('isLowEndDevice', () => {
	it('treats an unknown machine as capable', () => {
		expect(isLowEndDevice({})).toBe(false);
		expect(isLowEndDevice({ hardwareConcurrency: null, deviceMemoryGb: null })).toBe(false);
	});

	it('flags low core counts', () => {
		expect(isLowEndDevice({ hardwareConcurrency: LOW_END_CORES })).toBe(true);
		expect(isLowEndDevice({ hardwareConcurrency: LOW_END_CORES + 1 })).toBe(false);
	});

	it('flags low memory', () => {
		expect(isLowEndDevice({ hardwareConcurrency: 12, deviceMemoryGb: LOW_END_MEMORY_GB })).toBe(
			true
		);
		expect(isLowEndDevice({ hardwareConcurrency: 12, deviceMemoryGb: 8 })).toBe(false);
	});

	it('honours an explicit data-saver request', () => {
		expect(isLowEndDevice({ hardwareConcurrency: 32, deviceMemoryGb: 32, saveData: true })).toBe(
			true
		);
	});

	it('ignores nonsense hints rather than guessing', () => {
		expect(isLowEndDevice({ hardwareConcurrency: 0, deviceMemoryGb: 0 })).toBe(false);
	});
});

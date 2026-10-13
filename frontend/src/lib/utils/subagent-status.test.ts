import { describe, expect, it } from 'vitest';
import { isSubagentRunning } from './subagent-status';

describe('isSubagentRunning', () => {
	it('is running when agentStatus is "running" (no registry needed)', () => {
		expect(isSubagentRunning('running', undefined, undefined)).toBe(true);
		expect(isSubagentRunning('running', 'toolu_x', new Set())).toBe(true);
	});

	it('forces running when the launch tool id matches a running active task', () => {
		const running = new Set(['toolu_a', 'toolu_b']);
		// server says completed, but the registry still lists the task as running
		expect(isSubagentRunning('completed', 'toolu_a', running)).toBe(true);
		expect(isSubagentRunning('error', 'toolu_b', running)).toBe(true);
	});

	it('falls back to agentStatus when there is no registry match', () => {
		const running = new Set(['toolu_a']);
		expect(isSubagentRunning('completed', 'toolu_other', running)).toBe(false);
		expect(isSubagentRunning('error', 'toolu_other', running)).toBe(false);
	});

	it('is not running for a non-running status with no toolId', () => {
		const running = new Set(['toolu_a']);
		expect(isSubagentRunning('completed', undefined, running)).toBe(false);
	});

	it('undefined status + no match renders completed (legacy transcripts)', () => {
		expect(isSubagentRunning(undefined, undefined, undefined)).toBe(false);
		expect(isSubagentRunning(undefined, 'toolu_x', new Set())).toBe(false);
		expect(isSubagentRunning(undefined, 'toolu_x', undefined)).toBe(false);
		// unknown/absent status but a matching running task still forces running
		expect(isSubagentRunning(undefined, 'toolu_x', new Set(['toolu_x']))).toBe(true);
	});
});

import { describe, expect, it } from 'vitest';
import { CALL_ELSEWHERE_REASON, CALL_PARKED_REASON, CALL_WORKING_REASON, callBusyReason } from './call-gate';

const ready = { sessionId: 'home', isStreaming: false, activeTasks: [], pendingPermissions: [], pendingQuestions: [] };
const row = (extra: Record<string, unknown> = {}) => ({ id: 'home', is_remote: false, controllable: true, remote_runtime_state: null, has_running_tasks: false, parked_task_kind: null, ...extra }) as never;

describe('callBusyReason', () => {
	it('blocks an ordinary conversation while it works, waits on a decision or is parked', () => {
		expect(callBusyReason({ ...ready, isStreaming: true })).toBe(CALL_WORKING_REASON);
		expect(callBusyReason({ ...ready, pendingPermissions: [{}] })).toBe(CALL_WORKING_REASON);
		expect(callBusyReason(ready, row({ parked_task_kind: 'agent' }))).toBe(CALL_PARKED_REASON);
		expect(callBusyReason(ready, row())).toBeNull();
	});

	it('lets the assistant home take a call while it works, waits or is parked', () => {
		const always = { alwaysAvailable: true };
		expect(callBusyReason({ ...ready, isStreaming: true }, row(), always)).toBeNull();
		expect(callBusyReason({ ...ready, activeTasks: [{ status: 'running' } as never] }, row(), always)).toBeNull();
		expect(callBusyReason({ ...ready, pendingPermissions: [{}], pendingQuestions: [{}] }, row(), always)).toBeNull();
		expect(callBusyReason(ready, row({ parked_task_kind: 'agent', has_running_tasks: true }), always)).toBeNull();
	});

	it('still blocks the home when another device runs it', () => {
		expect(callBusyReason(ready, row({ is_remote: true, remote_runtime_state: 'working' }), { alwaysAvailable: true }))
			.toBe(CALL_ELSEWHERE_REASON);
	});
});

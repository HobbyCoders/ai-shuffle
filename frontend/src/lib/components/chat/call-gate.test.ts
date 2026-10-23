import { describe, expect, it } from 'vitest';
import { CALL_ELSEWHERE_REASON, callBusyReason } from './call-gate';

const ready = { sessionId: 'chat', isStreaming: false, activeTasks: [], pendingPermissions: [], pendingQuestions: [] };
const row = (extra: Record<string, unknown> = {}) => ({ id: 'chat', is_remote: false, controllable: true, remote_runtime_state: null, has_running_tasks: false, parked_task_kind: null, ...extra }) as never;

describe('callBusyReason', () => {
	it('lets every conversation take a call while it works, waits or is parked', () => {
		expect(callBusyReason(ready, row())).toBeNull();
		expect(callBusyReason({ ...ready, isStreaming: true }, row())).toBeNull();
		expect(callBusyReason({ ...ready, activeTasks: [{ status: 'running' } as never] }, row())).toBeNull();
		expect(callBusyReason({ ...ready, pendingPermissions: [{}], pendingQuestions: [{}] }, row())).toBeNull();
		expect(callBusyReason(ready, row({ parked_task_kind: 'agent', has_running_tasks: true }))).toBeNull();
	});

	it('blocks a conversation another device runs', () => {
		expect(callBusyReason(ready, row({ is_remote: true, remote_runtime_state: 'working' }))).toBe(CALL_ELSEWHERE_REASON);
	});
});

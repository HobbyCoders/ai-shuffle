import { describe, expect, it } from 'vitest';
import { callBusyReason, freshCallReason } from './call-gate';

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

	it('lets a conversation another worker runs take a call while that worker is online (#700)', () => {
		expect(callBusyReason(ready, row({ is_remote: true, remote_runtime_state: 'working' }))).toBeNull();
	});

	it('names an offline owning worker', () => {
		expect(callBusyReason(ready, row({ is_remote: true, controllable: false, device_name: 'Office PC' })))
			.toMatch(/^Office PC is offline/);
		expect(callBusyReason(ready, row({ is_remote: true, controllable: false })))
			.toMatch(/^The worker that runs this conversation is offline/);
	});
});

describe('freshCallReason', () => {
	it('lets a picked runner take the new conversation (#700)', () => {
		expect(freshCallReason({ isStreaming: false, isCreatingWorktree: false })).toBeNull();
	});

	it('waits while a first message or worktree is creating the session', () => {
		expect(freshCallReason({ isStreaming: true, isCreatingWorktree: false })).toMatch(/once this session is created/);
		expect(freshCallReason({ isStreaming: false, isCreatingWorktree: true })).toMatch(/once this session is created/);
	});
});

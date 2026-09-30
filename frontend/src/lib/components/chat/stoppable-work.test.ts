import { describe, expect, it } from 'vitest';
import { hasStoppableWork } from './stoppable-work';
import type { Task } from '$lib/stores/types';

const idle = { sessionId: 'remote-session', isStreaming: false, activeTasks: [] as Task[] };
const remote = { id: 'remote-session', is_remote: true, controllable: true };

describe('session Stop visibility', () => {
	it.each(['working', 'parked', 'permission', 'question'] as const)(
		'exposes Stop for an owner-reported %s session without faking streaming', remote_runtime_state => {
			expect(hasStoppableWork(idle, { ...remote, remote_runtime_state })).toBe(true);
			expect(idle.isStreaming).toBe(false);
		}
	);
	it.each(['idle', 'offline', 'unknown', null, undefined] as const)(
		'does not invent work from a %s summary', remote_runtime_state => {
			expect(hasStoppableWork(idle, { ...remote, remote_runtime_state })).toBe(false);
		}
	);
	it('follows owner transitions back to idle', () => {
		expect(hasStoppableWork(idle, { ...remote, remote_runtime_state: 'working' })).toBe(true);
		expect(hasStoppableWork(idle, { ...remote, remote_runtime_state: 'idle' })).toBe(false);
	});
	it('does not leak another session or offline/local summaries into the composer', () => {
		const working = { ...remote, remote_runtime_state: 'working' as const };
		expect(hasStoppableWork(idle, { ...working, id: 'other' })).toBe(false);
		expect(hasStoppableWork(idle, { ...working, controllable: false })).toBe(false);
		expect(hasStoppableWork(idle, { ...working, is_remote: false })).toBe(false);
		expect(hasStoppableWork({ ...idle, sessionId: null }, working)).toBe(false);
	});
	it('uses durable task presence for this session even when its local task feed has not hydrated', () => {
		expect(hasStoppableWork(idle, { id: idle.sessionId, is_remote: false, has_running_tasks: true })).toBe(true);
		expect(hasStoppableWork(idle, { id: 'other', is_remote: false, has_running_tasks: true })).toBe(false);
		expect(hasStoppableWork(idle, { id: idle.sessionId, controllable: false, has_running_tasks: true })).toBe(false);
	});
	it('preserves foreground Stop including a not-yet-created session', () => {
		expect(hasStoppableWork({ ...idle, isStreaming: true })).toBe(true);
		expect(hasStoppableWork({ ...idle, sessionId: null, isStreaming: true })).toBe(true);
	});
	it('keeps Stop available for running background work after streaming ends', () => {
		const task = { status: 'running' } as Task;
		expect(hasStoppableWork({ ...idle, activeTasks: [task] })).toBe(true);
		for (const status of ['completed', 'error', 'killed'] as const) {
			expect(hasStoppableWork({ ...idle, activeTasks: [{ ...task, status }] })).toBe(false);
		}
	});
});

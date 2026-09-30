import type { Session } from '$lib/api/sessions';
import type { Task } from '$lib/stores/types';

/** Stop is session-scoped: foreground streaming is not the only live work.
 * Network runtime summaries deliberately do not populate local execution tables
 * or mutate the transcript reducer. Use them only to expose the control. */
export function hasStoppableWork(
	state: { sessionId: string | null; isStreaming: boolean; activeTasks: Task[] },
	row?: Pick<Session, 'id' | 'is_remote' | 'controllable' | 'remote_runtime_state'>
): boolean {
	if (state.isStreaming || state.activeTasks.some(task => task.status === 'running')) return true;
	if (!state.sessionId || row?.id !== state.sessionId || !row.is_remote || row.controllable === false) return false;
	return row.remote_runtime_state === 'working'
		|| row.remote_runtime_state === 'parked'
		|| row.remote_runtime_state === 'permission'
		|| row.remote_runtime_state === 'question';
}

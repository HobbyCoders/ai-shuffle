/**
 * Subagent running-status resolution.
 *
 * A subagent (Task tool) card renders a green checkmark once its `agentStatus`
 * is anything other than 'running'. On fresh loads of parked sessions the
 * server-supplied status can be stale/wrong, so we cross-check against the
 * registry truth of currently-running background tasks (the `/live` payload's
 * `activeTasks`, keyed by the Task launch `tool_use` id).
 *
 * A card is considered running when EITHER the server says so, OR its launch
 * tool id matches a currently-running active task. Absent/unknown statuses with
 * no registry match resolve to NOT running, so legacy transcripts (which carry
 * no live active tasks) still render as completed.
 */
export function isSubagentRunning(
	agentStatus: string | undefined,
	toolId: string | undefined,
	runningToolIds?: Set<string>
): boolean {
	if (agentStatus === 'running') return true;
	if (toolId && runningToolIds?.has(toolId)) return true;
	return false;
}

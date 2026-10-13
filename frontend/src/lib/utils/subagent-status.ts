import type { SubagentChildMessage } from '$lib/stores/types';

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

/**
 * The worker's report split for the card (transcript-organization spec §4):
 * the first paragraph shows on the card, the rest sits behind "Full report".
 * The report is the card's own `content`, which the event reducer fills from
 * `subagent.completed` (or the error text on failure). Blank paragraphs are
 * skipped; a fenced block is never split across head and rest.
 */
export function reportParts(content: string | undefined): { head: string; rest: string } | null {
	const text = content?.trim();
	if (!text) return null;
	const lines = text.split('\n');
	let inFence = false;
	for (let i = 0; i < lines.length; i++) {
		if (/^\s*(```|~~~)/.test(lines[i])) inFence = !inFence;
		if (!inFence && !lines[i].trim()) {
			return { head: lines.slice(0, i).join('\n').trim(), rest: lines.slice(i + 1).join('\n').trim() };
		}
	}
	return { head: text, rest: '' };
}

/** The tool the running worker called last, from its progress children. */
export function latestWorkerTool(
	children: readonly SubagentChildMessage[] | undefined
): SubagentChildMessage | null {
	if (!children) return null;
	for (let i = children.length - 1; i >= 0; i--) {
		if (children[i].type === 'tool_use' && children[i].toolName) return children[i];
	}
	return null;
}

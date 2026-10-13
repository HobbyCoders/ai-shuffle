/**
 * The Git target of a chat that has not started yet: the worker picked in the
 * composer (or this one) and, optionally, a checkout branch.
 *
 * MainPanel derives it from `activeTab`, which is replaced on many unrelated
 * updates. Building a new object each time re-ran the Git load effect and
 * re-fetched status several times a second; on a failing status the Git button
 * then flashed between its error glyph and its normal state. Deriving a string
 * key first keeps the target's identity until its value really changes.
 */
import type { WorkerGitTarget } from '$lib/api/git';

export interface DraftGitTab {
	sessionId?: string | null;
	deviceIntent?: { id?: string | null } | null;
	projectV2Id?: string | null;
	worktreeId?: string | null;
	worktreeBranch?: string | null;
	selectedWorktreeBranch?: string | null;
}

/** A value key for the draft's worker Git target, or null when it has none. */
export function workerGitTargetKey(tab: DraftGitTab | null | undefined): string | null {
	if (!tab || tab.sessionId) return null;
	const scoped = tab.deviceIntent
		|| (tab.projectV2Id && !tab.worktreeId)
		|| (tab.worktreeBranch && !tab.worktreeId);
	if (!scoped) return null;
	return JSON.stringify([tab.deviceIntent?.id ?? 'local', tab.worktreeBranch || tab.selectedWorktreeBranch || null]);
}

export function workerGitTargetFromKey(key: string | null): WorkerGitTarget | null {
	if (!key) return null;
	const [workerId, branch] = JSON.parse(key) as [string, string | null];
	return { workerId, branch };
}

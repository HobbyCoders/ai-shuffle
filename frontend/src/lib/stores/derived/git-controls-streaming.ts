/** Safety gate for Git writes in the checkout/branch being viewed. */
export interface CrossDeviceStreamingInfo {
	projectId: string | null;
	worktreeId: string | null;
	worktreeBranch?: string | null;
}
export interface GitControlsStreamingInput {
	activeIsStreaming: boolean;
	activeIsWorktree: boolean;
	activeProject: string | null;
	activeProjectV2Id?: string | null;
	activeWorktreeId?: string | null;
	/** Logical branch, or the actual branch of the shared main checkout. */
	activeBranch?: string | null;
	/** Fresh owner-side busy check also covers background work and cwd aliases. */
	targetMutationsBlocked?: boolean;
	crossDeviceStreaming: ReadonlyMap<string, CrossDeviceStreamingInfo>;
}
export function computeGitControlsStreaming(input: GitControlsStreamingInput): boolean {
	if (input.activeIsStreaming || input.targetMutationsBlocked) return true;
	for (const info of input.crossDeviceStreaming.values()) {
		if (!info.projectId || ![input.activeProject, input.activeProjectV2Id].includes(info.projectId)) continue;
		if (input.activeWorktreeId && info.worktreeId === input.activeWorktreeId) return true;
		if (input.activeBranch && info.worktreeBranch === input.activeBranch) return true;
		// Two main-checkout sessions share files, even without branch metadata.
		if (!input.activeIsWorktree && !info.worktreeId && !info.worktreeBranch) return true;
		// A worktree without any identity cannot be proved independent.
		if (input.activeIsWorktree && !input.activeWorktreeId && !input.activeBranch) return true;
	}
	return false;
}

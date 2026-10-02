// Stand-in for the network-backed git store. vite.one-time-branch.config.js
// routes the production `git.svelte` import here; one-time-branch.ts drives it.
import { derived, writable } from 'svelte/store';

const initial = { projectId: 'shuffle-scratch', worktreeId: null, sessionId: null, hydrated: true,
	loading: false, status: { is_git_repo: true, current_branch: 'main' } };
const snapshot = writable<Record<string, any>>(initial);
let current: Record<string, any> = initial;
snapshot.subscribe((value) => { current = value; });

export const gitSnapshot = snapshot;
export const gitStatus = derived(snapshot, (s) => s.status);
export const gitMode = writable('project');
export const branches = writable([
	{ name: 'main', current: true, remote: false },
	{ name: 'develop', current: false, remote: false },
]);
export const worktrees = writable<Record<string, unknown>[]>([]);
export const currentBranch = writable({ name: 'main' });
export const changedFiles = writable([]);
export const changedFilesLoading = writable(false);

/** Branch names the New Branch modal created through this stub. */
export const createdBranches: string[] = [];

export const git = {
	subscribe: snapshot.subscribe,
	get current() { return current; },
	async createBranch(name: string) {
		createdBranches.push(name);
		branches.update((list) => [...list, { name, current: false, remote: false }]);
	},
	async loadBranches() {},
	async loadWorktrees() {},
	async refreshStatus() {},
	async refreshDiffStats() {},
	async refreshPrState() {},
	startBackgroundRefresh() {},
	stopBackgroundRefresh() {},
	retryLoad() {},
	refreshChangedFiles() {},
};

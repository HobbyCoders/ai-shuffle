// Stand-ins for the network-backed git, git-operation and GitHub stores, and
// for the build-time shell flag. vite.git-panel.config.js routes the production
// imports here; GitPanelFixture.svelte drives them.
import { writable } from 'svelte/store';
export * from '../src/lib/utils/platform';
import type { UiPlatform } from '../src/lib/utils/platform';

export function buildShell(): UiPlatform {
	return new URLSearchParams(location.search).get('shell') === 'mobile' ? 'mobile' : 'desktop';
}

export const gitSnapshot = writable<Record<string, unknown>>({ hydrated: true, branches: [], status: null });
export const gitMode = writable('project');
export const git = {
	startBackgroundRefresh() {},
	stopBackgroundRefresh() {},
	retryLoad() {
		(window as unknown as { gitPanelRetries: number }).gitPanelRetries = ((window as unknown as { gitPanelRetries?: number }).gitPanelRetries ?? 0) + 1;
	},
	loadBranches() {},
	refreshDiffStats() {},
	refreshChangedFiles() {}
};
export const githubRepoInfo = writable<Record<string, unknown> | null>({ default_branch: 'main' });

const idle = {
	active: false,
	status: 'idle',
	title: null as string | null,
	steps: [] as Record<string, unknown>[],
	error: null as string | null
};
export const gitOperationState = writable(idle);
export const gitOperations = { dismiss: () => gitOperationState.set(idle) };

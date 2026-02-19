/**
 * GitHub Store -- Re-export shim.
 * Implementation lives in github.svelte.ts (Svelte 5 runes).
 */
export {
	github,
	githubAuthenticated,
	githubRepoName,
	githubRepoInfo,
	pullRequests,
	workflowRuns,
	githubLoading,
	githubLoadingPRs,
	githubLoadingRuns,
	githubError,
	openPullRequests,
	mergedPullRequests,
	activeWorkflowRuns,
} from './github.svelte';

export type {
	PullRequest,
	WorkflowRun,
	GitHubStatus,
	CreatePullRequestRequest,
} from './github.svelte';

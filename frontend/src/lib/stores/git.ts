/**
 * Git Store for AI Hub
 *
 * Manages git repository state including branches, commits, and worktrees.
 */

import { writable, derived } from 'svelte/store';
import * as gitApi from '$lib/api/git';

// Re-export types from API
export type {
    Branch,
    CommitNode,
    Worktree,
    GitStatus,
    CreateWorktreeRequest
} from '$lib/api/git';

// ============================================================================
// Store State
// ============================================================================

interface GitState {
    projectId: string | null;
    status: gitApi.GitStatus | null;
    branches: gitApi.Branch[];
    commits: gitApi.CommitNode[];
    worktrees: gitApi.Worktree[];
    head: string | null;
    loading: boolean;
    error: string | null;
}

// ============================================================================
// Git Store
// ============================================================================

function createGitStore() {
    const { subscribe, set, update } = writable<GitState>({
        projectId: null,
        status: null,
        branches: [],
        commits: [],
        worktrees: [],
        head: null,
        loading: false,
        error: null
    });

    return {
        subscribe,

        /**
         * Load repository data for a project
         */
        async loadRepository(projectId: string) {
            update(s => ({ ...s, projectId, loading: true, error: null }));

            try {
                // Load status, branches, and graph in parallel
                const [status, branches, graph, worktrees] = await Promise.all([
                    gitApi.getGitStatus(projectId),
                    gitApi.getBranches(projectId),
                    gitApi.getCommitGraph(projectId, 50),
                    gitApi.getWorktrees(projectId)
                ]);

                update(s => ({
                    ...s,
                    status,
                    branches,
                    commits: graph.commits,
                    head: graph.head,
                    worktrees,
                    loading: false
                }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || 'Failed to load repository'
                }));
            }
        },

        /**
         * Refresh the git status
         */
        async refreshStatus() {
            const state = getCurrentState();
            if (!state.projectId) return;

            try {
                const status = await gitApi.getGitStatus(state.projectId);
                update(s => ({ ...s, status }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({ ...s, error: error.detail || 'Failed to refresh status' }));
            }
        },

        /**
         * Load branches
         */
        async loadBranches() {
            const state = getCurrentState();
            if (!state.projectId) return;

            try {
                const branches = await gitApi.getBranches(state.projectId);
                update(s => ({ ...s, branches }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({ ...s, error: error.detail || 'Failed to load branches' }));
            }
        },

        /**
         * Load commit graph
         */
        async loadGraph(limit?: number) {
            const state = getCurrentState();
            if (!state.projectId) return;

            try {
                const graph = await gitApi.getCommitGraph(state.projectId, limit);
                update(s => ({ ...s, commits: graph.commits, head: graph.head }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({ ...s, error: error.detail || 'Failed to load commit graph' }));
            }
        },

        /**
         * Load worktrees
         */
        async loadWorktrees() {
            const state = getCurrentState();
            if (!state.projectId) return;

            try {
                const worktrees = await gitApi.getWorktrees(state.projectId);
                update(s => ({ ...s, worktrees }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({ ...s, error: error.detail || 'Failed to load worktrees' }));
            }
        },

        /**
         * Create a new branch
         */
        async createBranch(name: string, startPoint?: string) {
            const state = getCurrentState();
            if (!state.projectId) return;

            update(s => ({ ...s, loading: true, error: null }));

            try {
                await gitApi.createBranch(state.projectId, name, startPoint);
                await this.loadBranches();
                await this.refreshStatus();
                update(s => ({ ...s, loading: false }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || 'Failed to create branch'
                }));
                throw e;
            }
        },

        /**
         * Delete a branch
         */
        async deleteBranch(name: string) {
            const state = getCurrentState();
            if (!state.projectId) return;

            update(s => ({ ...s, loading: true, error: null }));

            try {
                await gitApi.deleteBranch(state.projectId, name);
                await this.loadBranches();
                update(s => ({ ...s, loading: false }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || 'Failed to delete branch'
                }));
                throw e;
            }
        },

        /**
         * Checkout a branch or ref
         */
        async checkout(ref: string) {
            const state = getCurrentState();
            if (!state.projectId) return;

            update(s => ({ ...s, loading: true, error: null }));

            try {
                await gitApi.checkout(state.projectId, ref);
                await this.loadBranches();
                await this.refreshStatus();
                await this.loadGraph();
                update(s => ({ ...s, loading: false }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || 'Failed to checkout'
                }));
                throw e;
            }
        },

        /**
         * Fetch from remote
         */
        async fetch() {
            const state = getCurrentState();
            if (!state.projectId) return;

            update(s => ({ ...s, loading: true, error: null }));

            try {
                await gitApi.fetchRemote(state.projectId);
                await this.loadBranches();
                await this.loadGraph();
                update(s => ({ ...s, loading: false }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || 'Failed to fetch'
                }));
                throw e;
            }
        },

        /**
         * Create a new worktree
         */
        async createWorktree(
            branchName: string,
            createNew?: boolean,
            baseBranch?: string,
            profileId?: string
        ) {
            const state = getCurrentState();
            if (!state.projectId) return;

            update(s => ({ ...s, loading: true, error: null }));

            try {
                await gitApi.createWorktree(state.projectId, {
                    branch_name: branchName,
                    create_new: createNew,
                    base_branch: baseBranch,
                    profile_id: profileId
                });
                await this.loadWorktrees();
                await this.loadBranches();
                update(s => ({ ...s, loading: false }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || 'Failed to create worktree'
                }));
                throw e;
            }
        },

        /**
         * Delete a worktree
         */
        async deleteWorktree(worktreeId: string) {
            const state = getCurrentState();
            if (!state.projectId) return;

            update(s => ({ ...s, loading: true, error: null }));

            try {
                await gitApi.deleteWorktree(state.projectId, worktreeId);
                await this.loadWorktrees();
                await this.loadBranches();
                update(s => ({ ...s, loading: false }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || 'Failed to delete worktree'
                }));
                throw e;
            }
        },

        /**
         * Clear error
         */
        clearError() {
            update(s => ({ ...s, error: null }));
        },

        /**
         * Clear store state
         */
        clear() {
            set({
                projectId: null,
                status: null,
                branches: [],
                commits: [],
                worktrees: [],
                head: null,
                loading: false,
                error: null
            });
        }
    };

    // Helper to get current state synchronously
    function getCurrentState(): GitState {
        let state: GitState = {
            projectId: null,
            status: null,
            branches: [],
            commits: [],
            worktrees: [],
            head: null,
            loading: false,
            error: null
        };
        subscribe(s => { state = s; })();
        return state;
    }
}

export const git = createGitStore();

// ============================================================================
// Derived Stores
// ============================================================================

export const branches = derived(git, $git => $git.branches);
export const commits = derived(git, $git => $git.commits);
export const worktrees = derived(git, $git => $git.worktrees);
export const gitStatus = derived(git, $git => $git.status);
export const gitLoading = derived(git, $git => $git.loading);
export const gitError = derived(git, $git => $git.error);
export const gitHead = derived(git, $git => $git.head);

// Derived store for current branch
export const currentBranch = derived(git, $git =>
    $git.branches.find(b => b.current) || null
);

// Derived store for local branches only
export const localBranches = derived(git, $git =>
    $git.branches.filter(b => !b.remote)
);

// Derived store for remote branches only
export const remoteBranches = derived(git, $git =>
    $git.branches.filter(b => b.remote)
);

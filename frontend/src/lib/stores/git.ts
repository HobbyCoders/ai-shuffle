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
    CreateWorktreeRequest,
    DiffStats
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
    diffStats: gitApi.DiffStats | null;
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
        diffStats: null,
        loading: false,
        error: null
    });

    /** Track which projectId is currently loaded to skip redundant reloads. */
    let _loadedProjectId: string | null = null;
    /** Guard against concurrent loadRepository calls. */
    let _loadInFlight = false;

    return {
        subscribe,

        /**
         * Load repository data for a project.
         * Skips if already loaded for the same project (use forceReload to override).
         */
        async loadRepository(projectId: string, forceReload = false) {
            // Skip if already loaded for this project
            if (!forceReload && _loadedProjectId === projectId) return;
            // Prevent concurrent loads
            if (_loadInFlight) return;
            _loadInFlight = true;

            update(s => ({ ...s, projectId, loading: true, error: null }));

            try {
                // Load status first to check if it's a git repo
                const status = await gitApi.getGitStatus(projectId);

                // Update status immediately
                update(s => ({ ...s, status }));

                // If it's a git repo, fetch remote first then load data
                if (status.is_git_repo) {
                    // Fetch from remote to discover new branches (ignore errors)
                    await gitApi.fetchRemote(projectId).catch(() => {});

                    const [branches, graphResponse, worktrees] = await Promise.all([
                        gitApi.getBranches(projectId, true),
                        gitApi.getCommitGraph(projectId, 50),
                        gitApi.getWorktrees(projectId)
                    ]);

                    update(s => ({
                        ...s,
                        branches,
                        commits: graphResponse.commits,
                        worktrees,
                        loading: false
                    }));
                } else {
                    update(s => ({ ...s, loading: false }));
                }
                _loadedProjectId = projectId;
            } catch (e) {
                console.error('Failed to load git repository:', e);
                const error = e as { detail?: string; message?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || error.message || 'Failed to load repository'
                }));
            } finally {
                _loadInFlight = false;
            }
        },

        /**
         * Clear repository data (used when switching to a tab with no project)
         */
        clearRepository() {
            _loadedProjectId = null;
            set({
                projectId: null,
                status: null,
                branches: [],
                commits: [],
                worktrees: [],
                diffStats: null,
                loading: false,
                error: null
            });
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
                const branches = await gitApi.getBranches(state.projectId, true);
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
                const graphResponse = await gitApi.getCommitGraph(state.projectId, limit);
                update(s => ({ ...s, commits: graphResponse.commits }));
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
        ): Promise<gitApi.Worktree | undefined> {
            const state = getCurrentState();
            if (!state.projectId) return undefined;

            update(s => ({ ...s, loading: true, error: null }));

            try {
                const response = await gitApi.createWorktree(state.projectId, {
                    branch_name: branchName,
                    create_new_branch: createNew,
                    base_branch: baseBranch,
                    profile_id: profileId
                });
                await this.loadWorktrees();
                await this.loadBranches();
                update(s => ({ ...s, loading: false }));
                return response.worktree || undefined;
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
         * Refresh diff stats
         */
        async refreshDiffStats() {
            const state = getCurrentState();
            if (!state.projectId) return;

            try {
                const diffStats = await gitApi.getDiffStats(state.projectId);
                update(s => ({ ...s, diffStats }));
            } catch (e) {
                // Silently fail - diff stats are non-critical
                console.debug('Failed to refresh diff stats:', e);
            }
        },

        /**
         * Commit changes
         */
        async commitChanges(
            message: string,
            includeUnstaged: boolean = true
        ): Promise<gitApi.CommitResponse> {
            const state = getCurrentState();
            if (!state.projectId) throw new Error('No project selected');

            try {
                const response = await gitApi.commitChanges(state.projectId, {
                    message,
                    include_unstaged: includeUnstaged
                });

                // Refresh status and diff stats after commit
                await Promise.all([
                    this.refreshStatus(),
                    this.refreshDiffStats()
                ]);

                return response;
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    error: error.detail || 'Failed to commit changes'
                }));
                throw e;
            }
        },

        /**
         * Push commits to remote
         */
        async pushCommits(
            branch?: string,
            setUpstream: boolean = false
        ): Promise<{ success: boolean; message: string }> {
            const state = getCurrentState();
            if (!state.projectId) throw new Error('No project selected');

            try {
                const response = await gitApi.pushCommits(state.projectId, {
                    branch,
                    set_upstream: setUpstream
                });

                // Refresh status after push
                await this.refreshStatus();

                return response;
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    error: error.detail || 'Failed to push commits'
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
                diffStats: null,
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
            diffStats: null,
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
export const diffStats = derived(git, $git => $git.diffStats);
export const gitLoading = derived(git, $git => $git.loading);
export const gitError = derived(git, $git => $git.error);

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

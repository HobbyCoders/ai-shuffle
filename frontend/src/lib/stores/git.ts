/**
 * Git Store for AI Hub
 *
 * Manages git repository state including branches, commits, and worktrees.
 * Features per-key caching for instant tab switches and deferred remote fetch.
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

export type GitMode = 'project' | 'worktree';

interface GitState {
    projectId: string | null;
    worktreeId: string | null;
    mode: GitMode;
    status: gitApi.GitStatus | null;
    branches: gitApi.Branch[];
    commits: gitApi.CommitNode[];
    worktrees: gitApi.Worktree[];
    diffStats: gitApi.DiffStats | null;
    loading: boolean;
    error: string | null;
}

// Subset of GitState that we cache per key
interface CachedState {
    status: gitApi.GitStatus | null;
    branches: gitApi.Branch[];
    commits: gitApi.CommitNode[];
    worktrees: gitApi.Worktree[];
    diffStats: gitApi.DiffStats | null;
}

const EMPTY_STATE: GitState = {
    projectId: null,
    worktreeId: null,
    mode: 'project',
    status: null,
    branches: [],
    commits: [],
    worktrees: [],
    diffStats: null,
    loading: false,
    error: null
};

const MAX_CACHE_ENTRIES = 10;

// ============================================================================
// Git Store
// ============================================================================

function createGitStore() {
    const { subscribe, set, update } = writable<GitState>({ ...EMPTY_STATE });

    /** Track which projectId+worktreeId is currently loaded to skip redundant reloads. */
    let _loadedKey: string | null = null;
    /**
     * Track which key is currently being loaded.
     * Uses a key string (not boolean) so a load for a DIFFERENT key can supersede
     * the current in-flight load.
     */
    let _loadInFlight: string | null = null;

    /** Per-key state cache for instant tab switches. */
    const _cache = new Map<string, CachedState>();

    function _makeKey(projectId: string, worktreeId?: string | null): string {
        return worktreeId ? `${projectId}:${worktreeId}` : projectId;
    }

    function _saveToCache(key: string, state: CachedState) {
        // Evict oldest if at capacity
        if (_cache.size >= MAX_CACHE_ENTRIES && !_cache.has(key)) {
            const oldest = _cache.keys().next().value;
            if (oldest !== undefined) _cache.delete(oldest);
        }
        _cache.set(key, state);
    }

    function _restoreFromCache(key: string): CachedState | null {
        const cached = _cache.get(key);
        if (!cached) return null;
        // Move to end (most recently used)
        _cache.delete(key);
        _cache.set(key, cached);
        return cached;
    }

    /**
     * Core load logic shared by loadRepository and loadForWorktree.
     * Loads data in optimal order: status → parallel(branches, commits, worktrees, diffStats) → background fetch.
     */
    async function _loadCore(
        key: string,
        projectId: string,
        worktreeId: string | undefined,
        mode: GitMode
    ) {
        // Restore cached state instantly if available
        const cached = _restoreFromCache(key);
        if (cached) {
            update(s => ({
                ...s,
                projectId,
                worktreeId: worktreeId ?? null,
                mode,
                ...cached,
                loading: true,
                error: null
            }));
        } else {
            update(s => ({
                ...s,
                projectId,
                worktreeId: worktreeId ?? null,
                mode,
                loading: true,
                error: null
            }));
        }

        try {
            const status = await gitApi.getGitStatus(projectId, worktreeId);
            if (_loadInFlight !== key) return;

            update(s => ({ ...s, status }));

            if (status.is_git_repo) {
                // Load all data in parallel — don't wait for fetchRemote
                const [branches, graphResponse, worktrees, diffStats] = await Promise.all([
                    gitApi.getBranches(projectId, true, worktreeId),
                    gitApi.getCommitGraph(projectId, 50),
                    gitApi.getWorktrees(projectId),
                    gitApi.getDiffStats(projectId, true, worktreeId).catch(() => null)
                ]);
                if (_loadInFlight !== key) return;

                const newState: CachedState = {
                    status,
                    branches,
                    commits: graphResponse.commits,
                    worktrees,
                    diffStats: diffStats ?? null
                };

                update(s => ({
                    ...s,
                    ...newState,
                    loading: false
                }));

                _loadedKey = key;
                _saveToCache(key, newState);

                // Background fetch — update ahead/behind counts after network completes
                _backgroundFetch(key, projectId, worktreeId);
            } else {
                update(s => ({ ...s, loading: false }));
                _loadedKey = key;
            }
        } catch (e) {
            if (_loadInFlight !== key) return;
            console.error('Failed to load git repository:', e);
            const error = e as { detail?: string; message?: string };
            update(s => ({
                ...s,
                loading: false,
                error: error.detail || error.message || 'Failed to load repository'
            }));
        } finally {
            if (_loadInFlight === key) _loadInFlight = null;
        }
    }

    /**
     * Background fetch from remote + silent refresh of status/branches.
     * Runs after the UI is already populated, so the user sees data instantly.
     */
    function _backgroundFetch(key: string, projectId: string, worktreeId?: string) {
        gitApi.fetchRemote(projectId, worktreeId).then(async () => {
            // Only update if we're still on the same key
            if (_loadedKey !== key) return;
            try {
                const [status, branches] = await Promise.all([
                    gitApi.getGitStatus(projectId, worktreeId),
                    gitApi.getBranches(projectId, true, worktreeId)
                ]);
                if (_loadedKey !== key) return;
                update(s => ({ ...s, status, branches }));
                // Update cache too
                const cached = _cache.get(key);
                if (cached) {
                    cached.status = status;
                    cached.branches = branches;
                }
            } catch {
                // Silent — background refresh is non-critical
            }
        }).catch(() => {
            // fetchRemote failed — not critical, user already has local data
        });
    }

    return {
        subscribe,

        /**
         * Load repository data for a project.
         * Skips if already loaded for the same project (use forceReload to override).
         */
        async loadRepository(projectId: string, forceReload = false) {
            const key = _makeKey(projectId);
            if (!forceReload && _loadedKey === key) return;
            if (_loadInFlight === key) return;
            _loadInFlight = key;
            await _loadCore(key, projectId, undefined, 'project');
        },

        /**
         * Load repository data in worktree mode.
         * Status, branches, and diff stats are loaded from the worktree's perspective.
         */
        async loadForWorktree(projectId: string, worktreeId: string, forceReload = false) {
            const key = _makeKey(projectId, worktreeId);
            if (!forceReload && _loadedKey === key) return;
            if (_loadInFlight === key) return;
            _loadInFlight = key;
            await _loadCore(key, projectId, worktreeId, 'worktree');
        },

        /**
         * Clear repository data (used when switching to a tab with no project)
         */
        clearRepository() {
            _loadedKey = null;
            set({ ...EMPTY_STATE });
        },

        /**
         * Refresh the git status
         */
        async refreshStatus() {
            const state = getCurrentState();
            if (!state.projectId) return;

            try {
                const status = await gitApi.getGitStatus(state.projectId, state.worktreeId ?? undefined);
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
                const branches = await gitApi.getBranches(state.projectId, true, state.worktreeId ?? undefined);
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
                await gitApi.fetchRemote(state.projectId, state.worktreeId ?? undefined);
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
        async deleteWorktree(worktreeId: string, keepBranch: boolean = true) {
            const state = getCurrentState();
            if (!state.projectId) return;

            update(s => ({ ...s, loading: true, error: null }));

            try {
                await gitApi.deleteWorktree(state.projectId, worktreeId, keepBranch);
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
                const diffStats = await gitApi.getDiffStats(state.projectId, true, state.worktreeId ?? undefined);
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
                }, state.worktreeId ?? undefined);

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
                }, state.worktreeId ?? undefined);

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
         * Apply a server-pushed git state update from the file watcher via WebSocket.
         * Updates both the live store and the LRU cache without making API calls.
         */
        applyServerUpdate(
            projectId: string,
            worktreeId: string | null,
            changes: {
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                status?: Record<string, any>;
                diff_stats?: gitApi.DiffStats;
                branches?: {
                    branches: Array<{
                        name: string; is_current: boolean; is_remote: boolean;
                        upstream?: string; ahead?: number; behind?: number;
                        commit_message?: string; has_worktree?: boolean; worktree_id?: string;
                    }>;
                    current_branch: string | null;
                };
            }
        ) {
            const key = _makeKey(projectId, worktreeId ?? undefined);

            // Only apply if we're currently viewing this project/worktree
            if (_loadedKey !== key) return;

            update(s => {
                const updated = { ...s };

                if (changes.status) {
                    updated.status = s.status
                        ? { ...s.status, ...changes.status }
                        : changes.status as gitApi.GitStatus;
                }

                if (changes.diff_stats) {
                    updated.diffStats = changes.diff_stats;
                }

                if (changes.branches) {
                    updated.branches = changes.branches.branches.map(b => ({
                        name: b.name,
                        current: b.is_current,
                        remote: b.is_remote,
                        upstream: b.upstream,
                        ahead: b.ahead,
                        behind: b.behind,
                        lastCommit: b.commit_message,
                        hasWorktree: b.has_worktree,
                        worktreeId: b.worktree_id
                    }));
                }

                return updated;
            });

            // Update LRU cache too
            const cached = _cache.get(key);
            if (cached) {
                if (changes.status) {
                    cached.status = cached.status
                        ? { ...cached.status, ...changes.status }
                        : changes.status as gitApi.GitStatus;
                }
                if (changes.diff_stats) {
                    cached.diffStats = changes.diff_stats;
                }
                if (changes.branches) {
                    cached.branches = changes.branches.branches.map(b => ({
                        name: b.name,
                        current: b.is_current,
                        remote: b.is_remote,
                        upstream: b.upstream,
                        ahead: b.ahead,
                        behind: b.behind,
                        lastCommit: b.commit_message,
                        hasWorktree: b.has_worktree,
                        worktreeId: b.worktree_id
                    }));
                }
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
            _loadedKey = null;
            set({ ...EMPTY_STATE });
        }
    };

    // Helper to get current state synchronously
    function getCurrentState(): GitState {
        let state: GitState = { ...EMPTY_STATE };
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
export const gitMode = derived(git, $git => $git.mode);

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

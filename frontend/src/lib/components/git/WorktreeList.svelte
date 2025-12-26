<script lang="ts">
    import { git, worktrees, localBranches, gitLoading, branches } from '$lib/stores/git';
    import type { Worktree, Branch } from '$lib/stores/git';
    import { sessions } from '$lib/stores/chat';
    import type { Session } from '$lib/api/client';

    interface Props {
        projectId: string | null;
        onSessionSwitch?: (sessionId: string) => void;
    }

    let { projectId, onSessionSwitch }: Props = $props();

    // Form state
    let showCreateForm = $state(false);
    let newBranchName = $state('');
    let createNewBranch = $state(true);
    let baseBranch = $state('');
    let existingBranch = $state('');
    let creating = $state(false);
    let createError = $state<string | null>(null);

    // Delete confirmation
    let deletingWorktree = $state<string | null>(null);

    function getStatusColor(status: string): string {
        switch (status) {
            case 'active':
                return 'text-green-500 bg-green-500/15';
            case 'idle':
                return 'text-amber-500 bg-amber-500/15';
            case 'error':
                return 'text-destructive bg-destructive/15';
            default:
                return 'text-muted-foreground bg-muted';
        }
    }

    function formatPath(path: string): string {
        // Show only the last 2 segments
        const parts = path.split('/');
        if (parts.length > 2) {
            return '.../' + parts.slice(-2).join('/');
        }
        return path;
    }

    function formatDate(dateStr: string): string {
        const date = new Date(dateStr);
        return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    // Get branch status (ahead/behind) from branches store
    function getBranchStatus(branchName: string): { ahead?: number; behind?: number } | null {
        const branch = $branches.find(b => b.name === branchName);
        if (branch && (branch.ahead !== undefined || branch.behind !== undefined)) {
            return { ahead: branch.ahead, behind: branch.behind };
        }
        return null;
    }

    // Get session info for a worktree
    function getSessionForWorktree(sessionId: string | null): Session | null {
        if (!sessionId) return null;
        return $sessions.find(s => s.id === sessionId) || null;
    }

    // Handle clicking on session link
    function handleSessionClick(sessionId: string) {
        if (onSessionSwitch) {
            onSessionSwitch(sessionId);
        }
    }

    async function handleCreate() {
        const branchName = createNewBranch ? newBranchName.trim() : existingBranch;
        if (!branchName) return;

        creating = true;
        createError = null;

        try {
            await git.createWorktree(
                branchName,
                createNewBranch,
                createNewBranch ? baseBranch.trim() || undefined : undefined
            );
            resetForm();
        } catch (e) {
            const error = e as { detail?: string };
            createError = error.detail || 'Failed to create worktree';
        } finally {
            creating = false;
        }
    }

    async function handleDelete(worktreeId: string) {
        try {
            await git.deleteWorktree(worktreeId);
            deletingWorktree = null;
        } catch (e) {
            // Error handled by store
        }
    }

    function resetForm() {
        showCreateForm = false;
        newBranchName = '';
        createNewBranch = true;
        baseBranch = '';
        existingBranch = '';
        createError = null;
    }

    // Available branches for existing branch selection (non-remote, not already in worktree)
    const availableBranches = $derived(() => {
        const worktreeBranches = new Set($worktrees.map(w => w.branch_name));
        return $localBranches.filter(b => !worktreeBranches.has(b.name));
    });
</script>

<div class="p-4">
    <!-- Header -->
    <div class="flex items-center justify-between mb-4">
        <div>
            <h3 class="text-sm font-medium text-foreground">Worktrees</h3>
            <p class="text-xs text-muted-foreground mt-0.5">
                Manage parallel working directories for concurrent development
            </p>
        </div>

        <button
            onclick={() => showCreateForm = !showCreateForm}
            class="px-3 py-2 text-sm bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity flex items-center gap-2"
        >
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
            </svg>
            New Worktree
        </button>
    </div>

    <!-- Create form -->
    {#if showCreateForm}
        <div class="mb-4 p-4 bg-muted/50 border border-border rounded-lg">
            <h3 class="text-sm font-medium text-foreground mb-3">Create New Worktree</h3>

            {#if createError}
                <div class="mb-3 p-2 bg-destructive/10 border border-destructive/30 rounded text-destructive text-sm">
                    {createError}
                </div>
            {/if}

            <!-- Branch type selector -->
            <div class="flex gap-2 mb-4">
                <button
                    onclick={() => createNewBranch = true}
                    class="px-3 py-1.5 text-xs font-medium rounded-lg transition-colors {createNewBranch ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-accent'}"
                >
                    New Branch
                </button>
                <button
                    onclick={() => createNewBranch = false}
                    class="px-3 py-1.5 text-xs font-medium rounded-lg transition-colors {!createNewBranch ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-accent'}"
                >
                    Existing Branch
                </button>
            </div>

            {#if createNewBranch}
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                        <label for="worktree-path" class="block text-xs text-muted-foreground mb-1">New Branch Name <span class="text-destructive">*</span></label>
                        <input
                            id="worktree-path"
                            type="text"
                            bind:value={newBranchName}
                            placeholder="feature/my-worktree"
                            class="w-full px-3 py-2 bg-muted border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                        />
                    </div>
                    <div>
                        <label for="worktree-branch" class="block text-xs text-muted-foreground mb-1">Base Branch (optional)</label>
                        <select
                            id="worktree-branch"
                            bind:value={baseBranch}
                            class="w-full px-3 py-2 bg-muted border border-border rounded-lg text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                        >
                            <option value="">HEAD (current)</option>
                            {#each $localBranches as branch}
                                <option value={branch.name}>{branch.name}</option>
                            {/each}
                        </select>
                    </div>
                </div>
            {:else}
                <div>
                    <label for="existing-branch" class="block text-xs text-muted-foreground mb-1">Select Branch <span class="text-destructive">*</span></label>
                    {#if availableBranches().length === 0}
                        <p class="text-sm text-muted-foreground py-2">No available branches. All local branches are already in use by worktrees.</p>
                    {:else}
                        <select
                            id="existing-branch"
                            bind:value={existingBranch}
                            class="w-full px-3 py-2 bg-muted border border-border rounded-lg text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                        >
                            <option value="">Select a branch...</option>
                            {#each availableBranches() as branch}
                                <option value={branch.name}>{branch.name}</option>
                            {/each}
                        </select>
                    {/if}
                </div>
            {/if}

            <div class="flex justify-end gap-2 mt-4">
                <button
                    onclick={resetForm}
                    class="px-3 py-1.5 text-sm bg-muted text-foreground rounded-lg hover:bg-accent transition-colors"
                >
                    Cancel
                </button>
                <button
                    onclick={handleCreate}
                    disabled={(createNewBranch ? !newBranchName.trim() : !existingBranch) || creating}
                    class="px-3 py-1.5 text-sm bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50"
                >
                    {creating ? 'Creating...' : 'Create Worktree'}
                </button>
            </div>
        </div>
    {/if}

    <!-- Worktree list -->
    {#if $gitLoading && $worktrees.length === 0}
        <!-- Loading skeleton -->
        <div class="space-y-3">
            {#each [1, 2, 3] as i}
                <div class="p-4 bg-card border border-border rounded-lg animate-pulse">
                    <div class="flex items-start justify-between gap-4">
                        <div class="flex-1 space-y-2">
                            <div class="flex items-center gap-2">
                                <div class="w-4 h-4 bg-muted rounded"></div>
                                <div class="h-4 bg-muted rounded w-32"></div>
                                <div class="h-4 bg-muted rounded w-12"></div>
                            </div>
                            <div class="flex items-center gap-1.5">
                                <div class="w-3.5 h-3.5 bg-muted rounded"></div>
                                <div class="h-3 bg-muted rounded w-48"></div>
                            </div>
                            <div class="h-3 bg-muted rounded w-24"></div>
                        </div>
                        <div class="flex gap-1">
                            <div class="w-8 h-8 bg-muted rounded-lg"></div>
                            <div class="w-8 h-8 bg-muted rounded-lg"></div>
                        </div>
                    </div>
                </div>
            {/each}
        </div>
    {:else if $worktrees.length === 0}
        <!-- Empty state with instructions -->
        <div class="text-center py-12 text-muted-foreground border border-dashed border-border rounded-lg bg-muted/20">
            <svg class="w-16 h-16 mx-auto mb-4 opacity-40" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
            </svg>
            <p class="text-lg font-medium text-foreground mb-2">No worktrees yet</p>
            <p class="text-sm max-w-sm mx-auto mb-4">
                Worktrees let you check out multiple branches at once,
                each in its own directory with its own session.
            </p>
            <div class="flex flex-col sm:flex-row justify-center gap-2">
                <button
                    onclick={() => showCreateForm = true}
                    class="inline-flex items-center justify-center gap-2 px-4 py-2 bg-primary text-primary-foreground text-sm rounded-lg hover:opacity-90 transition-opacity"
                >
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
                    </svg>
                    Create First Worktree
                </button>
            </div>
            <div class="mt-6 text-left max-w-md mx-auto">
                <p class="text-xs font-medium text-foreground mb-2">Quick start:</p>
                <ol class="text-xs space-y-1 list-decimal list-inside">
                    <li>Click "New Worktree" above</li>
                    <li>Enter a branch name (or pick existing)</li>
                    <li>A new directory and session will be created</li>
                    <li>Click the session link to switch to it</li>
                </ol>
            </div>
        </div>
    {:else}
        <div class="space-y-3">
            {#each $worktrees as worktree (worktree.id)}
                {@const branchStatus = getBranchStatus(worktree.branch_name)}
                {@const session = getSessionForWorktree(worktree.session_id)}
                <div class="p-4 bg-card border border-border rounded-lg hover:border-primary/30 transition-colors group">
                    <div class="flex items-start justify-between gap-4">
                        <div class="min-w-0 flex-1">
                            <!-- Branch name and status -->
                            <div class="flex items-center gap-2 flex-wrap">
                                <svg class="w-4 h-4 text-primary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 3v12m0 0c0 2.5 2 3 3.5 3s3.5-.5 3.5-3m-7 0c0-2.5 2-3 3.5-3s3.5.5 3.5 3m0 0v-6m0 0c0-2.5-2-3-3.5-3S6 6.5 6 9" />
                                </svg>
                                <span class="font-medium text-foreground">{worktree.branch_name}</span>
                                <span class="px-1.5 py-0.5 text-[10px] uppercase rounded {getStatusColor(worktree.status)}">
                                    {worktree.status}
                                </span>
                                <!-- Branch ahead/behind status -->
                                {#if branchStatus}
                                    {#if branchStatus.ahead && branchStatus.ahead > 0}
                                        <span class="px-1.5 py-0.5 text-[10px] rounded bg-green-500/15 text-green-600">
                                            {branchStatus.ahead} ahead
                                        </span>
                                    {/if}
                                    {#if branchStatus.behind && branchStatus.behind > 0}
                                        <span class="px-1.5 py-0.5 text-[10px] rounded bg-amber-500/15 text-amber-600">
                                            {branchStatus.behind} behind
                                        </span>
                                    {/if}
                                {/if}
                            </div>

                            <!-- Path -->
                            <div class="flex items-center gap-1.5 mt-1.5 text-xs text-muted-foreground">
                                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                                </svg>
                                <span class="font-mono truncate" title={worktree.worktree_path}>{formatPath(worktree.worktree_path)}</span>
                            </div>

                            <!-- Base branch -->
                            {#if worktree.base_branch}
                                <div class="flex items-center gap-1.5 mt-1 text-xs text-muted-foreground">
                                    <span>Based on:</span>
                                    <span class="text-foreground">{worktree.base_branch}</span>
                                </div>
                            {/if}

                            <!-- Session link - clickable if we have session info -->
                            {#if worktree.session_id}
                                <div class="flex items-center gap-1.5 mt-2">
                                    {#if session}
                                        <button
                                            onclick={() => handleSessionClick(worktree.session_id!)}
                                            class="inline-flex items-center gap-1.5 px-2 py-1 text-xs bg-green-500/10 text-green-600 rounded-lg hover:bg-green-500/20 transition-colors"
                                        >
                                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                                            </svg>
                                            <span class="truncate max-w-[200px]">{session.title || 'Untitled session'}</span>
                                            <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                                            </svg>
                                        </button>
                                    {:else}
                                        <span class="inline-flex items-center gap-1.5 px-2 py-1 text-xs text-green-500">
                                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
                                            </svg>
                                            Linked to session
                                        </span>
                                    {/if}
                                </div>
                            {:else}
                                <div class="flex items-center gap-1.5 mt-2 text-xs text-muted-foreground">
                                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
                                    </svg>
                                    <span>No session linked</span>
                                </div>
                            {/if}

                            <!-- Created date -->
                            <div class="text-xs text-muted-foreground mt-1.5">
                                Created: {formatDate(worktree.created_at)}
                            </div>
                        </div>

                        <!-- Actions -->
                        <div class="flex items-center gap-1 shrink-0">
                            {#if deletingWorktree === worktree.id}
                                <div class="flex items-center gap-2 bg-destructive/10 rounded-lg px-3 py-1.5">
                                    <span class="text-xs text-destructive">Delete?</span>
                                    <button
                                        onclick={() => handleDelete(worktree.id)}
                                        class="p-1 text-destructive hover:text-destructive/80"
                                        title="Confirm delete"
                                    >
                                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                                        </svg>
                                    </button>
                                    <button
                                        onclick={() => deletingWorktree = null}
                                        class="p-1 text-muted-foreground hover:text-foreground"
                                        title="Cancel"
                                    >
                                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                                        </svg>
                                    </button>
                                </div>
                            {:else}
                                <!-- Copy path button -->
                                <button
                                    onclick={() => navigator.clipboard.writeText(worktree.worktree_path)}
                                    class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors opacity-0 group-hover:opacity-100"
                                    title="Copy path"
                                >
                                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 5H6a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2v-1M8 5a2 2 0 002 2h2a2 2 0 002-2M8 5a2 2 0 012-2h2a2 2 0 012 2m0 0h2a2 2 0 012 2v3m2 4H10m0 0l3-3m-3 3l3 3" />
                                    </svg>
                                </button>

                                <!-- Delete button -->
                                <button
                                    onclick={() => deletingWorktree = worktree.id}
                                    class="p-1.5 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors opacity-0 group-hover:opacity-100"
                                    title="Delete worktree"
                                >
                                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                                    </svg>
                                </button>
                            {/if}
                        </div>
                    </div>
                </div>
            {/each}
        </div>
    {/if}

    <!-- Info box -->
    <div class="mt-6 p-4 bg-muted/50 border border-border rounded-lg">
        <h4 class="text-sm font-medium text-foreground flex items-center gap-2">
            <svg class="w-4 h-4 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            About Worktrees
        </h4>
        <p class="text-xs text-muted-foreground mt-2">
            Git worktrees allow you to check out multiple branches simultaneously in separate directories.
            This is useful for:
        </p>
        <ul class="text-xs text-muted-foreground mt-2 space-y-1 list-disc list-inside">
            <li>Working on multiple features in parallel</li>
            <li>Quick context switching between tasks</li>
            <li>Running different versions for testing</li>
        </ul>
    </div>
</div>

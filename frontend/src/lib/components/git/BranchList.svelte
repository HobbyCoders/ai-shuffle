<script lang="ts">
    import { git, branches, localBranches, remoteBranches, gitLoading, currentBranch } from '$lib/stores/git';
    import type { Branch } from '$lib/stores/git';

    interface Props {
        projectId: string | null;
    }

    let { projectId }: Props = $props();

    // Form state
    let showCreateForm = $state(false);
    let newBranchName = $state('');
    let newBranchStartPoint = $state('');
    let creating = $state(false);
    let createError = $state<string | null>(null);

    // Delete confirmation
    let deletingBranch = $state<string | null>(null);
    let deleteConfirmName = $state('');

    // Filter
    let filter = $state<'all' | 'local' | 'remote'>('local');
    let searchQuery = $state('');

    // Filtered branches
    const filteredBranches = $derived(() => {
        let branchList: Branch[] = [];

        if (filter === 'local') {
            branchList = $localBranches;
        } else if (filter === 'remote') {
            branchList = $remoteBranches;
        } else {
            branchList = $branches;
        }

        if (searchQuery) {
            const query = searchQuery.toLowerCase();
            branchList = branchList.filter(b => b.name.toLowerCase().includes(query));
        }

        return branchList;
    });

    async function handleCreateBranch() {
        if (!newBranchName.trim()) return;

        creating = true;
        createError = null;

        try {
            await git.createBranch(
                newBranchName.trim(),
                newBranchStartPoint.trim() || undefined
            );
            newBranchName = '';
            newBranchStartPoint = '';
            showCreateForm = false;
        } catch (e) {
            const error = e as { detail?: string };
            createError = error.detail || 'Failed to create branch';
        } finally {
            creating = false;
        }
    }

    async function handleCheckout(branch: Branch) {
        if (branch.current) return;
        await git.checkout(branch.name);
    }

    async function handleDelete(branchName: string) {
        if (deleteConfirmName !== branchName) return;

        try {
            await git.deleteBranch(branchName);
            deletingBranch = null;
            deleteConfirmName = '';
        } catch (e) {
            // Error is handled by store
        }
    }

    async function handleFetch() {
        await git.fetch();
    }

    function cancelDelete() {
        deletingBranch = null;
        deleteConfirmName = '';
    }
</script>

<div class="p-4">
    <!-- Header actions -->
    <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
        <!-- Search -->
        <div class="flex-1 min-w-[200px] max-w-md">
            <div class="relative">
                <svg class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                </svg>
                <input
                    type="text"
                    bind:value={searchQuery}
                    placeholder="Search branches..."
                    class="w-full pl-10 pr-4 py-2 bg-muted border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                />
            </div>
        </div>

        <!-- Filter tabs -->
        <div class="flex gap-1 bg-muted rounded-lg p-1">
            <button
                onclick={() => filter = 'local'}
                class="px-3 py-1.5 text-xs font-medium rounded-md transition-colors {filter === 'local' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
            >
                Local ({$localBranches.length})
            </button>
            <button
                onclick={() => filter = 'remote'}
                class="px-3 py-1.5 text-xs font-medium rounded-md transition-colors {filter === 'remote' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
            >
                Remote ({$remoteBranches.length})
            </button>
            <button
                onclick={() => filter = 'all'}
                class="px-3 py-1.5 text-xs font-medium rounded-md transition-colors {filter === 'all' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
            >
                All ({$branches.length})
            </button>
        </div>

        <!-- Actions -->
        <div class="flex gap-2">
            <button
                onclick={handleFetch}
                disabled={$gitLoading}
                class="px-3 py-2 text-sm bg-muted text-foreground border border-border rounded-lg hover:bg-accent transition-colors disabled:opacity-50 flex items-center gap-2"
            >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
                Fetch
            </button>
            <button
                onclick={() => showCreateForm = !showCreateForm}
                class="px-3 py-2 text-sm bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity flex items-center gap-2"
            >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
                </svg>
                New Branch
            </button>
        </div>
    </div>

    <!-- Create branch form -->
    {#if showCreateForm}
        <div class="mb-4 p-4 bg-muted/50 border border-border rounded-lg">
            <h3 class="text-sm font-medium text-foreground mb-3">Create New Branch</h3>

            {#if createError}
                <div class="mb-3 p-2 bg-destructive/10 border border-destructive/30 rounded text-destructive text-sm">
                    {createError}
                </div>
            {/if}

            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                    <label for="new-branch-name" class="block text-xs text-muted-foreground mb-1">Branch Name <span class="text-destructive">*</span></label>
                    <input
                        type="text"
                        id="new-branch-name"
                        bind:value={newBranchName}
                        placeholder="feature/my-feature"
                        class="w-full px-3 py-2 bg-muted border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                    />
                </div>
                <div>
                    <label for="branch-start-point" class="block text-xs text-muted-foreground mb-1">Start Point (optional)</label>
                    <input
                        type="text"
                        id="branch-start-point"
                        bind:value={newBranchStartPoint}
                        placeholder="main, HEAD, commit SHA..."
                        class="w-full px-3 py-2 bg-muted border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                    />
                </div>
            </div>

            <div class="flex justify-end gap-2 mt-3">
                <button
                    onclick={() => { showCreateForm = false; newBranchName = ''; newBranchStartPoint = ''; createError = null; }}
                    class="px-3 py-1.5 text-sm bg-muted text-foreground rounded-lg hover:bg-accent transition-colors"
                >
                    Cancel
                </button>
                <button
                    onclick={handleCreateBranch}
                    disabled={!newBranchName.trim() || creating}
                    class="px-3 py-1.5 text-sm bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50"
                >
                    {creating ? 'Creating...' : 'Create Branch'}
                </button>
            </div>
        </div>
    {/if}

    <!-- Branch list -->
    {#if $gitLoading && filteredBranches().length === 0}
        <div class="flex items-center justify-center py-12">
            <svg class="w-6 h-6 animate-spin text-primary" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            <span class="ml-2 text-muted-foreground">Loading branches...</span>
        </div>
    {:else if filteredBranches().length === 0}
        <div class="text-center py-12 text-muted-foreground">
            <svg class="w-12 h-12 mx-auto mb-3 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M6 3v12m0 0c0 2.5 2 3 3.5 3s3.5-.5 3.5-3m-7 0c0-2.5 2-3 3.5-3s3.5.5 3.5 3m0 0v-6m0 0c0-2.5-2-3-3.5-3S6 6.5 6 9" />
            </svg>
            <p>No branches found</p>
            {#if searchQuery}
                <p class="text-sm mt-1">Try a different search term</p>
            {/if}
        </div>
    {:else}
        <div class="space-y-2">
            {#each filteredBranches() as branch (branch.name)}
                <div
                    class="flex items-center justify-between p-3 bg-card border border-border rounded-lg hover:border-primary/30 transition-colors group {branch.current ? 'border-primary/50 bg-primary/5' : ''}"
                >
                    <div class="flex items-center gap-3 min-w-0">
                        <!-- Current indicator -->
                        {#if branch.current}
                            <div class="w-2 h-2 rounded-full bg-primary shrink-0"></div>
                        {:else}
                            <div class="w-2 h-2 rounded-full bg-muted-foreground/30 shrink-0"></div>
                        {/if}

                        <!-- Branch info -->
                        <div class="min-w-0">
                            <div class="flex items-center gap-2 flex-wrap">
                                <span class="font-medium text-foreground truncate">{branch.name}</span>
                                {#if branch.current}
                                    <span class="px-1.5 py-0.5 text-[10px] uppercase bg-primary/15 text-primary rounded">HEAD</span>
                                {/if}
                                {#if branch.remote}
                                    <span class="px-1.5 py-0.5 text-[10px] uppercase bg-muted text-muted-foreground rounded">remote</span>
                                {/if}
                            </div>

                            {#if branch.upstream || (branch.ahead !== undefined && branch.behind !== undefined)}
                                <div class="flex items-center gap-2 mt-0.5 text-xs text-muted-foreground">
                                    {#if branch.upstream}
                                        <span class="truncate">{branch.upstream}</span>
                                    {/if}
                                    {#if branch.ahead !== undefined && branch.ahead > 0}
                                        <span class="text-green-500">{branch.ahead} ahead</span>
                                    {/if}
                                    {#if branch.behind !== undefined && branch.behind > 0}
                                        <span class="text-amber-500">{branch.behind} behind</span>
                                    {/if}
                                </div>
                            {/if}

                            {#if branch.lastCommit}
                                <p class="text-xs text-muted-foreground mt-0.5 truncate">{branch.lastCommit}</p>
                            {/if}
                        </div>
                    </div>

                    <!-- Actions -->
                    <div class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
                        {#if !branch.current && !branch.remote}
                            <button
                                onclick={() => handleCheckout(branch)}
                                disabled={$gitLoading}
                                class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
                                title="Checkout"
                            >
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
                                </svg>
                            </button>
                        {/if}

                        {#if !branch.current && !branch.remote}
                            {#if deletingBranch === branch.name}
                                <!-- Delete confirmation -->
                                <div class="flex items-center gap-2 bg-destructive/10 rounded-lg px-2 py-1">
                                    <input
                                        type="text"
                                        bind:value={deleteConfirmName}
                                        placeholder="Type branch name"
                                        class="w-24 px-2 py-1 text-xs bg-transparent border-b border-destructive/50 text-foreground focus:outline-none"
                                    />
                                    <button
                                        onclick={() => handleDelete(branch.name)}
                                        disabled={deleteConfirmName !== branch.name}
                                        class="p-1 text-destructive hover:text-destructive/80 disabled:opacity-50"
                                        aria-label="Confirm delete"
                                    >
                                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                                        </svg>
                                    </button>
                                    <button
                                        onclick={cancelDelete}
                                        class="p-1 text-muted-foreground hover:text-foreground"
                                        aria-label="Cancel"
                                    >
                                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                                        </svg>
                                    </button>
                                </div>
                            {:else}
                                <button
                                    onclick={() => deletingBranch = branch.name}
                                    class="p-1.5 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
                                    title="Delete"
                                >
                                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                                    </svg>
                                </button>
                            {/if}
                        {/if}
                    </div>
                </div>
            {/each}
        </div>
    {/if}
</div>

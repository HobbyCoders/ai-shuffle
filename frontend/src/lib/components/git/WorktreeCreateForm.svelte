<script lang="ts">
    import { git, localBranches } from '$lib/stores/git';
    import type { Branch, Worktree } from '$lib/stores/git';
    import type { Profile, Session } from '$lib/api/client';

    interface Props {
        projectId: string;
        branches: Branch[];
        profiles?: Profile[];
        onCreated: (worktree: Worktree, session: Session | null) => void;
        onCancel: () => void;
    }

    let { projectId, branches, profiles = [], onCreated, onCancel }: Props = $props();

    // Form state
    let createNewBranch = $state(true);
    let newBranchName = $state('');
    let baseBranch = $state('');
    let existingBranch = $state('');
    let selectedProfileId = $state('');
    let creating = $state(false);
    let error = $state<string | null>(null);

    // Validation
    const branchNameValid = $derived(() => {
        if (!createNewBranch) return true;
        const name = newBranchName.trim();
        if (!name) return false;
        // Git branch name rules: no spaces, no .., no leading/trailing slashes
        if (name.includes(' ') || name.includes('..') || name.startsWith('/') || name.endsWith('/')) {
            return false;
        }
        // No special characters except - _ /
        if (!/^[a-zA-Z0-9\-_\/]+$/.test(name)) {
            return false;
        }
        // Check it doesn't already exist
        const existingBranchNames = branches.map(b => b.name);
        if (existingBranchNames.includes(name)) {
            return false;
        }
        return true;
    });

    const formValid = $derived(() => {
        if (createNewBranch) {
            return branchNameValid() && newBranchName.trim().length > 0;
        } else {
            return existingBranch.length > 0;
        }
    });

    // Available branches for existing branch selection (non-remote, not already in worktree)
    const availableExistingBranches = $derived(() => {
        const worktreeBranches = new Set($localBranches.map(w => w.name));
        return branches.filter(b => !b.remote && !worktreeBranches.has(b.name));
    });

    // Available base branches for creating new branch
    const availableBaseBranches = $derived(() => {
        return branches.filter(b => !b.remote);
    });

    // Get main/master as default base branch
    $effect(() => {
        if (!baseBranch) {
            const mainBranch = branches.find(b => b.name === 'main' || b.name === 'master');
            if (mainBranch) {
                baseBranch = mainBranch.name;
            } else if (branches.length > 0) {
                baseBranch = branches[0].name;
            }
        }
    });

    // Get default profile
    $effect(() => {
        if (!selectedProfileId && profiles.length > 0) {
            // Try to find a default or first profile
            const defaultProfile = profiles.find(p => p.id === 'default') || profiles[0];
            if (defaultProfile) {
                selectedProfileId = defaultProfile.id;
            }
        }
    });

    async function handleSubmit() {
        if (!formValid()) return;

        const branchName = createNewBranch ? newBranchName.trim() : existingBranch;

        creating = true;
        error = null;

        try {
            const worktree = await git.createWorktree(
                branchName,
                createNewBranch,
                createNewBranch ? (baseBranch || undefined) : undefined
            );

            // Call onCreated with the new worktree
            // Note: Session creation is handled by the backend when creating worktree
            if (worktree) {
                onCreated(worktree, null);
            } else {
                error = 'Failed to create worktree - no project selected';
            }
        } catch (e) {
            const err = e as { detail?: string };
            error = err.detail || 'Failed to create worktree';
        } finally {
            creating = false;
        }
    }

    function handleKeydown(event: KeyboardEvent) {
        if (event.key === 'Escape') {
            onCancel();
        } else if (event.key === 'Enter' && formValid() && !creating) {
            handleSubmit();
        }
    }
</script>

<div
    class="p-4 bg-muted/50 border border-border rounded-lg"
    onkeydown={handleKeydown}
    role="form"
>
    <h4 class="text-sm font-medium text-foreground mb-4">Create New Worktree</h4>

    {#if error}
        <div class="mb-4 p-3 bg-destructive/10 border border-destructive/30 rounded-lg flex items-start gap-2">
            <svg class="w-4 h-4 text-destructive shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <span class="text-sm text-destructive">{error}</span>
        </div>
    {/if}

    <div class="space-y-4">
        <!-- Branch type selection -->
        <div class="flex gap-2">
            <button
                type="button"
                onclick={() => createNewBranch = true}
                class="flex-1 px-3 py-2 text-sm font-medium rounded-lg border transition-colors {createNewBranch ? 'bg-primary text-primary-foreground border-primary' : 'bg-card text-muted-foreground border-border hover:text-foreground'}"
            >
                New Branch
            </button>
            <button
                type="button"
                onclick={() => createNewBranch = false}
                class="flex-1 px-3 py-2 text-sm font-medium rounded-lg border transition-colors {!createNewBranch ? 'bg-primary text-primary-foreground border-primary' : 'bg-card text-muted-foreground border-border hover:text-foreground'}"
            >
                Existing Branch
            </button>
        </div>

        {#if createNewBranch}
            <!-- New branch form -->
            <div class="space-y-3">
                <div>
                    <label for="new-branch-name" class="block text-xs text-muted-foreground mb-1">
                        Branch Name <span class="text-destructive">*</span>
                    </label>
                    <input
                        id="new-branch-name"
                        type="text"
                        bind:value={newBranchName}
                        placeholder="feature/my-new-feature"
                        class="w-full px-3 py-2 bg-muted border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 {newBranchName && !branchNameValid() ? 'border-destructive' : 'border-border'}"
                    />
                    {#if newBranchName && !branchNameValid()}
                        <p class="text-xs text-destructive mt-1">
                            {#if branches.some(b => b.name === newBranchName.trim())}
                                Branch already exists
                            {:else}
                                Invalid branch name. Use only letters, numbers, -, _, /
                            {/if}
                        </p>
                    {/if}
                </div>

                <div>
                    <label for="base-branch" class="block text-xs text-muted-foreground mb-1">
                        Base Branch
                    </label>
                    <select
                        id="base-branch"
                        bind:value={baseBranch}
                        class="w-full px-3 py-2 bg-muted border border-border rounded-lg text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                    >
                        {#each availableBaseBranches() as branch}
                            <option value={branch.name}>{branch.name}</option>
                        {/each}
                    </select>
                    <p class="text-xs text-muted-foreground mt-1">
                        New branch will be created from this branch
                    </p>
                </div>
            </div>
        {:else}
            <!-- Existing branch form -->
            <div>
                <label for="existing-branch" class="block text-xs text-muted-foreground mb-1">
                    Select Branch <span class="text-destructive">*</span>
                </label>
                <select
                    id="existing-branch"
                    bind:value={existingBranch}
                    class="w-full px-3 py-2 bg-muted border border-border rounded-lg text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                >
                    <option value="">Select a branch...</option>
                    {#each availableExistingBranches() as branch}
                        <option value={branch.name}>{branch.name}</option>
                    {/each}
                </select>
                {#if availableExistingBranches().length === 0}
                    <p class="text-xs text-amber-600 mt-1">
                        No available branches. All local branches already have worktrees.
                    </p>
                {/if}
            </div>
        {/if}

        <!-- Profile selection (optional) -->
        {#if profiles.length > 0}
            <div>
                <label for="profile-select" class="block text-xs text-muted-foreground mb-1">
                    Profile for Session
                </label>
                <select
                    id="profile-select"
                    bind:value={selectedProfileId}
                    class="w-full px-3 py-2 bg-muted border border-border rounded-lg text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                >
                    {#each profiles as profile}
                        <option value={profile.id}>{profile.name}</option>
                    {/each}
                </select>
                <p class="text-xs text-muted-foreground mt-1">
                    A new session will be created with this profile
                </p>
            </div>
        {/if}

        <!-- Info box -->
        <div class="p-3 bg-primary/5 border border-primary/20 rounded-lg">
            <div class="flex items-start gap-2">
                <svg class="w-4 h-4 text-primary shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <div class="text-xs text-muted-foreground">
                    <p class="font-medium text-foreground mb-1">What happens when you create a worktree:</p>
                    <ul class="list-disc list-inside space-y-0.5">
                        <li>A new directory is created for the branch</li>
                        <li>The branch is checked out in that directory</li>
                        <li>You can work on multiple branches simultaneously</li>
                    </ul>
                </div>
            </div>
        </div>
    </div>

    <!-- Actions -->
    <div class="flex justify-end gap-2 mt-4 pt-4 border-t border-border">
        <button
            type="button"
            onclick={onCancel}
            class="px-4 py-2 text-sm bg-muted text-foreground rounded-lg hover:bg-accent transition-colors"
        >
            Cancel
        </button>
        <button
            type="button"
            onclick={handleSubmit}
            disabled={!formValid() || creating}
            class="px-4 py-2 text-sm bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50"
        >
            {#if creating}
                <span class="flex items-center gap-2">
                    <svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    Creating...
                </span>
            {:else}
                Create Worktree
            {/if}
        </button>
    </div>
</div>

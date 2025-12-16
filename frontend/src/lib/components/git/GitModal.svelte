<script lang="ts">
    import UniversalModal from '../UniversalModal.svelte';
    import GitGraph from './GitGraph.svelte';
    import BranchList from './BranchList.svelte';
    import WorktreeList from './WorktreeList.svelte';
    import GitHubPanel from './GitHubPanel.svelte';
    import { git, gitStatus, gitLoading, gitError } from '$lib/stores/git';

    interface Props {
        open: boolean;
        projectId: string | null;
        onClose: () => void;
        onOpenSession?: (sessionId: string) => void;
    }

    let { open, projectId, onClose, onOpenSession }: Props = $props();

    let activeTab = $state('graph');

    const tabs = [
        { id: 'graph', label: 'History' },
        { id: 'branches', label: 'Branches' },
        { id: 'worktrees', label: 'Worktrees' },
        { id: 'github', label: 'GitHub' }
    ];

    // Git branch icon SVG path
    const gitIcon = "M6 3v12m0 0c0 2.5 2 3 3.5 3s3.5-.5 3.5-3m-7 0c0-2.5 2-3 3.5-3s3.5.5 3.5 3m0 0v-6m0 0c0-2.5-2-3-3.5-3S6 6.5 6 9";

    $effect(() => {
        if (open && projectId) {
            git.loadRepository(projectId);
        }
    });

    function handleTabChange(tabId: string) {
        activeTab = tabId;
    }

    function handleRefresh() {
        if (projectId) {
            git.loadRepository(projectId);
        }
    }
</script>

<UniversalModal
    {open}
    title="Git & GitHub"
    icon={gitIcon}
    {tabs}
    {activeTab}
    onTabChange={handleTabChange}
    {onClose}
    showFooter={false}
    size="xl"
>
    <!-- Error banner -->
    {#if $gitError}
        <div class="mx-6 mt-4 p-3 bg-destructive/10 border border-destructive/30 rounded-lg flex items-center justify-between">
            <div class="flex items-center gap-2 text-destructive">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <span class="text-sm">{$gitError}</span>
            </div>
            <button
                onclick={() => git.clearError()}
                class="text-destructive hover:text-destructive/80"
            >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                </svg>
            </button>
        </div>
    {/if}

    <!-- Status bar -->
    {#if $gitStatus}
        <div class="px-6 py-3 border-b border-border bg-muted/30 flex items-center justify-between flex-wrap gap-2">
            <div class="flex items-center gap-4 text-sm">
                <!-- Current branch -->
                <div class="flex items-center gap-1.5">
                    <svg class="w-4 h-4 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={gitIcon} />
                    </svg>
                    <span class="font-medium text-foreground">{$gitStatus.current_branch || 'detached'}</span>
                </div>

                <!-- Changes indicator -->
                {#if $gitStatus.has_changes}
                    <div class="flex items-center gap-2 text-muted-foreground">
                        {#if $gitStatus.staged > 0}
                            <span class="text-green-500">+{$gitStatus.staged} staged</span>
                        {/if}
                        {#if $gitStatus.modified > 0}
                            <span class="text-yellow-500">~{$gitStatus.modified} modified</span>
                        {/if}
                        {#if $gitStatus.untracked > 0}
                            <span class="text-muted-foreground">?{$gitStatus.untracked} untracked</span>
                        {/if}
                    </div>
                {:else}
                    <span class="text-muted-foreground">Clean</span>
                {/if}
            </div>

            <!-- Refresh button -->
            <button
                onclick={handleRefresh}
                disabled={$gitLoading}
                class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors disabled:opacity-50"
                title="Refresh"
            >
                <svg class="w-4 h-4 {$gitLoading ? 'animate-spin' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
            </button>
        </div>
    {:else if !$gitStatus && !$gitLoading}
        <div class="px-6 py-3 border-b border-border bg-amber-500/10">
            <div class="flex items-center gap-2 text-amber-600 text-sm">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
                <span>Not a git repository or project not selected</span>
            </div>
        </div>
    {/if}

    <!-- Tab content -->
    <div class="min-h-[400px]">
        {#if activeTab === 'graph'}
            <GitGraph {projectId} />
        {:else if activeTab === 'branches'}
            <BranchList {projectId} />
        {:else if activeTab === 'worktrees'}
            <WorktreeList {projectId} onSessionSwitch={onOpenSession} />
        {:else if activeTab === 'github'}
            <GitHubPanel {projectId} />
        {/if}
    </div>
</UniversalModal>

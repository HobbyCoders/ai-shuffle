<script lang="ts">
    import { gitStatus, localBranches } from '$lib/stores/git';
    import {
        github,
        pullRequests,
        workflowRuns,
        githubLoading,
        githubLoadingPRs,
        githubLoadingRuns,
        githubError,
        githubAuthenticated,
        githubRepoName,
        githubRepoInfo
    } from '$lib/stores/github';
    import type { PullRequest, WorkflowRun } from '$lib/stores/github';

    interface Props {
        projectId: string | null;
    }

    let { projectId }: Props = $props();

    // State
    let activeTab = $state<'prs' | 'actions'>('prs');
    let prFilter = $state<'open' | 'closed' | 'all'>('open');
    let showCreatePR = $state(false);

    // Create PR form
    let prTitle = $state('');
    let prBody = $state('');
    let prHead = $state('');
    let prBase = $state('');
    let prCreating = $state(false);
    let prError = $state<string | null>(null);

    // Merge dialog
    let mergingPR = $state<PullRequest | null>(null);
    let mergeMethod = $state<'merge' | 'squash' | 'rebase'>('merge');

    // Load data when project changes
    $effect(() => {
        if (projectId) {
            loadGitHubData();
        }
    });

    async function loadGitHubData() {
        if (!projectId) return;

        const authenticated = await github.loadStatus(projectId);
        if (authenticated) {
            await Promise.all([
                github.loadPullRequests(prFilter),
                github.loadWorkflowRuns()
            ]);
        }
    }

    async function handlePRFilterChange(filter: 'open' | 'closed' | 'all') {
        prFilter = filter;
        await github.loadPullRequests(filter);
    }

    async function handleCreatePR() {
        if (!prTitle.trim() || !prHead || !prBase) return;

        prCreating = true;
        prError = null;

        try {
            await github.createPullRequest({
                title: prTitle.trim(),
                body: prBody.trim(),
                head: prHead,
                base: prBase
            });
            resetCreateForm();
        } catch (e) {
            const error = e as { detail?: string };
            prError = error.detail || 'Failed to create pull request';
        } finally {
            prCreating = false;
        }
    }

    async function handleMergePR() {
        if (!mergingPR) return;

        try {
            await github.mergePullRequest(mergingPR.number, mergeMethod);
            mergingPR = null;
        } catch (e) {
            // Error handled by store
        }
    }

    async function handleClosePR(pr: PullRequest) {
        if (!confirm(`Close PR #${pr.number}: ${pr.title}?`)) return;

        try {
            await github.closePullRequest(pr.number);
        } catch (e) {
            // Error handled by store
        }
    }

    async function handleRerunWorkflow(run: WorkflowRun) {
        try {
            await github.rerunWorkflow(run.id);
        } catch (e) {
            // Error handled by store
        }
    }

    function resetCreateForm() {
        showCreatePR = false;
        prTitle = '';
        prBody = '';
        prHead = '';
        prBase = '';
        prError = null;
    }

    function getStatusColor(status: string): string {
        switch (status) {
            case 'open':
                return 'text-green-500 bg-green-500/15';
            case 'closed':
                return 'text-destructive bg-destructive/15';
            case 'merged':
                return 'text-violet-500 bg-violet-500/15';
            default:
                return 'text-muted-foreground bg-muted';
        }
    }

    function getRunStatusColor(status: string, conclusion: string | null): string {
        if (status === 'in_progress' || status === 'queued') {
            return 'text-amber-500 bg-amber-500/15';
        }
        if (conclusion === 'success') {
            return 'text-green-500 bg-green-500/15';
        }
        if (conclusion === 'failure') {
            return 'text-destructive bg-destructive/15';
        }
        if (conclusion === 'cancelled') {
            return 'text-muted-foreground bg-muted';
        }
        return 'text-muted-foreground bg-muted';
    }

    function formatDate(dateStr: string): string {
        const date = new Date(dateStr);
        return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function formatRelativeTime(dateStr: string): string {
        const date = new Date(dateStr);
        const now = new Date();
        const diffMs = now.getTime() - date.getTime();
        const diffMins = Math.floor(diffMs / 60000);
        const diffHours = Math.floor(diffMins / 60);
        const diffDays = Math.floor(diffHours / 24);

        if (diffMins < 1) return 'just now';
        if (diffMins < 60) return `${diffMins}m ago`;
        if (diffHours < 24) return `${diffHours}h ago`;
        if (diffDays < 7) return `${diffDays}d ago`;
        return date.toLocaleDateString();
    }

    // Extract GitHub info from remote URL for fallback display
    function getGitHubInfo(remoteUrl: string | null | undefined): { owner: string; repo: string } | null {
        if (!remoteUrl) return null;

        let match = remoteUrl.match(/github\.com[:/]([^/]+)\/([^/.]+)(?:\.git)?$/);
        if (match) {
            return { owner: match[1], repo: match[2] };
        }

        return null;
    }

    const githubInfo = $derived(getGitHubInfo($gitStatus?.remote_url));
    const repoUrl = $derived($githubRepoName ? `https://github.com/${$githubRepoName}` : (githubInfo ? `https://github.com/${githubInfo.owner}/${githubInfo.repo}` : null));
</script>

<div class="p-4">
    {#if !$gitStatus?.is_git_repo}
        <div class="text-center py-12 text-muted-foreground">
            <svg class="w-12 h-12 mx-auto mb-3 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
            <p>Not a git repository</p>
            <p class="text-sm mt-1">Initialize a git repository to use GitHub features</p>
        </div>
    {:else if !githubInfo && !$githubRepoName}
        <div class="text-center py-12 text-muted-foreground">
            <svg class="w-12 h-12 mx-auto mb-3 opacity-50" fill="currentColor" viewBox="0 0 24 24">
                <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
            </svg>
            <p>No GitHub remote detected</p>
            <p class="text-sm mt-1">Add a GitHub remote to enable integration features</p>
            {#if $gitStatus?.remote_url}
                <p class="text-xs mt-2 font-mono text-muted-foreground/70">{$gitStatus.remote_url}</p>
            {/if}
        </div>
    {:else}
        <!-- GitHub repo info header -->
        <div class="mb-6 p-4 bg-card border border-border rounded-lg">
            <div class="flex items-center gap-3 flex-wrap">
                <svg class="w-8 h-8 shrink-0" fill="currentColor" viewBox="0 0 24 24">
                    <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
                </svg>
                <div class="flex-1 min-w-0">
                    <h3 class="font-medium text-foreground truncate">
                        {$githubRepoName || (githubInfo ? `${githubInfo.owner}/${githubInfo.repo}` : 'Unknown')}
                    </h3>
                    <div class="flex items-center gap-3 flex-wrap">
                        <a
                            href={repoUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            class="text-sm text-primary hover:underline"
                        >
                            Open in GitHub
                        </a>
                        {#if $githubAuthenticated}
                            <span class="inline-flex items-center gap-1 text-xs text-green-500">
                                <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                                </svg>
                                Authenticated
                            </span>
                        {:else}
                            <span class="inline-flex items-center gap-1 text-xs text-amber-500">
                                <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                                </svg>
                                Not authenticated
                            </span>
                        {/if}
                    </div>
                </div>
                <button
                    onclick={loadGitHubData}
                    disabled={$githubLoading}
                    class="p-2 text-muted-foreground hover:text-foreground hover:bg-muted rounded-lg transition-colors disabled:opacity-50"
                    title="Refresh"
                >
                    <svg class="w-5 h-5 {$githubLoading ? 'animate-spin' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                    </svg>
                </button>
            </div>
        </div>

        <!-- Error display -->
        {#if $githubError}
            <div class="mb-4 p-3 bg-destructive/10 border border-destructive/30 rounded-lg flex items-start gap-2">
                <svg class="w-4 h-4 text-destructive shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <div class="flex-1">
                    <p class="text-sm text-destructive">{$githubError}</p>
                </div>
                <button
                    onclick={() => github.clearError()}
                    class="text-destructive hover:text-destructive/80"
                >
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                </button>
            </div>
        {/if}

        {#if !$githubAuthenticated}
            <!-- Authentication required message -->
            <div class="p-6 bg-muted/50 border border-border rounded-lg text-center">
                <svg class="w-12 h-12 mx-auto mb-3 text-muted-foreground/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                </svg>
                <p class="text-foreground font-medium">GitHub CLI not authenticated</p>
                <p class="text-sm text-muted-foreground mt-1 mb-4">
                    Authenticate with GitHub CLI to manage pull requests and view actions.
                </p>
                <code class="block text-xs bg-muted px-3 py-2 rounded text-foreground font-mono">
                    gh auth login
                </code>
            </div>
        {:else}
            <!-- Tabs -->
            <div class="flex gap-1 bg-muted rounded-lg p-1 mb-4">
                <button
                    onclick={() => activeTab = 'prs'}
                    class="flex-1 px-3 py-2 text-sm font-medium rounded-md transition-colors {activeTab === 'prs' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
                >
                    Pull Requests
                    {#if $pullRequests.length > 0}
                        <span class="ml-1.5 px-1.5 py-0.5 text-xs bg-primary/15 text-primary rounded">{$pullRequests.length}</span>
                    {/if}
                </button>
                <button
                    onclick={() => activeTab = 'actions'}
                    class="flex-1 px-3 py-2 text-sm font-medium rounded-md transition-colors {activeTab === 'actions' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
                >
                    Actions
                    {#if $workflowRuns.some(r => r.status === 'in_progress')}
                        <span class="ml-1.5 w-2 h-2 bg-amber-500 rounded-full inline-block animate-pulse"></span>
                    {/if}
                </button>
            </div>

            {#if activeTab === 'prs'}
                <!-- Pull Requests Tab -->
                <div class="space-y-4">
                    <!-- Actions bar -->
                    <div class="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3">
                        <!-- Filter -->
                        <div class="flex gap-1 bg-muted/50 rounded-lg p-0.5">
                            <button
                                onclick={() => handlePRFilterChange('open')}
                                class="px-3 py-1.5 text-xs font-medium rounded-md transition-colors {prFilter === 'open' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
                            >
                                Open
                            </button>
                            <button
                                onclick={() => handlePRFilterChange('closed')}
                                class="px-3 py-1.5 text-xs font-medium rounded-md transition-colors {prFilter === 'closed' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
                            >
                                Closed
                            </button>
                            <button
                                onclick={() => handlePRFilterChange('all')}
                                class="px-3 py-1.5 text-xs font-medium rounded-md transition-colors {prFilter === 'all' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
                            >
                                All
                            </button>
                        </div>

                        <!-- Create PR button -->
                        <button
                            onclick={() => showCreatePR = !showCreatePR}
                            class="px-3 py-2 text-sm bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity flex items-center justify-center gap-2"
                        >
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
                            </svg>
                            New PR
                        </button>
                    </div>

                    <!-- Create PR form -->
                    {#if showCreatePR}
                        <div class="p-4 bg-muted/50 border border-border rounded-lg">
                            <h4 class="text-sm font-medium text-foreground mb-3">Create Pull Request</h4>

                            {#if prError}
                                <div class="mb-3 p-2 bg-destructive/10 border border-destructive/30 rounded text-destructive text-sm">
                                    {prError}
                                </div>
                            {/if}

                            <div class="space-y-3">
                                <div>
                                    <label class="block text-xs text-muted-foreground mb-1">Title <span class="text-destructive">*</span></label>
                                    <input
                                        type="text"
                                        bind:value={prTitle}
                                        placeholder="Add amazing feature"
                                        class="w-full px-3 py-2 bg-muted border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                                    />
                                </div>

                                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                    <div>
                                        <label class="block text-xs text-muted-foreground mb-1">Head Branch <span class="text-destructive">*</span></label>
                                        <select
                                            bind:value={prHead}
                                            class="w-full px-3 py-2 bg-muted border border-border rounded-lg text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                                        >
                                            <option value="">Select branch...</option>
                                            {#each $localBranches as branch}
                                                <option value={branch.name}>{branch.name}</option>
                                            {/each}
                                        </select>
                                    </div>
                                    <div>
                                        <label class="block text-xs text-muted-foreground mb-1">Base Branch <span class="text-destructive">*</span></label>
                                        <select
                                            bind:value={prBase}
                                            class="w-full px-3 py-2 bg-muted border border-border rounded-lg text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
                                        >
                                            <option value="">Select branch...</option>
                                            <option value="main">main</option>
                                            <option value="master">master</option>
                                            {#each $localBranches.filter(b => b.name !== 'main' && b.name !== 'master') as branch}
                                                <option value={branch.name}>{branch.name}</option>
                                            {/each}
                                        </select>
                                    </div>
                                </div>

                                <div>
                                    <label class="block text-xs text-muted-foreground mb-1">Description</label>
                                    <textarea
                                        bind:value={prBody}
                                        rows="3"
                                        placeholder="Describe your changes..."
                                        class="w-full px-3 py-2 bg-muted border border-border rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-y"
                                    ></textarea>
                                </div>

                                <div class="flex justify-end gap-2">
                                    <button
                                        onclick={resetCreateForm}
                                        class="px-3 py-1.5 text-sm bg-muted text-foreground rounded-lg hover:bg-accent transition-colors"
                                    >
                                        Cancel
                                    </button>
                                    <button
                                        onclick={handleCreatePR}
                                        disabled={!prTitle.trim() || !prHead || !prBase || prCreating}
                                        class="px-3 py-1.5 text-sm bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50"
                                    >
                                        {prCreating ? 'Creating...' : 'Create PR'}
                                    </button>
                                </div>
                            </div>
                        </div>
                    {/if}

                    <!-- PR list -->
                    {#if $githubLoadingPRs && $pullRequests.length === 0}
                        <!-- Loading skeleton -->
                        <div class="space-y-2">
                            {#each [1, 2, 3] as i}
                                <div class="p-3 bg-card border border-border rounded-lg animate-pulse">
                                    <div class="flex items-start gap-3">
                                        <div class="w-12 h-4 bg-muted rounded"></div>
                                        <div class="flex-1 space-y-2">
                                            <div class="h-4 bg-muted rounded w-3/4"></div>
                                            <div class="h-3 bg-muted rounded w-1/2"></div>
                                        </div>
                                    </div>
                                </div>
                            {/each}
                        </div>
                    {:else if $pullRequests.length === 0}
                        <div class="text-center py-8 text-muted-foreground">
                            <svg class="w-10 h-10 mx-auto mb-2 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
                            </svg>
                            <p>No {prFilter === 'all' ? '' : prFilter} pull requests</p>
                            <p class="text-xs mt-1">Create a PR to start collaborating</p>
                        </div>
                    {:else}
                        <div class="space-y-2">
                            {#each $pullRequests as pr (pr.number)}
                                <div class="p-3 bg-card border border-border rounded-lg hover:border-primary/30 transition-colors group">
                                    <div class="flex flex-col sm:flex-row sm:items-start gap-2 sm:gap-3">
                                        <!-- PR info -->
                                        <div class="flex-1 min-w-0">
                                            <div class="flex items-center gap-2 flex-wrap">
                                                <span class="px-1.5 py-0.5 text-[10px] uppercase rounded font-medium {getStatusColor(pr.state)}">
                                                    {pr.state}
                                                </span>
                                                <a
                                                    href={pr.url}
                                                    target="_blank"
                                                    rel="noopener noreferrer"
                                                    class="font-medium text-foreground hover:text-primary transition-colors"
                                                >
                                                    #{pr.number} {pr.title}
                                                </a>
                                            </div>
                                            <div class="flex items-center gap-2 mt-1 text-xs text-muted-foreground flex-wrap">
                                                <span>{pr.head_branch}</span>
                                                <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 8l4 4m0 0l-4 4m4-4H3" />
                                                </svg>
                                                <span>{pr.base_branch}</span>
                                                <span class="text-muted-foreground/60">|</span>
                                                <span>by {pr.author}</span>
                                                <span class="text-muted-foreground/60">|</span>
                                                <span title={formatDate(pr.created_at)}>{formatRelativeTime(pr.created_at)}</span>
                                            </div>
                                        </div>

                                        <!-- Actions -->
                                        <div class="flex items-center gap-1 shrink-0 sm:opacity-0 sm:group-hover:opacity-100 transition-opacity">
                                            {#if pr.state === 'open'}
                                                <button
                                                    onclick={() => { mergingPR = pr; mergeMethod = 'merge'; }}
                                                    class="px-2 py-1 text-xs bg-green-500/15 text-green-600 rounded hover:bg-green-500/25 transition-colors"
                                                >
                                                    Merge
                                                </button>
                                                <button
                                                    onclick={() => handleClosePR(pr)}
                                                    class="p-1 text-muted-foreground hover:text-destructive transition-colors"
                                                    title="Close PR"
                                                >
                                                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                                                    </svg>
                                                </button>
                                            {/if}
                                            <a
                                                href={pr.url}
                                                target="_blank"
                                                rel="noopener noreferrer"
                                                class="p-1 text-muted-foreground hover:text-foreground transition-colors"
                                                title="View on GitHub"
                                            >
                                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                                                </svg>
                                            </a>
                                        </div>
                                    </div>
                                </div>
                            {/each}
                        </div>
                    {/if}
                </div>

            {:else if activeTab === 'actions'}
                <!-- GitHub Actions Tab -->
                <div class="space-y-4">
                    <!-- Refresh button -->
                    <div class="flex justify-end">
                        <button
                            onclick={() => github.loadWorkflowRuns()}
                            disabled={$githubLoadingRuns}
                            class="px-3 py-1.5 text-sm bg-muted text-foreground border border-border rounded-lg hover:bg-accent transition-colors disabled:opacity-50 flex items-center gap-2"
                        >
                            <svg class="w-4 h-4 {$githubLoadingRuns ? 'animate-spin' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                            </svg>
                            Refresh
                        </button>
                    </div>

                    <!-- Workflow runs list -->
                    {#if $githubLoadingRuns && $workflowRuns.length === 0}
                        <!-- Loading skeleton -->
                        <div class="space-y-2">
                            {#each [1, 2, 3] as i}
                                <div class="p-3 bg-card border border-border rounded-lg animate-pulse">
                                    <div class="flex items-center gap-3">
                                        <div class="w-3 h-3 bg-muted rounded-full"></div>
                                        <div class="flex-1 space-y-2">
                                            <div class="h-4 bg-muted rounded w-1/2"></div>
                                            <div class="h-3 bg-muted rounded w-1/3"></div>
                                        </div>
                                    </div>
                                </div>
                            {/each}
                        </div>
                    {:else if $workflowRuns.length === 0}
                        <div class="text-center py-8 text-muted-foreground">
                            <svg class="w-10 h-10 mx-auto mb-2 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                            </svg>
                            <p>No workflow runs</p>
                            <p class="text-xs mt-1">Workflow runs will appear here when triggered</p>
                        </div>
                    {:else}
                        <div class="space-y-2">
                            {#each $workflowRuns as run (run.id)}
                                <div class="p-3 bg-card border border-border rounded-lg hover:border-primary/30 transition-colors group">
                                    <div class="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-3">
                                        <!-- Status indicator -->
                                        <div class="flex items-center gap-2 flex-1 min-w-0">
                                            {#if run.status === 'in_progress'}
                                                <svg class="w-4 h-4 text-amber-500 animate-spin shrink-0" fill="none" viewBox="0 0 24 24">
                                                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                                                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                                                </svg>
                                            {:else if run.conclusion === 'success'}
                                                <svg class="w-4 h-4 text-green-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                                                </svg>
                                            {:else if run.conclusion === 'failure'}
                                                <svg class="w-4 h-4 text-destructive shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                                                </svg>
                                            {:else}
                                                <svg class="w-4 h-4 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                                                </svg>
                                            {/if}

                                            <div class="min-w-0 flex-1">
                                                <a
                                                    href={run.url}
                                                    target="_blank"
                                                    rel="noopener noreferrer"
                                                    class="font-medium text-foreground hover:text-primary transition-colors block truncate"
                                                >
                                                    {run.name}
                                                </a>
                                                <div class="flex items-center gap-2 text-xs text-muted-foreground flex-wrap">
                                                    <span class="px-1.5 py-0.5 rounded {getRunStatusColor(run.status, run.conclusion)}">
                                                        {run.conclusion || run.status}
                                                    </span>
                                                    <span>{run.branch}</span>
                                                    <span class="text-muted-foreground/60">|</span>
                                                    <span>{run.event}</span>
                                                    <span class="text-muted-foreground/60">|</span>
                                                    <span title={formatDate(run.created_at)}>{formatRelativeTime(run.created_at)}</span>
                                                </div>
                                            </div>
                                        </div>

                                        <!-- Actions -->
                                        <div class="flex items-center gap-1 shrink-0 sm:opacity-0 sm:group-hover:opacity-100 transition-opacity">
                                            {#if run.conclusion && run.conclusion !== 'success'}
                                                <button
                                                    onclick={() => handleRerunWorkflow(run)}
                                                    class="px-2 py-1 text-xs bg-muted text-foreground rounded hover:bg-accent transition-colors"
                                                >
                                                    Re-run
                                                </button>
                                            {/if}
                                            <a
                                                href={run.url}
                                                target="_blank"
                                                rel="noopener noreferrer"
                                                class="p-1 text-muted-foreground hover:text-foreground transition-colors"
                                                title="View on GitHub"
                                            >
                                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                                                </svg>
                                            </a>
                                        </div>
                                    </div>
                                </div>
                            {/each}
                        </div>
                    {/if}
                </div>
            {/if}
        {/if}
    {/if}
</div>

<!-- Merge PR Dialog -->
{#if mergingPR}
    <!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
    <div
        class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] bg-black/50 z-50 flex items-center justify-center p-4"
        onclick={() => mergingPR = null}
        role="dialog"
        aria-modal="true"
    >
        <!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
        <div
            class="bg-card rounded-xl w-full max-w-md shadow-xl border border-border"
            onclick={(e) => e.stopPropagation()}
        >
            <div class="p-4 border-b border-border">
                <h3 class="text-lg font-semibold text-foreground">Merge Pull Request</h3>
                <p class="text-sm text-muted-foreground mt-1">
                    #{mergingPR.number}: {mergingPR.title}
                </p>
            </div>

            <div class="p-4 space-y-4">
                <div>
                    <label class="block text-sm font-medium text-foreground mb-2">Merge Method</label>
                    <div class="space-y-2">
                        <label class="flex items-start gap-3 p-3 border border-border rounded-lg cursor-pointer hover:bg-muted/50 transition-colors {mergeMethod === 'merge' ? 'border-primary bg-primary/5' : ''}">
                            <input
                                type="radio"
                                name="mergeMethod"
                                value="merge"
                                bind:group={mergeMethod}
                                class="mt-0.5"
                            />
                            <div>
                                <span class="font-medium text-foreground">Create a merge commit</span>
                                <p class="text-xs text-muted-foreground">All commits will be added to the base branch via a merge commit.</p>
                            </div>
                        </label>
                        <label class="flex items-start gap-3 p-3 border border-border rounded-lg cursor-pointer hover:bg-muted/50 transition-colors {mergeMethod === 'squash' ? 'border-primary bg-primary/5' : ''}">
                            <input
                                type="radio"
                                name="mergeMethod"
                                value="squash"
                                bind:group={mergeMethod}
                                class="mt-0.5"
                            />
                            <div>
                                <span class="font-medium text-foreground">Squash and merge</span>
                                <p class="text-xs text-muted-foreground">All commits will be combined into one commit.</p>
                            </div>
                        </label>
                        <label class="flex items-start gap-3 p-3 border border-border rounded-lg cursor-pointer hover:bg-muted/50 transition-colors {mergeMethod === 'rebase' ? 'border-primary bg-primary/5' : ''}">
                            <input
                                type="radio"
                                name="mergeMethod"
                                value="rebase"
                                bind:group={mergeMethod}
                                class="mt-0.5"
                            />
                            <div>
                                <span class="font-medium text-foreground">Rebase and merge</span>
                                <p class="text-xs text-muted-foreground">All commits will be rebased onto the base branch.</p>
                            </div>
                        </label>
                    </div>
                </div>
            </div>

            <div class="p-4 border-t border-border flex justify-end gap-2">
                <button
                    onclick={() => mergingPR = null}
                    class="px-4 py-2 text-sm bg-muted text-foreground rounded-lg hover:bg-accent transition-colors"
                >
                    Cancel
                </button>
                <button
                    onclick={handleMergePR}
                    disabled={$githubLoading}
                    class="px-4 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors disabled:opacity-50"
                >
                    {$githubLoading ? 'Merging...' : 'Merge'}
                </button>
            </div>
        </div>
    </div>
{/if}

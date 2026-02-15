<script lang="ts">
	/**
	 * GitHeaderActions - Git controls for the PageHeader
	 *
	 * Branch selector dropdown (checkout branches), commit button, and diff stats.
	 * Branch selector is locked during streaming.
	 */
	import { GitBranch, GitCommitHorizontal, GitPullRequest, ChevronDown, Check, Plus, Loader2, Lock, CheckCircle2, ArrowDownToLine, ArrowUpFromLine, FolderGit2 } from 'lucide-svelte';
	import { git, diffStats, gitStatus, branches as branchStore, gitMode } from '$lib/stores/git';
	import { githubRepoInfo } from '$lib/stores/github';

	interface Props {
		projectId: string;
		isStreaming?: boolean;
		isGitRepo?: boolean;
		/** When set, forces worktree read-only mode and uses this as the branch display.
		 *  This ensures the correct branch shows immediately without waiting for the store to load. */
		worktreeBranch?: string | null;
		/** When set, shows the "Finish" button instead of Commit/Create PR */
		openPR?: { number: number; title: string; url: string } | null;
		onClickCommit: () => void;
		onClickCreatePR: () => void;
		onClickFinish: () => void;
		onClickNewBranch: () => void;
		onClickPush: () => void;
		onClickPull: () => void;
		onClickInitRepo?: () => void;
	}

	let { projectId, isStreaming = false, isGitRepo = true, worktreeBranch = null, openPR = null, onClickCommit, onClickCreatePR, onClickFinish, onClickNewBranch, onClickPush, onClickPull, onClickInitRepo }: Props = $props();

	// Worktree mode: determined by prop (immediate) OR store (after load)
	const isWorktreeMode = $derived(!!worktreeBranch || $gitMode === 'worktree');

	// --- Dropdown state ---
	let showDropdown = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);
	let checkingOut = $state(false);

	// Use worktreeBranch prop as immediate fallback while store is loading
	const currentBranch = $derived($gitStatus?.current_branch ?? worktreeBranch ?? null);
	const hasChanges = $derived(
		($diffStats?.additions ?? 0) > 0 ||
		($diffStats?.deletions ?? 0) > 0 ||
		($diffStats?.files_changed ?? 0) > 0
	);
	const additions = $derived($diffStats?.additions ?? 0);
	const deletions = $derived($diffStats?.deletions ?? 0);
	const commitsAhead = $derived($gitStatus?.ahead ?? 0);
	const defaultBranch = $derived($githubRepoInfo?.default_branch ?? 'main');
	const isOnDefaultBranch = $derived(currentBranch === defaultBranch);

	// Behind upstream tracking branch (for pull button)
	const currentBranchInfo = $derived($branchStore.find(b => b.current));
	const commitsBehindUpstream = $derived(currentBranchInfo?.behind ?? 0);
	const commitsAheadUpstream = $derived(currentBranchInfo?.ahead ?? 0);
	const hasUpstream = $derived(!!currentBranchInfo?.upstream);
	const needsPull = $derived(commitsBehindUpstream > 0 && hasUpstream);
	const canPush = $derived(!hasChanges && commitsAheadUpstream > 0 && hasUpstream && !needsPull);
	const canCreatePR = $derived(!hasChanges && commitsAhead > 0 && !isOnDefaultBranch && !needsPull);

	// Local branches for the dropdown (exclude remotes)
	const localBranches = $derived(
		$branchStore.filter(b => !b.remote)
	);

	// Can the user interact with the branch selector?
	// In worktree mode, branch is always locked (read-only display)
	const branchLocked = $derived(isWorktreeMode || isStreaming || checkingOut);

	function toggleDropdown() {
		if (branchLocked) return;
		showDropdown = !showDropdown;
		if (showDropdown) {
			// Refresh branches when opening
			git.loadBranches();
		}
	}

	async function checkoutBranch(branchName: string) {
		if (branchName === currentBranch) {
			showDropdown = false;
			return;
		}
		checkingOut = true;
		showDropdown = false;
		try {
			await git.checkout(branchName);
			// Refresh diff stats after checkout
			await git.refreshDiffStats();
		} catch (e) {
			console.error('Failed to checkout branch:', e);
		} finally {
			checkingOut = false;
		}
	}

	function handleNewBranch() {
		showDropdown = false;
		onClickNewBranch();
	}

	// Close dropdown on outside click
	function handleClickOutside(e: MouseEvent) {
		if (!showDropdown) return;
		if (dropdownRef && !dropdownRef.contains(e.target as Node)) {
			showDropdown = false;
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && showDropdown) {
			showDropdown = false;
		}
	}
</script>

<svelte:window onclick={handleClickOutside} onkeydown={handleKeydown} />

<div class="git-actions">
	{#if isGitRepo}
	<!-- Branch selector -->
	<div class="branch-selector" bind:this={dropdownRef}>
		<button
			class="git-btn branch-btn"
			class:locked={branchLocked}
			onclick={toggleDropdown}
			disabled={branchLocked}
			title={isWorktreeMode ? `Worktree branch: ${currentBranch}` : isStreaming ? 'Branch locked during streaming' : (currentBranch ? `Branch: ${currentBranch}` : 'Select branch')}
		>
			<GitBranch size={14} strokeWidth={1.5} />
			{#if currentBranch}
				<span class="btn-label branch-name">{currentBranch}</span>
			{/if}
			{#if checkingOut}
				<Loader2 size={11} strokeWidth={2} class="spin" />
			{:else if isWorktreeMode}
				<!-- No chevron in worktree mode - branch is fixed -->
			{:else if isStreaming}
				<Lock size={10} strokeWidth={1.5} />
			{:else}
				<ChevronDown size={11} strokeWidth={1.5} />
			{/if}
		</button>

		{#if showDropdown}
			<div class="branch-dropdown">
				{#if localBranches.length > 0}
					<div class="dropdown-section-label">Branches</div>
					{#each localBranches as branch (branch.name)}
						<button
							class="dropdown-item"
							class:active={branch.name === currentBranch}
							onclick={() => checkoutBranch(branch.name)}
						>
							<GitBranch size={13} strokeWidth={1.5} />
							<span class="dropdown-item-name">{branch.name}</span>
							{#if branch.name === currentBranch}
								<Check size={14} strokeWidth={2} />
							{/if}
						</button>
					{/each}
				{:else}
					<p class="dropdown-empty">No local branches</p>
				{/if}

				<div class="dropdown-divider"></div>
				<button class="dropdown-item new-branch-item" onclick={handleNewBranch}>
					<Plus size={13} strokeWidth={1.5} />
					<span class="dropdown-item-name">New branch...</span>
				</button>
			</div>
		{/if}
	</div>

	<!-- Pull button (when behind upstream) -->
	{#if commitsBehindUpstream > 0 && hasUpstream}
		<button
			class="git-btn pull-btn"
			onclick={onClickPull}
			disabled={isStreaming}
			title={isStreaming
				? 'Cannot pull during streaming'
				: `${commitsBehindUpstream} commit${commitsBehindUpstream === 1 ? '' : 's'} behind upstream — pull latest`}
		>
			<ArrowDownToLine size={14} strokeWidth={1.5} />
			<span class="btn-label">Pull</span>
			<span class="behind-badge">{commitsBehindUpstream}</span>
		</button>
	{/if}

	<!-- Push button (when commits ahead of upstream) -->
	{#if canPush}
		<button
			class="git-btn push-btn"
			onclick={onClickPush}
			disabled={isStreaming}
			title={isStreaming
				? 'Cannot push during streaming'
				: `${commitsAheadUpstream} commit${commitsAheadUpstream === 1 ? '' : 's'} to push`}
		>
			<ArrowUpFromLine size={14} strokeWidth={1.5} />
			<span class="btn-label">Push</span>
			<span class="ahead-badge">{commitsAheadUpstream}</span>
		</button>
	{/if}

	<!-- Commit / Create PR / Finish button -->
	{#if hasChanges}
		<button
			class="git-btn"
			onclick={onClickCommit}
			disabled={isStreaming}
			title={isStreaming ? 'Cannot commit during streaming' : 'Commit changes'}
		>
			<GitCommitHorizontal size={14} strokeWidth={1.5} />
			<span class="btn-label">Commit</span>
			<ChevronDown size={11} strokeWidth={1.5} />
		</button>
	{:else if openPR}
		<button
			class="git-btn"
			onclick={onClickFinish}
			disabled={isStreaming}
			title={isStreaming ? 'Cannot finish during streaming' : `PR #${openPR.number}: ${openPR.title} — Merge and finish`}
		>
			<CheckCircle2 size={14} strokeWidth={1.5} />
			<span class="btn-label">Finish</span>
		</button>
	{:else if canCreatePR}
		<button
			class="git-btn"
			onclick={onClickCreatePR}
			disabled={isStreaming}
			title={isStreaming ? 'Cannot create PR during streaming' : `${commitsAhead} commit${commitsAhead === 1 ? '' : 's'} ahead — create a pull request`}
		>
			<GitPullRequest size={14} strokeWidth={1.5} />
			<span class="btn-label">Create PR</span>
			<ChevronDown size={11} strokeWidth={1.5} />
		</button>
	{:else}
		<button
			class="git-btn"
			disabled={true}
			title="No changes to commit"
		>
			<GitCommitHorizontal size={14} strokeWidth={1.5} />
			<span class="btn-label">Commit</span>
		</button>
	{/if}

	<!-- Diff stats -->
	{#if hasChanges}
		<div class="diff-stats">
			{#if additions > 0}
				<span class="stat additions">+{additions}</span>
			{/if}
			{#if deletions > 0}
				<span class="stat deletions">-{deletions}</span>
			{/if}
		</div>
	{/if}

	{:else}
		<!-- No git repo: show Init button -->
		<button
			class="git-btn init-btn"
			onclick={onClickInitRepo}
			title="Initialize a git repository for this project"
		>
			<FolderGit2 size={14} strokeWidth={1.5} />
			<span class="btn-label">Init Repo</span>
		</button>
	{/if}
</div>

<style>
	.git-actions {
		display: flex;
		align-items: center;
		gap: 4px;
	}

	.branch-selector {
		position: relative;
	}

	.git-btn {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 26px;
		padding: 0 8px;
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 6px;
		color: var(--muted-foreground);
		font-size: 0.6875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
		white-space: nowrap;
	}

	.git-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.git-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.git-btn.locked {
		opacity: 0.6;
	}

	.git-btn :global(svg) {
		flex-shrink: 0;
	}

	.init-btn {
		border-color: var(--primary);
		color: var(--primary);
	}

	.init-btn:hover:not(:disabled) {
		background: color-mix(in oklch, var(--primary) 10%, transparent);
	}

	.btn-label {
		line-height: 1;
	}

	.branch-name {
		max-width: 140px;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* Pull behind badge */
	.behind-badge {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 16px;
		height: 16px;
		padding: 0 4px;
		border-radius: 8px;
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		font-size: 0.625rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		line-height: 1;
	}

	/* Push ahead badge */
	.ahead-badge {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 16px;
		height: 16px;
		padding: 0 4px;
		border-radius: 8px;
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		font-size: 0.625rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		line-height: 1;
	}

	/* Branch dropdown */
	.branch-dropdown {
		position: absolute;
		top: calc(100% + 4px);
		right: 0;
		min-width: 220px;
		max-width: 320px;
		max-height: 320px;
		overflow-y: auto;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 10px;
		box-shadow: var(--shadow-l);
		padding: 4px;
		z-index: 100;
		animation: dropdown-in 120ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes dropdown-in {
		from { opacity: 0; transform: translateY(-4px); }
		to { opacity: 1; transform: translateY(0); }
	}

	.dropdown-section-label {
		padding: 6px 10px 2px;
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 7px 10px;
		background: transparent;
		border: none;
		border-radius: 7px;
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		text-align: left;
		transition: background-color 120ms ease;
	}

	.dropdown-item:hover {
		background: var(--hover-overlay);
	}

	.dropdown-item.active {
		font-weight: 500;
	}

	.dropdown-item :global(svg) {
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.dropdown-item.active :global(svg:last-child) {
		color: var(--success);
	}

	.dropdown-item-name {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.dropdown-divider {
		height: 1px;
		background: var(--border);
		margin: 4px 0;
	}

	.dropdown-empty {
		padding: 12px;
		text-align: center;
		color: var(--muted-foreground);
		font-size: 0.75rem;
		margin: 0;
	}

	.new-branch-item {
		color: var(--primary) !important;
	}

	.new-branch-item :global(svg) {
		color: var(--primary) !important;
	}

	/* Diff stats */
	.diff-stats {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 0 4px;
	}

	.stat {
		font-size: 0.6875rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		line-height: 1;
	}

	.stat.additions {
		color: var(--success);
	}

	.stat.deletions {
		color: var(--destructive);
	}

	/* Spin animation */
	:global(.spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	/* Mobile touch targets */
	@media (max-width: 639px) {
		.git-actions {
			flex-wrap: wrap;
		}

		.git-btn {
			height: 36px;
			padding: 0 12px;
			font-size: 0.75rem;
		}

		.branch-name {
			max-width: 80px;
		}

		.branch-dropdown {
			right: auto;
			left: 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.branch-dropdown {
			animation: none;
		}
	}
</style>

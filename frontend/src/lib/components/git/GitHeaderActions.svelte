<script lang="ts">
	/**
	 * GitHeaderActions - Git controls for the PageHeader
	 *
	 * Branch selector dropdown (checkout branches), split action buttons, and diff stats.
	 * Split buttons: main area performs quick action with saved settings,
	 * chevron opens the modal for customization.
	 * Branch selector is locked during streaming.
	 */
	import { GitBranch, GitCommitHorizontal, GitPullRequest, ChevronDown, Check, Plus, Loader2, Lock, CheckCircle2, ArrowDownToLine, ArrowUpFromLine, FolderGit2 } from 'lucide-svelte';
	import { git, diffStats, gitStatus, branches as branchStore, gitMode, changedFiles } from '$lib/stores/git';
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
		/** Quick actions (use saved settings, no modal) */
		onClickCommit: () => void;
		onClickCreatePR: () => void;
		onClickFinish: () => void;
		onClickPush: () => void;
		onClickPull: () => void;
		onClickNewBranch: () => void;
		onClickInitRepo?: () => void;
		/** Open modal for customization */
		onOpenCommitModal: () => void;
		onOpenCreatePRModal: () => void;
		onOpenFinishModal: () => void;
		onOpenPullModal: () => void;
		/** Open diff review panel (side-by-side) */
		onOpenDiffReview?: () => void;
	}

	let { projectId, isStreaming = false, isGitRepo = true, worktreeBranch = null, openPR = null, onClickCommit, onClickCreatePR, onClickFinish, onClickNewBranch, onClickPush, onClickPull, onClickInitRepo, onOpenCommitModal, onOpenCreatePRModal, onOpenFinishModal, onOpenPullModal, onOpenDiffReview }: Props = $props();

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
	const filesChanged = $derived($changedFiles?.length ?? $diffStats?.files_changed ?? 0);
	const commitsAhead = $derived($gitStatus?.ahead ?? 0);
	const defaultBranch = $derived($githubRepoInfo?.default_branch ?? 'main');
	const isOnDefaultBranch = $derived(currentBranch === defaultBranch);

	// Upstream tracking branch state (for push/pull buttons)
	const currentBranchInfo = $derived($branchStore.find(b => b.current));
	const commitsBehindUpstream = $derived(currentBranchInfo?.behind ?? 0);
	const commitsAheadUpstream = $derived(currentBranchInfo?.ahead ?? 0);
	const hasUpstream = $derived(!!currentBranchInfo?.upstream);
	// Diverged = both ahead AND behind (e.g. after rebase/amend or normal development divergence).
	// Always show Pull as the primary action — pull integrates remote changes safely.
	// Force push is available as a secondary option in the Pull modal for rebase recovery.
	const isDiverged = $derived(commitsAheadUpstream > 0 && commitsBehindUpstream > 0 && hasUpstream);
	const needsPull = $derived(commitsBehindUpstream > 0 && hasUpstream);
	const canPush = $derived(!hasChanges && commitsAheadUpstream > 0 && hasUpstream && !needsPull);
	const canCreatePR = $derived(!hasChanges && commitsAhead > 0 && !isOnDefaultBranch && !needsPull);

	// Local branches for the dropdown (exclude remotes)
	const localBranches = $derived(
		$branchStore.filter(b => !b.remote)
	);

	// Remote branches that don't have a local counterpart
	const remoteBranches = $derived(() => {
		const localNames = new Set(localBranches.map(b => b.name));
		return $branchStore
			.filter(b => b.remote)
			.map(b => ({
				...b,
				// Strip "origin/" prefix for display and checkout
				shortName: b.name.replace(/^[^/]+\//, '')
			}))
			// Exclude remotes that already have a local branch (e.g. origin/main when main exists)
			.filter(b => !localNames.has(b.shortName));
	});

	// Search filter for branch dropdown
	let branchSearch = $state('');
	let searchInputRef = $state<HTMLInputElement | null>(null);
	const filteredLocalBranches = $derived(
		branchSearch
			? localBranches.filter(b => b.name.toLowerCase().includes(branchSearch.toLowerCase()))
			: localBranches
	);
	const filteredRemoteBranches = $derived(
		branchSearch
			? remoteBranches().filter(b => b.shortName.toLowerCase().includes(branchSearch.toLowerCase()) || b.name.toLowerCase().includes(branchSearch.toLowerCase()))
			: remoteBranches()
	);
	const showSearch = $derived(localBranches.length + remoteBranches().length > 5);

	// Can the user interact with the branch selector?
	// In worktree mode, branch is always locked (read-only display)
	const branchLocked = $derived(isWorktreeMode || isStreaming || checkingOut);

	function toggleDropdown() {
		if (branchLocked) return;
		showDropdown = !showDropdown;
		if (showDropdown) {
			branchSearch = '';
			// Refresh branches when opening
			git.loadBranches();
			// Focus search input after render
			requestAnimationFrame(() => searchInputRef?.focus());
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
				{#if showSearch}
					<div class="dropdown-search">
						<input
							bind:this={searchInputRef}
							bind:value={branchSearch}
							type="text"
							class="branch-search-input"
							placeholder="Filter branches..."
							onclick={(e) => e.stopPropagation()}
							onkeydown={(e) => { if (e.key === 'Escape' && branchSearch) { branchSearch = ''; e.stopPropagation(); }}}
						/>
					</div>
				{/if}

				{#if filteredLocalBranches.length > 0}
					<div class="dropdown-section-label">Branches</div>
					{#each filteredLocalBranches as branch (branch.name)}
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
				{:else if !branchSearch}
					<p class="dropdown-empty">No local branches</p>
				{/if}

				{#if filteredRemoteBranches.length > 0}
					<div class="dropdown-section-label">Remote</div>
					{#each filteredRemoteBranches as branch (branch.name)}
						<button
							class="dropdown-item remote-item"
							onclick={() => checkoutBranch(branch.shortName)}
							title={branch.name}
						>
							<GitBranch size={13} strokeWidth={1.5} />
							<span class="dropdown-item-name">{branch.shortName}</span>
							<span class="remote-tag">origin</span>
						</button>
					{/each}
				{/if}

				{#if branchSearch && filteredLocalBranches.length === 0 && filteredRemoteBranches.length === 0}
					<p class="dropdown-empty">No matching branches</p>
				{/if}

				<div class="dropdown-divider"></div>
				<button class="dropdown-item new-branch-item" onclick={handleNewBranch}>
					<Plus size={13} strokeWidth={1.5} />
					<span class="dropdown-item-name">New branch...</span>
				</button>
			</div>
		{/if}
	</div>

	<!-- Pull button (when behind upstream, including diverged state) -->
	{#if needsPull}
		<div class="split-btn-group">
			<button
				class="git-btn split-main pull-btn"
				class:diverged={isDiverged}
				onclick={onClickPull}
				disabled={isStreaming}
				title={isStreaming
					? 'Cannot pull during streaming'
					: isDiverged
						? `Quick sync — ${commitsAheadUpstream} ahead, ${commitsBehindUpstream} behind upstream`
						: `Quick pull ${commitsBehindUpstream} commit${commitsBehindUpstream === 1 ? '' : 's'} with saved settings`}
			>
				<ArrowDownToLine size={14} strokeWidth={1.5} />
				<span class="btn-label">{isDiverged ? 'Sync' : 'Pull'}</span>
				<span class="count-badge">{commitsBehindUpstream}</span>
			</button>
			<button
				class="git-btn split-chevron pull-btn"
				class:diverged={isDiverged}
				onclick={onOpenPullModal}
				disabled={isStreaming}
				title="Pull options..."
			>
				<ChevronDown size={11} strokeWidth={1.5} />
			</button>
		</div>
	{/if}

	<!-- Push button (when commits ahead of upstream, not behind) -->
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
			<span class="count-badge">{commitsAheadUpstream}</span>
		</button>
	{/if}

	<!-- Commit / Create PR / Finish button -->
	{#if hasChanges}
		<div class="split-btn-group">
			<button
				class="git-btn split-main"
				onclick={onClickCommit}
				disabled={isStreaming}
				title={isStreaming ? 'Cannot commit during streaming' : 'Quick commit with saved settings'}
			>
				<GitCommitHorizontal size={14} strokeWidth={1.5} />
				<span class="btn-label">Commit</span>
			</button>
			<button
				class="git-btn split-chevron"
				onclick={onOpenCommitModal}
				disabled={isStreaming}
				title="Commit options..."
			>
				<ChevronDown size={11} strokeWidth={1.5} />
			</button>
		</div>
	{:else if openPR}
		<div class="split-btn-group">
			<button
				class="git-btn split-main"
				onclick={onClickFinish}
				disabled={isStreaming}
				title={isStreaming ? 'Cannot finish during streaming' : `Quick merge PR #${openPR.number} with saved settings`}
			>
				<CheckCircle2 size={14} strokeWidth={1.5} />
				<span class="btn-label">Finish</span>
			</button>
			<button
				class="git-btn split-chevron"
				onclick={onOpenFinishModal}
				disabled={isStreaming}
				title="Merge options..."
			>
				<ChevronDown size={11} strokeWidth={1.5} />
			</button>
		</div>
	{:else if canCreatePR}
		<div class="split-btn-group">
			<button
				class="git-btn split-main"
				onclick={onClickCreatePR}
				disabled={isStreaming}
				title={isStreaming ? 'Cannot create PR during streaming' : `Quick create PR (${commitsAhead} commit${commitsAhead === 1 ? '' : 's'} ahead)`}
			>
				<GitPullRequest size={14} strokeWidth={1.5} />
				<span class="btn-label">Create PR</span>
			</button>
			<button
				class="git-btn split-chevron"
				onclick={onOpenCreatePRModal}
				disabled={isStreaming}
				title="PR options..."
			>
				<ChevronDown size={11} strokeWidth={1.5} />
			</button>
		</div>
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

	<!-- Diff stats (clickable to open review panel) -->
	{#if hasChanges}
		<button
			class="diff-stats"
			onclick={onOpenDiffReview}
			title="Review {filesChanged} changed file{filesChanged === 1 ? '' : 's'}"
		>
			{#if additions > 0}
				<span class="stat additions">+{additions}</span>
			{/if}
			{#if deletions > 0}
				<span class="stat deletions">-{deletions}</span>
			{/if}
			<span class="stat files-count">{filesChanged} file{filesChanged === 1 ? '' : 's'}</span>
		</button>
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
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
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

	/* Split button group */
	.split-btn-group {
		display: flex;
		align-items: center;
	}

	.split-btn-group .split-main {
		border-top-right-radius: 0;
		border-bottom-right-radius: 0;
		border-right: none;
	}

	.split-btn-group .split-chevron {
		border-top-left-radius: 0;
		border-bottom-left-radius: 0;
		padding: 0 5px;
		gap: 0;
		border-left: 1px solid color-mix(in oklch, var(--border) 60%, transparent);
	}

	.split-btn-group .split-chevron:hover:not(:disabled) {
		background: color-mix(in oklch, var(--hover-overlay) 80%, transparent);
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

	/* Commit count badge (used in pull/push buttons) */
	.count-badge {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 16px;
		height: 16px;
		padding: 0 4px;
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		font-size: 0.625rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		line-height: 1;
	}

	/* Pull button in diverged state (branch both ahead and behind) */
	.pull-btn.diverged {
		border-color: var(--warning, #e8a735);
		color: var(--warning, #e8a735);
	}

	.pull-btn.diverged:hover:not(:disabled) {
		background: color-mix(in oklch, var(--warning, #e8a735) 10%, transparent);
	}

	.pull-btn.diverged .count-badge {
		background: color-mix(in oklch, var(--warning, #e8a735) 15%, transparent);
		color: var(--warning, #e8a735);
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
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
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
		border-radius: calc(var(--radius, 0.75rem) * 0.58);
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

	/* Search input */
	.dropdown-search {
		padding: 4px;
	}

	.branch-search-input {
		width: 100%;
		height: 28px;
		padding: 0 8px;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--foreground);
		font-size: 0.75rem;
		outline: none;
		transition: border-color 150ms ease;
	}

	.branch-search-input:focus {
		border-color: var(--primary);
	}

	.branch-search-input::placeholder {
		color: var(--muted-foreground);
	}

	/* Remote branch items */
	.remote-item :global(svg) {
		color: var(--muted-foreground);
		opacity: 0.6;
	}

	.remote-tag {
		font-size: 0.5625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted-foreground) 12%, transparent);
		padding: 1px 5px;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		flex-shrink: 0;
	}

	/* Diff stats */
	.diff-stats {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 2px 8px;
		background: none;
		border: 1px solid transparent;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.diff-stats:hover {
		background: var(--hover-bg, rgba(255, 255, 255, 0.06));
		border-color: rgba(255, 255, 255, 0.1);
	}

	.diff-stats:focus-visible {
		outline: 2px solid var(--primary);
		outline-offset: 1px;
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

	.stat.files-count {
		color: var(--muted-fg, rgba(255, 255, 255, 0.45));
		font-size: 0.625rem;
		margin-left: 2px;
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

		.split-btn-group .split-chevron {
			padding: 0 8px;
			min-width: 36px;
			justify-content: center;
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

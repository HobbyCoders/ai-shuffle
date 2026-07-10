<script lang="ts">
	/**
	 * GitHeaderActions - Git controls for the PageHeader
	 *
	 * Branch selector dropdown (checkout branches), split action buttons, and diff
	 * stats. Each mutating action (Commit / Push / Pull / Create PR / Finish /
	 * Ship It) is a split button: the main area runs the action immediately with
	 * saved defaults, and the chevron opens a compact GitActionOptionsDropdown for
	 * per-invocation overrides. Branch selector, diff review, and Init Repo remain
	 * direct git actions. Branch selector is locked during streaming.
	 */
	import { onDestroy } from 'svelte';
	import { GitBranch, GitCommitHorizontal, GitPullRequest, ChevronDown, Check, Plus, Loader2, Lock, CheckCircle2, ArrowDownToLine, ArrowUpFromLine, FolderGit2, FolderClosed, Rocket } from 'lucide-svelte';
	import { git, diffStats, gitStatus, branches as branchStore, gitMode, changedFiles } from '$lib/stores/git.svelte';
	import { checkoutBranchWithToast } from '$lib/stores/gitCheckoutAction';
	import { githubRepoInfo } from '$lib/stores/github.svelte';
	import { openHeaderDropdown, openDropdown, closeDropdown } from '$lib/stores/header-dropdowns';
	import { computeShowShipIt } from '$lib/stores/derived/ship-it-visibility';
	import GitActionOptionsDropdown from './GitActionOptionsDropdown.svelte';
	import type {
		CommitOptions,
		PushOptions,
		PullOptions,
		CreatePROptions,
		FinishOptions,
		ShipItOptions
	} from '$lib/stores/derived/git-action-options';

	interface Props {
		projectId: string;
		isStreaming?: boolean;
		/** True while a git operation (commit/push/PR/finish/ship) is in flight.
		 *  Disables the mutating action buttons + branch switch so the user can't
		 *  fire a second action while work is running. */
		operationInProgress?: boolean;
		isGitRepo?: boolean;
		/** Show the branch checkout selector. Disabled when the branch pill is
		 *  hosted elsewhere (e.g. the context pill bar above the chat input). */
		showBranchSelector?: boolean;
		/** Project name to display alongside the branch (combined badge) */
		projectName?: string | null;
		/** When set, forces worktree read-only mode and uses this as the branch display.
		 *  This ensures the correct branch shows immediately without waiting for the store to load. */
		worktreeBranch?: string | null;
		/** When set, shows the "Finish" button instead of Commit/Create PR */
		openPR?: { number: number; title: string; url: string } | null;
		/** Mutating actions. A bare main-button click passes no options (falling
		 *  back to the gitActionPreferences defaults); the option popover passes an
		 *  explicit bag. Pass the MainPanel handlers directly so both paths flow
		 *  through one function. */
		onClickCommit: (opts?: CommitOptions) => void;
		onClickCreatePR: (opts?: CreatePROptions) => void;
		onClickFinish: (opts?: FinishOptions) => void;
		onClickPush: (opts?: PushOptions) => void;
		onClickPull: (opts?: PullOptions) => void;
		onClickNewBranch: () => void;
		onClickInitRepo?: () => void;
		/** Ship It: one-shot commit → push → PR → merge → cleanup pipeline */
		onClickShipIt?: (opts?: ShipItOptions) => void;
		/** Open diff review panel (side-by-side) */
		onOpenDiffReview?: () => void;
	}

	let { projectId, isStreaming = false, operationInProgress = false, isGitRepo = true, showBranchSelector = true, projectName = null, worktreeBranch = null, openPR = null, onClickCommit, onClickCreatePR, onClickFinish, onClickNewBranch, onClickPush, onClickPull, onClickInitRepo, onClickShipIt, onOpenDiffReview }: Props = $props();

	// Mutating git buttons are blocked while streaming OR while another git
	// operation is in flight — the user must never fire a second action while
	// work is running.
	const actionsBusy = $derived(isStreaming || operationInProgress);

	// Worktree mode: determined by prop (immediate) OR store (after load)
	const isWorktreeMode = $derived(!!worktreeBranch || $gitMode === 'worktree');

	// --- Dropdown state ---
	// Driven by the shared store so opening this closes the profile/status dropdowns.
	const showDropdown = $derived($openHeaderDropdown === 'branch');
	let dropdownRef = $state<HTMLDivElement | null>(null);
	let checkingOut = $state(false);

	// Which git-action option popover is open (single-open, shared store — opening
	// any one closes the branch selector and every sibling git-action popover).
	const commitOptsOpen = $derived($openHeaderDropdown === 'git-commit');
	const pushOptsOpen = $derived($openHeaderDropdown === 'git-push');
	const pullOptsOpen = $derived($openHeaderDropdown === 'git-pull');
	const prOptsOpen = $derived($openHeaderDropdown === 'git-pr');
	const finishOptsOpen = $derived($openHeaderDropdown === 'git-finish');
	const shipItOptsOpen = $derived($openHeaderDropdown === 'git-shipit');

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
	// Whether the branch carries real content over its base branch (non-empty
	// base...HEAD diff). Distinct from commitsAhead: a branch can be ahead by
	// commit count yet have a 0/0 net diff (empty/cancelling commits, or an
	// already-merged/cleaned-up worktree) — those have nothing left to ship.
	const branchDiffersFromBase = $derived($gitStatus?.differs_from_base ?? false);
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

	// Whether this folder is a *confirmed* git repo. Strict `=== true` (not the
	// optimistic `!== false` used for the header gate): for a reused/cleaned-up
	// worktree the folder may be empty, so Ship It must only show once status has
	// loaded and positively confirms a repo — never on null/unconfirmed or stale
	// state. (diffStats, which drives hasChanges, can outlive the repo in cache.)
	const isRepoConfirmed = $derived($gitStatus?.is_git_repo === true);

	// Ship It shows only when there's actually something to ship — uncommitted
	// changes OR real content over the base branch — on a non-default branch
	// that isn't behind upstream, and only for a confirmed repo. See
	// computeShowShipIt for the full rationale.
	const showShipIt = $derived(computeShowShipIt({
		isRepoConfirmed,
		hasChanges,
		branchDiffersFromBase,
		isOnDefaultBranch,
		needsPull,
		hasShipItHandler: !!onClickShipIt
	}));

	// Local branches for the dropdown (exclude remotes)
	const localBranches = $derived(
		$branchStore.filter(b => !b.remote)
	);

	// Local branch names offered as PR base targets in the Create PR popover.
	const baseBranchNames = $derived(localBranches.map(b => b.name));

	// Remote branches that don't have a local counterpart. `$derived.by` memoizes
	// the computed array (recomputed only when its deps change) rather than a
	// `$derived(() => ...)` whose value is a closure re-run on every call site.
	const remoteBranches = $derived.by(() => {
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
			? remoteBranches.filter(b => b.shortName.toLowerCase().includes(branchSearch.toLowerCase()) || b.name.toLowerCase().includes(branchSearch.toLowerCase()))
			: remoteBranches
	);
	const showSearch = $derived(localBranches.length + remoteBranches.length > 5);

	// Can the user interact with the branch selector?
	// In worktree mode, branch is always locked (read-only display). Also locked
	// while an action is in flight so a checkout can't race a running operation.
	const branchLocked = $derived(isWorktreeMode || isStreaming || checkingOut || operationInProgress);

	function toggleDropdown() {
		if (branchLocked) return;
		if (showDropdown) {
			closeDropdown('branch');
		} else {
			openDropdown('branch');
			branchSearch = '';
			// Refresh branches when opening
			git.loadBranches();
			// Focus search input after render
			requestAnimationFrame(() => searchInputRef?.focus());
		}
	}

	async function checkoutBranch(branchName: string) {
		if (branchName === currentBranch) {
			closeDropdown('branch');
			return;
		}
		checkingOut = true;
		closeDropdown('branch');
		try {
			// Surface failures (incl. uncommitted-changes 409 → "Stash & switch").
			await checkoutBranchWithToast(branchName, () => git.refreshDiffStats());
		} finally {
			checkingOut = false;
		}
	}

	function handleNewBranch() {
		closeDropdown('branch');
		onClickNewBranch();
	}

	// Chevron handler for a git-action split button: toggle its option popover.
	// The shared store guarantees single-open, so this also closes any other
	// popover (or the branch selector). Never fires when actions are busy.
	type GitActionDropdownId = 'git-commit' | 'git-push' | 'git-pull' | 'git-pr' | 'git-finish' | 'git-shipit';
	function toggleActionDropdown(id: GitActionDropdownId) {
		if (actionsBusy) return;
		if ($openHeaderDropdown === id) closeDropdown(id);
		else openDropdown(id);
	}
	const closeActionDropdown = (id: GitActionDropdownId) => closeDropdown(id);

	// Close the branch dropdown on outside click
	function handleClickOutside(e: MouseEvent) {
		if (showDropdown && dropdownRef && !dropdownRef.contains(e.target as Node)) {
			closeDropdown('branch');
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			if (showDropdown) closeDropdown('branch');
		}
	}

	// Clear our shared-store flag on unmount so no dropdown re-opens on remount.
	onDestroy(() => {
		openHeaderDropdown.update((cur) => (cur === 'branch' || (cur ?? '').startsWith('git-') ? null : cur));
	});
</script>

<svelte:window onclick={handleClickOutside} onkeydown={handleKeydown} />

<div class="git-actions">
	{#if isGitRepo}
	<!-- Branch selector (hidden when hosted in the context pill bar) -->
	{#if showBranchSelector}
	<div class="branch-selector" bind:this={dropdownRef}>
		<button
			class="git-btn branch-btn"
			class:locked={branchLocked}
			onclick={toggleDropdown}
			disabled={branchLocked}
			title={isWorktreeMode ? `${projectName ? projectName + ' · ' : ''}Worktree branch: ${currentBranch}` : isStreaming ? 'Branch locked during streaming' : (currentBranch ? `${projectName ? projectName + ' · ' : ''}Branch: ${currentBranch}` : 'Select branch')}
		>
			<GitBranch size={13} strokeWidth={1.5} />
			{#if projectName}
				<span class="btn-label project-name">{projectName}</span>
				{#if currentBranch}
					<span class="branch-separator">/</span>
				{/if}
			{/if}
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

	{/if}

	<!-- Pull split button (when behind upstream, including diverged state) -->
	{#if needsPull}
		<div class="split-btn-group">
			<button
				class="git-btn split-main pull-btn"
				class:diverged={isDiverged}
				onclick={() => onClickPull()}
				disabled={actionsBusy}
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
				class:active={pullOptsOpen}
				onclick={() => toggleActionDropdown('git-pull')}
				disabled={actionsBusy}
				aria-label="Pull options"
				aria-haspopup="dialog"
				aria-expanded={pullOptsOpen}
				title="Pull options"
			>
				<ChevronDown size={11} strokeWidth={1.5} />
			</button>
			{#if pullOptsOpen}
				<GitActionOptionsDropdown
					action="pull"
					{currentBranch}
					{defaultBranch}
					baseBranches={baseBranchNames}
					{openPR}
					{hasChanges}
					{isWorktreeMode}
					busy={actionsBusy}
					onClose={() => closeActionDropdown('git-pull')}
					onPull={onClickPull}
				/>
			{/if}
		</div>
	{/if}

	<!-- Push split button (when commits ahead of upstream, not behind) -->
	{#if canPush}
		<div class="split-btn-group">
			<button
				class="git-btn split-main push-btn"
				onclick={() => onClickPush()}
				disabled={actionsBusy}
				title={isStreaming
					? 'Cannot push during streaming'
					: `${commitsAheadUpstream} commit${commitsAheadUpstream === 1 ? '' : 's'} to push`}
			>
				<ArrowUpFromLine size={14} strokeWidth={1.5} />
				<span class="btn-label">Push</span>
				<span class="count-badge">{commitsAheadUpstream}</span>
			</button>
			<button
				class="git-btn split-chevron push-btn"
				class:active={pushOptsOpen}
				onclick={() => toggleActionDropdown('git-push')}
				disabled={actionsBusy}
				aria-label="Push options"
				aria-haspopup="dialog"
				aria-expanded={pushOptsOpen}
				title="Push options"
			>
				<ChevronDown size={11} strokeWidth={1.5} />
			</button>
			{#if pushOptsOpen}
				<GitActionOptionsDropdown
					action="push"
					{currentBranch}
					{defaultBranch}
					baseBranches={baseBranchNames}
					{openPR}
					{hasChanges}
					{isWorktreeMode}
					busy={actionsBusy}
					onClose={() => closeActionDropdown('git-push')}
					onPush={onClickPush}
				/>
			{/if}
		</div>
	{/if}

	<!-- Commit / Create PR / Finish split button -->
	{#if hasChanges}
		<div class="split-btn-group">
			<button
				class="git-btn split-main"
				onclick={() => onClickCommit()}
				disabled={actionsBusy}
				title={isStreaming ? 'Cannot commit during streaming' : 'Quick commit with saved settings'}
			>
				<GitCommitHorizontal size={14} strokeWidth={1.5} />
				<span class="btn-label">Commit</span>
			</button>
			<button
				class="git-btn split-chevron"
				class:active={commitOptsOpen}
				onclick={() => toggleActionDropdown('git-commit')}
				disabled={actionsBusy}
				aria-label="Commit options"
				aria-haspopup="dialog"
				aria-expanded={commitOptsOpen}
				title="Commit options"
			>
				<ChevronDown size={11} strokeWidth={1.5} />
			</button>
			{#if commitOptsOpen}
				<GitActionOptionsDropdown
					action="commit"
					{currentBranch}
					{defaultBranch}
					baseBranches={baseBranchNames}
					{openPR}
					{hasChanges}
					{isWorktreeMode}
					busy={actionsBusy}
					onClose={() => closeActionDropdown('git-commit')}
					onCommit={onClickCommit}
				/>
			{/if}
		</div>
	{:else if openPR}
		<div class="split-btn-group">
			<button
				class="git-btn split-main"
				onclick={() => onClickFinish()}
				disabled={actionsBusy}
				title={isStreaming ? 'Cannot finish during streaming' : `Quick merge PR #${openPR.number} with saved settings`}
			>
				<CheckCircle2 size={14} strokeWidth={1.5} />
				<span class="btn-label">Finish</span>
			</button>
			<button
				class="git-btn split-chevron"
				class:active={finishOptsOpen}
				onclick={() => toggleActionDropdown('git-finish')}
				disabled={actionsBusy}
				aria-label="Merge options"
				aria-haspopup="dialog"
				aria-expanded={finishOptsOpen}
				title="Merge options"
			>
				<ChevronDown size={11} strokeWidth={1.5} />
			</button>
			{#if finishOptsOpen}
				<GitActionOptionsDropdown
					action="finish"
					{currentBranch}
					{defaultBranch}
					baseBranches={baseBranchNames}
					{openPR}
					{hasChanges}
					{isWorktreeMode}
					busy={actionsBusy}
					onClose={() => closeActionDropdown('git-finish')}
					onFinish={onClickFinish}
				/>
			{/if}
		</div>
	{:else if canCreatePR}
		<div class="split-btn-group">
			<button
				class="git-btn split-main"
				onclick={() => onClickCreatePR()}
				disabled={actionsBusy}
				title={isStreaming ? 'Cannot create PR during streaming' : `Quick create PR (${commitsAhead} commit${commitsAhead === 1 ? '' : 's'} ahead)`}
			>
				<GitPullRequest size={14} strokeWidth={1.5} />
				<span class="btn-label">Create PR</span>
			</button>
			<button
				class="git-btn split-chevron"
				class:active={prOptsOpen}
				onclick={() => toggleActionDropdown('git-pr')}
				disabled={actionsBusy}
				aria-label="Pull request options"
				aria-haspopup="dialog"
				aria-expanded={prOptsOpen}
				title="Pull request options"
			>
				<ChevronDown size={11} strokeWidth={1.5} />
			</button>
			{#if prOptsOpen}
				<GitActionOptionsDropdown
					action="pr"
					{currentBranch}
					{defaultBranch}
					baseBranches={baseBranchNames}
					{openPR}
					{hasChanges}
					{isWorktreeMode}
					busy={actionsBusy}
					onClose={() => closeActionDropdown('git-pr')}
					onCreatePR={onClickCreatePR}
				/>
			{/if}
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

	<!-- Ship It split button: one-click pipeline (commit → push → PR → merge → cleanup) -->
	<!-- Shows only when there's something to ship: uncommitted changes OR real
	     content over the base branch. Hidden for branches that are "ahead" by
	     commit count but carry a 0/0 net diff (already-merged / cleaned-up). -->
	{#if showShipIt}
		<div class="split-btn-group">
			<button
				class="git-btn split-main"
				onclick={() => onClickShipIt?.()}
				disabled={actionsBusy}
				title={isStreaming ? 'Cannot ship during streaming' : 'Ship It — commit, push, create PR, merge, and clean up in one shot'}
			>
				<Rocket size={13} strokeWidth={1.5} />
				<span class="btn-label">Ship It</span>
			</button>
			<button
				class="git-btn split-chevron"
				class:active={shipItOptsOpen}
				onclick={() => toggleActionDropdown('git-shipit')}
				disabled={actionsBusy}
				aria-label="Ship It options"
				aria-haspopup="dialog"
				aria-expanded={shipItOptsOpen}
				title="Ship It options"
			>
				<ChevronDown size={11} strokeWidth={1.5} />
			</button>
			{#if shipItOptsOpen}
				<GitActionOptionsDropdown
					action="shipit"
					{currentBranch}
					{defaultBranch}
					baseBranches={baseBranchNames}
					{openPR}
					{hasChanges}
					{isWorktreeMode}
					busy={actionsBusy}
					onClose={() => closeActionDropdown('git-shipit')}
					onShipIt={onClickShipIt}
				/>
			{/if}
		</div>
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
		<!-- No git repo: project name + Init button (profile selector lives in the header) -->
		{#if projectName}
			<span class="git-btn project-only-badge" title="Project: {projectName}">
				<FolderClosed size={13} strokeWidth={1.5} />
				<span class="btn-label project-name">{projectName}</span>
			</span>
		{/if}
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

	/* Split button: main action + chevron, sharing a seam. The group is the
	   positioning anchor for its GitActionOptionsDropdown popover. */
	.split-btn-group {
		position: relative;
		display: inline-flex;
		align-items: stretch;
	}

	.split-btn-group .split-main {
		border-top-right-radius: 0;
		border-bottom-right-radius: 0;
	}

	.split-btn-group .split-chevron {
		border-top-left-radius: 0;
		border-bottom-left-radius: 0;
		border-left: none;
		padding: 0 5px;
		gap: 0;
	}

	.split-chevron.active {
		background: var(--hover-overlay);
		color: var(--foreground);
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

	.project-name {
		max-width: 120px;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.branch-separator {
		color: var(--muted-foreground);
		opacity: 0.4;
		font-size: 0.75rem;
		line-height: 1;
		flex-shrink: 0;
		margin: 0 -1px;
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

	/* Mobile touch targets — single horizontal rail (no wrapping).
	   1080px keeps this in sync with AppLayout's isMobile width cutoff so it also
	   covers unfolded foldables (Z Fold 6/7 inner ~1028–1040px). */
	@media (max-width: 1080px) {
		.git-actions {
			/* Stay on one line so the header reads as a single touch-scrollable rail.
			   Items keep their intrinsic size and overflow-scroll with the header. */
			flex-wrap: nowrap;
			flex-shrink: 0;
		}

		/* Don't let rail items shrink — they should keep their size and scroll. */
		.git-btn,
		.split-btn-group,
		.diff-stats {
			flex-shrink: 0;
		}

		.git-btn {
			height: 36px;
			padding: 0 12px;
			font-size: 0.75rem;
		}

		/* Keep the chevron compact even at the larger mobile touch size. */
		.split-btn-group .split-chevron {
			padding: 0 8px;
		}

		.branch-name {
			max-width: 80px;
		}

		.project-name {
			max-width: 80px;
		}

		/* The header is a horizontal scroll container on mobile, which would clip an
		   absolutely-positioned dropdown. Pin it as a fixed panel just below the
		   header so it escapes the rail's overflow while staying in the DOM tree
		   (so outside-click detection keeps working). */
		.branch-dropdown {
			position: fixed;
			top: 100px;
			left: 8px;
			right: 8px;
			bottom: auto;
			min-width: 0;
			max-width: none;
			max-height: 60vh;
			z-index: 200;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.branch-dropdown {
			animation: none;
		}
	}
</style>

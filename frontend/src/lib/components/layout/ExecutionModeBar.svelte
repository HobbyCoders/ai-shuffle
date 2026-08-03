<script lang="ts">
	/**
	 * ExecutionModeBar - Local/Worktree selector with branch management
	 *
	 * Positioned below the chat input island. Shows a single dropdown
	 * pill for execution mode (the current mode's icon + label + chevron;
	 * clicking opens an upward menu with Local as the base mode and an
	 * orthogonal Worktree checkbox) with an inline branch selector when
	 * worktree mode is active. Includes "New Branch" creation modal. In Auto
	 * mode a second pill lets the user pick the base branch the auto-created
	 * worktree branch forks from.
	 *
	 * Uses the git store for worktree/branch data and syncWorktrees()
	 * to clean up stale DB records on project change. Worktree creation is
	 * deferred to send time (POST /sessions/with-worktree via message-sender);
	 * picking a branch here only stages it.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import type { ExecutionMode } from '$lib/stores/types';
	import { syncWorktrees, type Worktree, type Branch } from '$lib/api/git';
	import { worktrees as worktreeStore, branches as branchStore, git, currentBranch as gitCurrentBranch, gitStatus } from '$lib/stores/git.svelte';
	import { ChevronDown, GitBranch, Monitor, GitFork, Plus, Loader2, Check } from 'lucide-svelte';
	import { untrack } from 'svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import Banner from '$lib/components/Banner.svelte';

	// --- UI state ---
	let showBranchDropdown = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);
	// Separate dropdown for the execution-mode picker (coexists with the branch
	// dropdown above; its own container ref so outside-click closes it independently).
	let showModeDropdown = $state(false);
	let modeDropdownRef = $state<HTMLDivElement | null>(null);
	// Separate dropdown for the base-branch picker (only shown in Auto mode).
	let showBaseDropdown = $state(false);
	let baseDropdownRef = $state<HTMLDivElement | null>(null);
	let loading = $state(false);
	let lastSyncedProject = $state<string | null>(null);

	// --- New branch modal state ---
	let showCreateModal = $state(false);

	// --- Git-repo gating ---
	// Worktree mode and the branch selector only make sense for a git
	// repository. Reuse the existing git-status signal (loaded by MainPanel's git
	// store effect for the active project) rather than inventing a new one — the
	// same `is_git_repo !== false` convention GitHeaderActions uses, so non-git
	// controls only hide once the backend confirms the folder isn't a repo (during
	// the brief load window status is null and we optimistically show everything).
	const isGitRepo = $derived($gitStatus?.is_git_repo !== false);

	$effect(() => {
		// A non-git project can only run Local. If a stale persisted preference (or
		// a switch from a git project) left this session in a hidden Worktree mode,
		// fall back to Local so the toggle always has a visible active option.
		if (!isGitRepo && untrack(() => $activeSession.worktreeIntent) !== 'local') {
			setMode('local');
		}
	});

	// --- Derived data ---
	// Filter out worktrees that no longer exist on disk
	const filteredWorktrees = $derived($worktreeStore.filter(w => w.exists !== false));

	// Branches that don't already have a worktree
	const worktreeBranchNames = $derived(new Set(filteredWorktrees.map(w => w.branch_name)));

	// The git repo's currently checked-out branch name (can't create a worktree on it)
	const checkedOutBranchName = $derived($gitCurrentBranch?.name || null);

	const filteredBranches = $derived.by(() => {
		const seen = new Set<string>();
		return $branchStore.filter(b => {
			// Skip main/master (main worktree)
			if (b.name === 'main' || b.name === 'master') return false;
			// Skip the currently checked-out branch — git won't allow a worktree on it
			if (!b.remote && checkedOutBranchName && b.name === checkedOutBranchName) return false;
			// Skip remote branches that have a local counterpart
			if (b.remote) {
				const localName = b.name.replace(/^[^\/]+\//, '');
				if ($branchStore.some(lb => !lb.remote && lb.name === localName)) return false;
			}
			// Skip branches that already have a worktree
			const effectiveName = b.remote ? b.name.replace(/^[^\/]+\//, '') : b.name;
			if (worktreeBranchNames.has(effectiveName)) return false;
			// Deduplicate by effective display name to prevent duplicates from
			// multiple remotes or any upstream data issues
			if (seen.has(effectiveName)) return false;
			seen.add(effectiveName);
			return true;
		});
	});

	// Current branch display name — shows "Auto" when no branch is selected
	// to indicate that a branch will be auto-generated from the user's message
	const currentBranch = $derived($activeSession.selectedWorktreeBranch || $activeSession.worktreeBranch || 'Auto');
	const isAutoMode = $derived(!$activeSession.selectedWorktreeBranch && !$activeSession.worktreeBranch && !$activeSession.worktreeId);

	// Base branch an auto-created worktree branch forks FROM (defaults to 'main').
	const baseBranch = $derived($activeSession.worktreeBaseBranch || 'main');

	// Branches valid as a fork base. Unlike `filteredBranches` this keeps main/master,
	// the checked-out branch, and branches that already have worktrees (any branch is a
	// valid base); it only drops remote branches shadowed by a local counterpart and
	// dedupes by effective display name.
	const baseBranchOptions = $derived.by(() => {
		const seen = new Set<string>();
		return $branchStore.filter(b => {
			// Skip remote branches that have a local counterpart
			if (b.remote) {
				const localName = b.name.replace(/^[^\/]+\//, '');
				if ($branchStore.some(lb => !lb.remote && lb.name === localName)) return false;
			}
			// Deduplicate by effective display name
			const effectiveName = b.remote ? b.name.replace(/^[^\/]+\//, '') : b.name;
			if (seen.has(effectiveName)) return false;
			seen.add(effectiveName);
			return true;
		});
	});

	// Worktree error surfaced from the store (send-time creation via message-sender).
	const worktreeError = $derived($activeSession.worktreeError);

	// --- Effects ---

	// Sync worktrees when project changes (cleans up stale DB records)
	$effect(() => {
		const project = $activeSession.project;
		if (project && project !== untrack(() => lastSyncedProject)) {
			lastSyncedProject = project;
			syncWorktrees(project).catch(e => {
				console.warn('[ExecutionModeBar] Failed to sync:', e);
			});
		}
	});

	// Worktree/branch data is loaded by MainPanel's git store effect.
	// We only refresh on-demand when the dropdown is opened (see toggleDropdown).

	// --- Functions ---

	function setMode(mode: ExecutionMode) {
		activeSession.setWorktreeIntent(mode);
	}

	// --- Execution-mode dropdown ---
	//
	// `worktreeIntent` is the canonical enum ('local' | 'worktree') the rest of the
	// app dispatches on. The dropdown presents it as a base mode (Local) plus an
	// orthogonal Worktree checkbox:
	//   Local, no worktree → 'local'
	//   Local + worktree   → 'worktree'
	const worktreeOn = $derived($activeSession.worktreeIntent === 'worktree');

	function toggleModeDropdown() {
		showModeDropdown = !showModeDropdown;
	}

	function selectBaseMode() {
		// Local is the only base mode; clicking it just closes the menu (the
		// worktree checkbox is the orthogonal modifier).
		showModeDropdown = false;
	}

	function toggleWorktree() {
		activeSession.setWorktreeIntent(worktreeOn ? 'local' : 'worktree');
		// Leave the menu open so the user sees the checkbox flip and can keep going.
	}

	function selectWorktree(wt: Worktree) {
		activeSession.selectWorktreeBranch(wt.branch_name, wt.id);
		showBranchDropdown = false;
	}

	function selectBranch(branch: Branch) {
		showBranchDropdown = false;
		const branchName = branch.remote ? branch.name.replace(/^[^\/]+\//, '') : branch.name;

		// Stage the branch — the worktree is created at send time
		// (POST /sessions/with-worktree in message-sender).
		activeSession.setWorktreeBranch(branchName);
		activeSession.setWorktreeMode('new');
		activeSession.setWorktreeCreateNewBranch(false);
	}

	function toggleDropdown() {
		showBranchDropdown = !showBranchDropdown;
		if (showBranchDropdown && $activeSession.project) {
			refreshWorktrees();
		}
	}

	function toggleBaseDropdown() {
		showBaseDropdown = !showBaseDropdown;
		if (showBaseDropdown && $activeSession.project) {
			refreshWorktrees();
		}
	}

	function selectBaseBranch(branch: Branch) {
		const name = branch.remote ? branch.name.replace(/^[^\/]+\//, '') : branch.name;
		activeSession.setWorktreeBaseBranch(name);
		showBaseDropdown = false;
	}

	async function refreshWorktrees() {
		loading = true;
		try {
			await Promise.all([git.loadWorktrees(), git.loadBranches()]);
		} catch (e) {
			console.warn('[ExecutionModeBar] Failed to refresh:', e);
		} finally {
			loading = false;
		}
	}

	// Close either dropdown on outside click (each guarded to only act when open)
	function handleClickOutside(e: MouseEvent) {
		if (showBranchDropdown && dropdownRef && !dropdownRef.contains(e.target as Node)) {
			showBranchDropdown = false;
		}
		if (showModeDropdown && modeDropdownRef && !modeDropdownRef.contains(e.target as Node)) {
			showModeDropdown = false;
		}
		if (showBaseDropdown && baseDropdownRef && !baseDropdownRef.contains(e.target as Node)) {
			showBaseDropdown = false;
		}
	}

	// --- New Branch modal ---

	function openCreateModal() {
		showBranchDropdown = false;
		showCreateModal = true;
	}

	function handleBranchCreated(branchName: string) {
		// Branch was already created by CreateBranchModal; stage it as the worktree
		// branch (create_new_branch=false). The worktree itself is created at send
		// time (POST /sessions/with-worktree in message-sender).
		activeSession.setWorktreeBranch(branchName);
		activeSession.setWorktreeMode('new');
		activeSession.setWorktreeCreateNewBranch(false);
	}
</script>

<svelte:window onclick={handleClickOutside} />

<div class="execution-mode-bar">
	<div class="mode-bar-row">
		<!-- Mode dropdown pill: shows the base mode (Local) plus a worktree marker,
		     and on click opens an upward menu with Local as a radio and a Worktree
		     checkbox at the bottom. Worktree is gated to any git repo; for a non-git
		     project only Local is offered and the mode is forced to Local above. -->
		<div class="mode-selector" bind:this={modeDropdownRef}>
			<button
				type="button"
				class="mode-trigger"
				class:open={showModeDropdown}
				onclick={toggleModeDropdown}
				title="Run this session on this device"
				aria-haspopup="menu"
				aria-expanded={showModeDropdown}
			>
				<Monitor size={13} strokeWidth={1.5} />
				<span class="mode-trigger-label">Local</span>
				{#if worktreeOn}
					<GitFork size={12} strokeWidth={1.5} class="wt-marker" />
				{/if}
				<ChevronDown size={12} strokeWidth={1.5} />
			</button>

			{#if showModeDropdown}
				<div class="mode-dropdown branch-dropdown" role="menu">
					<!-- Base mode: Local -->
					<button
						type="button"
						class="branch-item active"
						onclick={() => selectBaseMode()}
						role="menuitemradio"
						aria-checked="true"
					>
						<Monitor size={13} strokeWidth={1.5} />
						<span class="branch-item-name">Local</span>
					</button>

					<!-- Worktree modifier: orthogonal checkbox at the bottom. -->
					{#if isGitRepo}
						<div class="branch-divider"></div>
						<button
							type="button"
							class="branch-item wt-check-item"
							onclick={toggleWorktree}
							title="Run in an isolated git worktree."
							role="menuitemcheckbox"
							aria-checked={worktreeOn}
						>
							<span class="wt-checkbox" class:checked={worktreeOn}>
								{#if worktreeOn}<Check size={11} strokeWidth={3} />{/if}
							</span>
							<GitFork size={13} strokeWidth={1.5} />
							<span class="branch-item-name">Worktree</span>
						</button>
					{/if}
				</div>
			{/if}
		</div>

		<!-- Branch selector (shown when worktree mode active) -->
		{#if $activeSession.worktreeIntent === 'worktree'}
			<div class="branch-selector" bind:this={dropdownRef}>
			<button
				type="button"
				class="branch-trigger"
				class:auto-mode={isAutoMode}
				onclick={toggleDropdown}
				title={isAutoMode ? 'Branch will be auto-generated from your message. Click to select a specific branch.' : `Current branch: ${currentBranch}`}
			>
				<GitBranch size={12} strokeWidth={1.5} />
				<span class="branch-name">{currentBranch}</span>
				<ChevronDown size={12} strokeWidth={1.5} />
			</button>

			{#if showBranchDropdown}
				<div class="branch-dropdown">
					{#if loading}
						<div class="branch-loading">
							<Loader2 size={14} class="animate-spin" />
							<span>Loading...</span>
						</div>
					{:else}
						<!-- Auto mode option -->
						<button
							type="button"
							class="branch-item auto-item"
							class:active={isAutoMode}
							onclick={() => { activeSession.setWorktreeBranch(''); activeSession.setWorktreeMode('new'); activeSession.clearWorktreeSelection(); showBranchDropdown = false; }}
						>
							<GitFork size={13} strokeWidth={1.5} />
							<span class="branch-item-name">Auto</span>
							<span class="auto-hint">from message</span>
						</button>
						<div class="branch-divider"></div>

						<!-- Active Worktrees -->
						{#if filteredWorktrees.length > 0}
							<div class="branch-section-label">Worktrees</div>
							{#each filteredWorktrees as wt (wt.id)}
								<button
									type="button"
									class="branch-item"
									class:active={$activeSession.worktreeId === wt.id}
									onclick={() => selectWorktree(wt)}
								>
									<GitBranch size={13} strokeWidth={1.5} />
									<span class="branch-item-name">{wt.branch_name}</span>
									{#if wt.git_status && !wt.git_status.is_clean}
										<span class="dirty-badge">M</span>
									{/if}
								</button>
							{/each}
						{/if}

						<!-- Available Branches -->
						{#if filteredBranches.length > 0}
							{#if filteredWorktrees.length > 0}
								<div class="branch-divider"></div>
							{/if}
							<div class="branch-section-label">Branches</div>
							{#each filteredBranches as branch}
								<button
									type="button"
									class="branch-item"
									onclick={() => selectBranch(branch)}
								>
									<GitBranch size={13} strokeWidth={1.5} />
									<span class="branch-item-name">{branch.remote ? branch.name.replace(/^[^\/]+\//, '') : branch.name}</span>
									<span class="branch-location-badge" class:remote={branch.remote}>
										{branch.remote ? 'remote' : 'local'}
									</span>
								</button>
							{/each}
						{/if}

						<!-- Empty state -->
						{#if filteredWorktrees.length === 0 && filteredBranches.length === 0}
							<p class="branch-empty">No branches available</p>
						{/if}

						<div class="branch-divider"></div>
						<button type="button" class="branch-item create-branch-item" onclick={openCreateModal}>
							<Plus size={13} strokeWidth={1.5} />
							<span class="branch-item-name">New branch...</span>
						</button>
					{/if}
				</div>
			{/if}
		</div>

		<!-- Base-branch picker: only shown in Auto mode, since the base branch only
		     affects the NEW branch auto-created at send time. Once a specific branch
		     or existing worktree is staged, there's nothing to fork from a base. -->
		{#if isAutoMode}
			<div class="branch-selector" bind:this={baseDropdownRef}>
				<button
					type="button"
					class="branch-trigger"
					onclick={toggleBaseDropdown}
					title={`New worktree branch will be created from ${baseBranch}. Click to change the base branch.`}
				>
					<GitBranch size={12} strokeWidth={1.5} />
					<span class="branch-name">from {baseBranch}</span>
					<ChevronDown size={12} strokeWidth={1.5} />
				</button>

				{#if showBaseDropdown}
					<div class="branch-dropdown">
						{#if loading}
							<div class="branch-loading">
								<Loader2 size={14} class="animate-spin" />
								<span>Loading...</span>
							</div>
						{:else}
							<div class="branch-section-label">Base branch</div>
							{#each baseBranchOptions as branch}
								<button
									type="button"
									class="branch-item"
									class:active={(branch.remote ? branch.name.replace(/^[^\/]+\//, '') : branch.name) === baseBranch}
									onclick={() => selectBaseBranch(branch)}
								>
									<GitBranch size={13} strokeWidth={1.5} />
									<span class="branch-item-name">{branch.remote ? branch.name.replace(/^[^\/]+\//, '') : branch.name}</span>
									<span class="branch-location-badge" class:remote={branch.remote}>
										{branch.remote ? 'remote' : 'local'}
									</span>
								</button>
							{/each}
							{#if baseBranchOptions.length === 0}
								<p class="branch-empty">No branches available</p>
							{/if}
						{/if}
					</div>
				{/if}
			</div>
		{/if}
		{/if}
	</div>

	<!-- Error shown below the pills row so it doesn't overflow on mobile -->
	{#if $activeSession.worktreeIntent === 'worktree' && worktreeError}
		<div class="wt-error-row">
			<Banner severity="error" message={worktreeError} dismissible onDismiss={() => { activeSession.clearWorktreeError(); }} />
		</div>
	{/if}
</div>

<CreateBranchModal
	open={showCreateModal}
	projectId={$activeSession.project}
	onClose={() => showCreateModal = false}
	onCreated={handleBranchCreated}
/>

<style>
	/* Hosted inline in the ContextPillBar (above the chat input) on the new-session
	   screen, so it flows as a pill group next to the profile selector rather than
	   as a standalone centered bar. align-self keeps the toggle row vertically
	   centered against the profile pill; the optional error wraps underneath. */
	.execution-mode-bar {
		position: relative;
		z-index: 10;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		align-self: center;
		gap: 6px;
		min-width: 0;
		max-width: 100%;
		overflow: visible;
	}

	.mode-bar-row {
		display: flex;
		align-items: center;
		gap: 8px;
		max-width: 100%;
	}

	/* Mode dropdown pill — styled to match the .branch-trigger pill */
	.mode-selector {
		position: relative;
		flex-shrink: 0;
		min-width: 0;
	}

	.mode-trigger {
		display: flex;
		align-items: center;
		gap: 5px;
		padding: 4px 10px;
		background: color-mix(in oklch, var(--foreground) 8%, var(--background));
		border: 1px solid var(--border);
		border-radius: var(--radius);
		color: var(--muted-foreground);
		font-size: 0.75rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
		white-space: nowrap;
		max-width: 100%;
	}

	.mode-trigger:hover,
	.mode-trigger.open {
		border-color: var(--primary);
		color: var(--foreground);
	}

	.mode-trigger :global(svg) {
		flex-shrink: 0;
	}

	/* Small worktree marker on the trigger pill when the checkbox is on. */
	.mode-trigger :global(svg.wt-marker) {
		color: var(--primary);
	}

	/* Worktree checkbox row in the mode menu. */
	.wt-checkbox {
		width: 15px;
		height: 15px;
		border: 1.5px solid var(--border);
		border-radius: 4px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		transition: background-color 120ms ease, border-color 120ms ease;
	}

	.wt-checkbox.checked {
		background: var(--primary);
		border-color: var(--primary);
	}

	/* Wins over the generic `.branch-item :global(svg)` muted color so the tick
	   reads as white on the filled primary box. */
	.wt-checkbox :global(svg) {
		color: var(--primary-foreground) !important;
	}

	.mode-trigger-label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Mode dropdown — reuses .branch-dropdown visuals; anchored to the pill's left
	   edge (it's the leftmost pill) and opens upward like the branch dropdown. */
	.mode-dropdown {
		left: 0;
		right: auto;
		min-width: 160px;
	}

	/* Branch selector */
	.branch-selector {
		position: relative;
		min-width: 0;
	}

	.branch-trigger {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 4px 10px;
		background: color-mix(in oklch, var(--foreground) 8%, var(--background));
		border: 1px solid var(--border);
		border-radius: var(--radius);
		color: var(--muted-foreground);
		font-size: 0.75rem;
		cursor: pointer;
		transition: all 150ms ease;
		max-width: 100%;
		min-width: 0;
	}

	.branch-trigger:hover {
		border-color: var(--primary);
		color: var(--foreground);
	}

	.branch-trigger.auto-mode {
		border-style: dashed;
		color: var(--muted-foreground);
		opacity: 0.8;
	}

	.branch-trigger.auto-mode:hover {
		opacity: 1;
	}

	.branch-name {
		max-width: min(180px, 40vw);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Branch dropdown */
	.branch-dropdown {
		position: absolute;
		bottom: calc(100% + 6px);
		right: 0;
		min-width: 240px;
		max-width: calc(100vw - 24px);
		max-height: 300px;
		overflow-y: auto;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		box-shadow: var(--shadow-l);
		padding: 4px;
		z-index: 100;
		animation: branchDropdown 120ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes branchDropdown {
		from {
			opacity: 0;
			transform: translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.branch-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 8px 10px;
		background: transparent;
		border: none;
		border-radius: 7px;
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		text-align: left;
		transition: background-color 120ms ease;
	}

	.branch-item:hover {
		background: var(--hover-overlay);
	}

	.branch-item.active {
		background: var(--selection-bg);
		color: var(--selection-text);
	}

	.branch-item :global(svg) {
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.branch-item.active :global(svg) {
		color: var(--selection-text);
	}

	.branch-item-name {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.dirty-badge {
		font-size: 0.625rem;
		font-weight: 600;
		color: var(--warning);
		padding: 1px 4px;
		background: color-mix(in oklch, var(--warning) 15%, transparent);
		border-radius: 4px;
	}

	.branch-empty {
		padding: 12px;
		text-align: center;
		color: var(--muted-foreground);
		font-size: 0.75rem;
	}

	.branch-section-label {
		padding: 6px 10px 2px;
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
	}

	.branch-location-badge {
		font-size: 0.5625rem;
		font-weight: 500;
		padding: 1px 5px;
		border-radius: 4px;
		background: color-mix(in oklch, var(--muted-foreground) 15%, transparent);
		color: var(--muted-foreground);
	}

	.branch-location-badge.remote {
		background: color-mix(in oklch, var(--info) 15%, transparent);
		color: var(--info);
	}

	/* Error banner - displayed below the pills row */
	.wt-error-row {
		width: 100%;
		display: flex;
		justify-content: center;
		padding: 0 8px;
	}

	/* Loading state */
	.branch-loading {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 12px;
		color: var(--muted-foreground);
		font-size: 0.75rem;
	}

	.branch-loading :global(svg) {
		color: var(--primary);
	}

	/* Divider */
	.branch-divider {
		height: 1px;
		background: var(--border);
		margin: 4px 0;
	}

	/* Auto mode item */
	.auto-item :global(svg) {
		color: var(--primary);
	}

	.auto-hint {
		font-size: 0.5625rem;
		font-weight: 500;
		padding: 1px 5px;
		border-radius: 4px;
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
	}

	/* Create branch item */
	.create-branch-item {
		color: var(--primary) !important;
	}

	.create-branch-item :global(svg) {
		color: var(--primary) !important;
	}

	/* Animate spin for loader */
	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	/* Mobile: tighter branch name limit + viewport-bounded dropdowns */
	@media (max-width: 639px) {
		.branch-name {
			max-width: min(120px, 30vw);
		}

		/* Branch dropdown can hold long branch names, so reanchor it to the
		   (relatively-positioned) .execution-mode-bar and stretch it edge-to-edge
		   within the viewport. The bar is wider in worktree mode (mode + branch
		   pills), which is the only state this dropdown appears in. */
		.branch-selector {
			position: static;
		}

		.branch-dropdown {
			left: 12px;
			right: 12px;
			min-width: 0;
			max-width: none;
		}

		/* Mode menu: keep it anchored to its OWN trigger (.mode-selector stays
		   position: relative). The shared .branch-dropdown rule above anchors to
		   .execution-mode-bar, but that container is shrink-wrapped to the pills —
		   in local mode it's just the narrow "Local" pill, so left/right:12px
		   collapsed this menu to a sliver and truncated the mode labels. Anchor to
		   the trigger's right edge instead (the mode pill always sits right of the
		   profile pill, so the menu grows leftward and stays on screen), sized to
		   its short fixed labels and clamped to the viewport. */
		.mode-dropdown {
			left: auto;
			right: 0;
			width: max-content;
			min-width: 0;
			max-width: calc(100vw - 24px);
		}
	}

	/* Reduced motion */
	@media (prefers-reduced-motion: reduce) {
		.branch-dropdown,
		:global(.animate-spin) {
			animation: none;
		}
	}
</style>

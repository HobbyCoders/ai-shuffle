<script lang="ts">
	/**
	 * ExecutionModeBar - Local/Worktree toggle with branch management
	 *
	 * Positioned below the chat input island. Shows a segmented
	 * pill toggle for execution mode with an inline branch selector
	 * when worktree mode is active. Includes "New Branch" creation modal.
	 *
	 * Uses the git store for worktree/branch data and syncWorktrees()
	 * to clean up stale DB records on project change.
	 */
	import { tabs, type ChatTab, type ExecutionMode } from '$lib/stores/tabs';
	import { syncWorktrees, createWorktree as createWorktreeApi, type Worktree, type Branch } from '$lib/api/git';
	import { worktrees as worktreeStore, branches as branchStore, git } from '$lib/stores/git';
	import { ChevronDown, GitBranch, Monitor, GitFork, Plus, Loader2 } from 'lucide-svelte';
	import { untrack } from 'svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';

	interface Props {
		tab: ChatTab;
	}

	let { tab }: Props = $props();

	// --- UI state ---
	let showBranchDropdown = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);
	let loading = $state(false);
	let lastSyncedProject = $state<string | null>(null);

	// --- New branch modal state ---
	let showCreateModal = $state(false);
	let creatingWorktree = $state(false);

	// --- Derived data ---
	// Filter out worktrees that no longer exist on disk
	const filteredWorktrees = $derived($worktreeStore.filter(w => w.exists !== false));

	// Branches that don't already have a worktree
	const worktreeBranchNames = $derived(new Set(filteredWorktrees.map(w => w.branch_name)));

	const filteredBranches = $derived.by(() => {
		return $branchStore.filter(b => {
			// Skip main/master (main worktree)
			if (b.name === 'main' || b.name === 'master') return false;
			// Skip remote branches that have a local counterpart
			if (b.remote) {
				const localName = b.name.replace(/^[^\/]+\//, '');
				if ($branchStore.some(lb => !lb.remote && lb.name === localName)) return false;
			}
			// Skip branches that already have a worktree
			const effectiveName = b.remote ? b.name.replace(/^[^\/]+\//, '') : b.name;
			if (worktreeBranchNames.has(effectiveName)) return false;
			return true;
		});
	});

	// Current branch display name
	const currentBranch = $derived(tab.selectedWorktreeBranch || tab.worktreeBranch || 'Select branch');

	// --- Effects ---

	// Sync worktrees when project changes (cleans up stale DB records)
	$effect(() => {
		const project = tab.project;
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
		tabs.setTabExecutionMode(tab.id, mode);
	}

	function selectWorktree(wt: Worktree) {
		tabs.selectExistingWorktree(tab.id, wt.id, wt.branch_name);
		showBranchDropdown = false;
	}

	function selectBranch(branch: Branch) {
		showBranchDropdown = false;
		const branchName = branch.remote ? branch.name.replace(/^[^\/]+\//, '') : branch.name;

		// Update tab state
		tabs.clearWorktreeSelection(tab.id);
		tabs.setTabWorktreeBranch(tab.id, branchName);
		tabs.setTabWorktreeMode(tab.id, 'new');
		tabs.setTabWorktreeCreateNewBranch(tab.id, false);

		// Eagerly create the worktree directory
		eagerCreateWorktree(branchName, false);
	}

	async function eagerCreateWorktree(branchName: string, createNew: boolean, base?: string) {
		if (!tab.project) return;
		creatingWorktree = true;
		tabs.clearWorktreeError(tab.id);
		try {
			const response = await createWorktreeApi(tab.project, {
				branch_name: branchName,
				create_new_branch: createNew,
				base_branch: base
			});
			if (response.success && response.worktree) {
				tabs.setTabWorktreeId(tab.id, response.worktree.id);
				tabs.setTabWorktreeMode(tab.id, 'existing');
				// Refresh so new worktree appears in list
				await git.loadWorktrees();
			} else {
				tabs.setWorktreeError(tab.id, response.message || 'Failed to create worktree');
			}
		} catch (err: any) {
			const msg = err?.detail || err?.message || 'Failed to create worktree';
			tabs.setWorktreeError(tab.id, msg);
		} finally {
			creatingWorktree = false;
		}
	}

	function toggleDropdown() {
		showBranchDropdown = !showBranchDropdown;
		if (showBranchDropdown && tab.project) {
			refreshWorktrees();
		}
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

	// Close dropdown on outside click (guarded to only run when open)
	function handleClickOutside(e: MouseEvent) {
		if (!showBranchDropdown) return;
		if (dropdownRef && !dropdownRef.contains(e.target as Node)) {
			showBranchDropdown = false;
		}
	}

	// --- New Branch modal ---

	function openCreateModal() {
		showBranchDropdown = false;
		showCreateModal = true;
	}

	function handleBranchCreated(branchName: string) {
		// Set up the new branch as a worktree
		tabs.clearWorktreeSelection(tab.id);
		tabs.setTabWorktreeBranch(tab.id, branchName);
		tabs.setTabWorktreeMode(tab.id, 'new');
		tabs.setTabWorktreeCreateNewBranch(tab.id, true);

		// Eagerly create the worktree + new branch
		eagerCreateWorktree(branchName, true);
	}
</script>

<svelte:window onclick={handleClickOutside} />

<div class="execution-mode-bar">
	<!-- Segmented pill toggle -->
	<div class="mode-toggle">
		<button
			class="mode-option"
			class:active={tab.executionMode === 'local'}
			onclick={() => setMode('local')}
		>
			<Monitor size={13} strokeWidth={1.5} />
			<span>Local</span>
		</button>
		<button
			class="mode-option"
			class:active={tab.executionMode === 'worktree'}
			onclick={() => setMode('worktree')}
		>
			<GitFork size={13} strokeWidth={1.5} />
			<span>Worktree</span>
		</button>
	</div>

	<!-- Branch selector (shown when worktree mode active) -->
	{#if tab.executionMode === 'worktree'}
		{#if creatingWorktree}
			<div class="wt-status creating">
				<Loader2 size={12} class="animate-spin" />
				<span>Creating...</span>
			</div>
		{:else if tab.worktreeError}
			<div class="wt-status error">
				<span>{tab.worktreeError}</span>
			</div>
		{/if}
		<div class="branch-selector" bind:this={dropdownRef}>
			<button
				class="branch-trigger"
				onclick={toggleDropdown}
				disabled={creatingWorktree}
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
						<!-- Active Worktrees -->
						{#if filteredWorktrees.length > 0}
							<div class="branch-section-label">Worktrees</div>
							{#each filteredWorktrees as wt (wt.id)}
								<button
									class="branch-item"
									class:active={tab.worktreeId === wt.id}
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
						<button class="branch-item create-branch-item" onclick={openCreateModal}>
							<Plus size={13} strokeWidth={1.5} />
							<span class="branch-item-name">New branch...</span>
						</button>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</div>

<CreateBranchModal
	open={showCreateModal}
	projectId={tab.project}
	onClose={() => showCreateModal = false}
	onCreated={handleBranchCreated}
/>

<style>
	.execution-mode-bar {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 12px;
		padding: 8px 0 4px;
	}

	/* Segmented pill */
	.mode-toggle {
		display: inline-flex;
		align-items: center;
		background: color-mix(in oklch, var(--foreground) 8%, var(--background));
		border: 1px solid var(--border);
		border-radius: 20px;
		padding: 2px;
		gap: 2px;
	}

	.mode-option {
		display: flex;
		align-items: center;
		gap: 5px;
		padding: 4px 12px;
		background: transparent;
		border: none;
		border-radius: 18px;
		color: var(--muted-foreground);
		font-size: 0.75rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
		white-space: nowrap;
	}

	.mode-option:hover:not(.active) {
		color: var(--foreground);
	}

	.mode-option.active {
		background: var(--card);
		color: var(--foreground);
		box-shadow: var(--shadow-s);
	}

	.mode-option :global(svg) {
		flex-shrink: 0;
	}

	/* Branch selector */
	.branch-selector {
		position: relative;
	}

	.branch-trigger {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 4px 10px;
		background: color-mix(in oklch, var(--foreground) 8%, var(--background));
		border: 1px solid var(--border);
		border-radius: 16px;
		color: var(--muted-foreground);
		font-size: 0.75rem;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.branch-trigger:hover {
		border-color: var(--primary);
		color: var(--foreground);
	}

	.branch-name {
		max-width: 180px;
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
		border-radius: 10px;
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

	.wt-status {
		display: flex;
		align-items: center;
		gap: 4px;
		font-size: 0.6875rem;
		padding: 2px 8px;
		border-radius: 12px;
	}

	.wt-status.creating {
		color: var(--muted-foreground);
	}

	.wt-status.creating :global(svg) {
		color: var(--primary);
	}

	.wt-status.error {
		color: var(--destructive);
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

	/* Reduced motion */
	@media (prefers-reduced-motion: reduce) {
		.branch-dropdown,
		:global(.animate-spin) {
			animation: none;
		}
	}
</style>

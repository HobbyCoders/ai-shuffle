<script lang="ts">
	/**
	 * ExecutionModeSelector - Simplified Cursor-like branch selector
	 *
	 * Just pick a branch - worktree creation is deferred to first message send.
	 *
	 * Sections:
	 * - Active Worktrees (reuse existing)
	 * - Local Branches (worktree created on send)
	 * - Remote Branches (worktree created on send)
	 * - Create New Branch (modal for new branch name)
	 */
	import { tabs, type ChatTab, type ExecutionMode } from '$lib/stores/tabs';
	import { getBranches, getWorktrees, syncWorktrees, getGitStatus, type Branch, type Worktree } from '$lib/api/git';
	import { onMount } from 'svelte';

	interface Props {
		tab: ChatTab;
	}

	let { tab }: Props = $props();

	// Data state
	let branches = $state<Branch[]>([]);
	let worktrees = $state<Worktree[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let lastSyncedProject = $state<string | null>(null);
	let isGitRepo = $state<boolean | null>(null);
	let lastCheckedGitProject = $state<string | null>(null);
	let dataLoadedAt = $state<number>(0);
	let lastLoadedProject = $state<string | null>(null);

	// UI state
	let showDropdown = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);
	let searchQuery = $state('');
	let searchInputRef = $state<HTMLInputElement | null>(null);

	// Create new branch modal
	let showCreateModal = $state(false);
	let newBranchName = $state('');
	let baseBranch = $state<string>('main');

	// Selected branch/worktree for display
	let selectedBranchName = $state<string | null>(null);
	let selectedWorktreeId = $state<string | null>(null);

	// Data freshness threshold (30 seconds)
	const DATA_STALE_MS = 30000;

	// Check if project is a git repo when project changes
	$effect(() => {
		const project = tab.project;
		if (project && project !== lastCheckedGitProject) {
			checkIfGitRepo(project);
		} else if (!project) {
			isGitRepo = null;
			lastCheckedGitProject = null;
		}
	});

	// Sync worktrees when project changes
	$effect(() => {
		const project = tab.project;
		if (project && project !== lastSyncedProject) {
			syncProjectWorktrees(project);
		}
	});

	// Load data when switching to worktree mode or changing projects
	$effect(() => {
		const project = tab.project;
		const mode = tab.executionMode;
		if (mode === 'worktree' && project && project !== lastLoadedProject && !loading) {
			loadData(project);
		}
	});

	// Sync selection from tab state
	$effect(() => {
		if (tab.worktreeId) {
			selectedWorktreeId = tab.worktreeId;
			const wt = worktrees.find(w => w.id === tab.worktreeId);
			selectedBranchName = wt?.branch_name || null;
		} else if (tab.worktreeBranch) {
			selectedBranchName = tab.worktreeBranch;
			selectedWorktreeId = null;
		}
	});

	// Focus search input when dropdown opens
	$effect(() => {
		if (showDropdown && searchInputRef) {
			setTimeout(() => searchInputRef?.focus(), 50);
		}
	});

	// Filtered data based on search
	const filteredWorktrees = $derived(() => {
		if (!searchQuery.trim()) return worktrees;
		const q = searchQuery.toLowerCase();
		return worktrees.filter(w => {
			// Search by branch name OR folder name
			const folderName = getWorktreeDisplayName(w).toLowerCase();
			return w.branch_name.toLowerCase().includes(q) || folderName.includes(q);
		});
	});

	const localBranches = $derived(() => {
		const worktreeBranchNames = new Set(worktrees.map(w => w.branch_name));
		return branches
			.filter(b => !b.remote && !worktreeBranchNames.has(b.name))
			.filter(b => !searchQuery.trim() || b.name.toLowerCase().includes(searchQuery.toLowerCase()));
	});

	const remoteBranches = $derived(() => {
		const worktreeBranchNames = new Set(worktrees.map(w => w.branch_name));
		const localBranchNames = new Set(branches.filter(b => !b.remote).map(b => b.name));

		return branches
			.filter(b => {
				if (!b.remote) return false;
				// Skip HEAD refs
				if (b.name === 'origin' || b.name === 'origin/HEAD' || b.name.endsWith('/HEAD')) return false;
				// Skip if already has worktree
				if (worktreeBranchNames.has(b.name)) return false;
				// Skip if we have it locally
				const localName = b.name.replace(/^[^/]+\//, '');
				if (localBranchNames.has(localName) || worktreeBranchNames.has(localName)) return false;
				return true;
			})
			.filter(b => !searchQuery.trim() || b.name.toLowerCase().includes(searchQuery.toLowerCase()));
	});

	// Display text for the trigger button
	const displayText = $derived(() => {
		if (selectedBranchName) return selectedBranchName;
		return '';
	});

	// Get dirty status text for a worktree
	function getDirtyStatus(wt: Worktree): string {
		if (!wt.git_status) return '';
		if (wt.git_status.is_clean) return '✓';
		const counts: string[] = [];
		if (wt.git_status.modified_count > 0) counts.push(`${wt.git_status.modified_count}M`);
		if (wt.git_status.staged_count > 0) counts.push(`${wt.git_status.staged_count}S`);
		if (wt.git_status.untracked_count > 0) counts.push(`${wt.git_status.untracked_count}U`);
		return counts.join(' ') || '●';
	}

	// Get display name for worktree (folder name from path)
	function getWorktreeDisplayName(wt: Worktree): string {
		// worktree_path is like "ai-shuffle-develop" or "ai-shuffle-develop-2"
		// Extract just the folder name
		const path = wt.worktree_path || '';
		const folderName = path.split('/').pop() || path;
		return folderName || wt.branch_name;
	}

	async function checkIfGitRepo(projectId: string) {
		try {
			const status = await getGitStatus(projectId);
			isGitRepo = status.is_git_repo;
			lastCheckedGitProject = projectId;
			if (!status.is_git_repo && tab.executionMode === 'worktree') {
				setExecutionMode('local');
			}
		} catch (e) {
			console.warn('[GitCheck] Failed to check git status:', e);
			isGitRepo = false;
			lastCheckedGitProject = projectId;
		}
	}

	async function syncProjectWorktrees(projectId: string) {
		try {
			await syncWorktrees(projectId);
			lastSyncedProject = projectId;
		} catch (e) {
			console.warn('[WorktreeSync] Failed to sync worktrees:', e);
		}
	}

	async function loadData(projectId: string, force: boolean = false) {
		// Skip if data is fresh for the same project (unless forced)
		const isSameProject = projectId === lastLoadedProject;
		if (!force && isSameProject && dataLoadedAt > 0 && Date.now() - dataLoadedAt < DATA_STALE_MS) {
			return;
		}

		loading = true;
		error = null;
		try {
			const [branchResult, worktreeResult] = await Promise.all([
				getBranches(projectId, true),
				getWorktrees(projectId)
			]);
			branches = branchResult;
			worktrees = worktreeResult.filter(w => w.exists !== false);
			dataLoadedAt = Date.now();
			lastLoadedProject = projectId;

			// Set default base branch for create modal
			const mainBranch = branches.find(b => b.name === 'main' || b.name === 'master');
			if (mainBranch) baseBranch = mainBranch.name;
		} catch (e) {
			console.error('Failed to load data:', e);
			error = e instanceof Error ? e.message : 'Failed to load';
			branches = [];
			worktrees = [];
		} finally {
			loading = false;
		}
	}

	function setExecutionMode(mode: ExecutionMode) {
		tabs.setTabExecutionMode(tab.id, mode);
		if (mode === 'local') {
			clearSelection();
		}
	}

	function clearSelection() {
		selectedBranchName = null;
		selectedWorktreeId = null;
		tabs.clearWorktreeSelection(tab.id);
	}

	// Select an existing worktree (reuse)
	function selectWorktree(worktree: Worktree) {
		selectedWorktreeId = worktree.id;
		selectedBranchName = worktree.branch_name;
		showDropdown = false;
		searchQuery = '';

		tabs.selectExistingWorktree(tab.id, worktree.id, worktree.branch_name);
		tabs.setTabWorktreeMode(tab.id, 'existing');
	}

	// Select a branch (worktree will be created on first message send, not here)
	function selectBranch(branch: Branch) {
		showDropdown = false;
		searchQuery = '';

		// Just store the branch name - worktree creation is deferred to message send
		const branchName = branch.remote ? branch.name.replace(/^[^/]+\//, '') : branch.name;
		selectedBranchName = branchName;
		selectedWorktreeId = null;

		// Update tab state - keep as 'new' mode since worktree doesn't exist yet
		tabs.setTabWorktreeBranch(tab.id, branchName);
		tabs.setTabWorktreeMode(tab.id, 'new');
		tabs.setTabWorktreeCreateNewBranch(tab.id, false);  // Using existing branch

		// Clear any previous worktree selection
		tabs.clearWorktreeSelection(tab.id);
		// Set the branch name again after clearing (clearWorktreeSelection resets it)
		tabs.setTabWorktreeBranch(tab.id, branchName);
		tabs.setTabWorktreeCreateNewBranch(tab.id, false);  // Using existing branch
	}

	// Set up for creating a new branch (actual creation deferred to message send)
	function createNewBranch() {
		if (!newBranchName.trim()) return;

		showCreateModal = false;

		// Store branch name and base branch - actual creation happens on first message
		const branchName = newBranchName.trim();
		selectedBranchName = branchName;
		selectedWorktreeId = null;

		// Update tab state with new branch info
		tabs.setTabWorktreeBranch(tab.id, branchName);
		tabs.setTabWorktreeBaseBranch(tab.id, baseBranch);
		tabs.setTabWorktreeMode(tab.id, 'new');
		tabs.setTabWorktreeCreateNewBranch(tab.id, true);  // Creating new branch

		// Clear any previous worktree selection
		tabs.clearWorktreeSelection(tab.id);
		// Restore branch settings after clearing
		tabs.setTabWorktreeBranch(tab.id, branchName);
		tabs.setTabWorktreeBaseBranch(tab.id, baseBranch);
		tabs.setTabWorktreeCreateNewBranch(tab.id, true);  // Creating new branch

		newBranchName = '';
	}

	function handleDropdownClick() {
		if (!showDropdown && tab.project) {
			// Live refresh when opening dropdown
			loadData(tab.project, true);
		}
		showDropdown = !showDropdown;
		searchQuery = '';
	}

	function handleClickOutside(e: MouseEvent) {
		if (dropdownRef && !dropdownRef.contains(e.target as Node)) {
			showDropdown = false;
			searchQuery = '';
		}
	}

	function openCreateModal() {
		showDropdown = false;
		showCreateModal = true;
		newBranchName = '';
	}

	onMount(() => {
		document.addEventListener('click', handleClickOutside);
		return () => document.removeEventListener('click', handleClickOutside);
	});
</script>

<div class="mode-selector">
	<!-- Segmented Control: Local / Worktree -->
	<div class="segment-control">
		<button
			type="button"
			class="segment-btn"
			class:active={tab.executionMode === 'local'}
			class:full-width={!isGitRepo}
			onclick={() => setExecutionMode('local')}
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
			</svg>
			<span>Local</span>
		</button>
		{#if isGitRepo}
			<button
				type="button"
				class="segment-btn"
				class:active={tab.executionMode === 'worktree'}
				onclick={() => setExecutionMode('worktree')}
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<circle cx="6" cy="6" r="2" stroke-width="2"/>
					<circle cx="18" cy="6" r="2" stroke-width="2"/>
					<circle cx="12" cy="18" r="2" stroke-width="2"/>
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 8v4c0 2 2 4 6 4m6-8v4c0 2-2 4-6 4" />
				</svg>
				<span>Worktree</span>
			</button>
		{/if}
	</div>

	<!-- Worktree Branch Selector -->
	{#if tab.executionMode === 'worktree'}
		<div class="worktree-options">
			{#if !tab.project}
				<div class="warning-text">Select a project to use Worktree mode</div>
			{:else if error}
				<div class="error-state">
					<span>{error}</span>
					<button type="button" class="retry-btn" onclick={() => { error = null; if (tab.project) loadData(tab.project, true); }}>Retry</button>
				</div>
			{:else}
				<!-- Branch Selector Dropdown -->
				<div class="dropdown-wrapper" bind:this={dropdownRef}>
					<button
						type="button"
						class="dropdown-trigger"
						onclick={handleDropdownClick}
					>
						{#if loading && (dataLoadedAt === 0 || tab.project !== lastLoadedProject)}
							<svg class="w-4 h-4 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
							</svg>
							<span class="placeholder">Loading branches...</span>
						{:else if displayText()}
							<svg class="w-4 h-4 branch-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<circle cx="6" cy="6" r="2" stroke-width="2"/>
								<circle cx="12" cy="18" r="2" stroke-width="2"/>
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 8v4c0 2 2 4 6 4v2" />
							</svg>
							<span class="selected-value">{displayText()}</span>
						{:else}
							<span class="placeholder">Select a branch...</span>
						{/if}
						<svg class="w-4 h-4 chevron" class:open={showDropdown} fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
						</svg>
					</button>

					<!-- Dropdown Menu -->
					{#if showDropdown}
						<div class="dropdown-menu">
							<!-- Search Input -->
							<div class="search-wrapper">
								<svg class="w-3.5 h-3.5 search-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
								</svg>
								<input
									bind:this={searchInputRef}
									type="text"
									class="search-input"
									placeholder="Search branches..."
									bind:value={searchQuery}
									onclick={(e) => e.stopPropagation()}
								/>
								{#if loading}
									<svg class="w-3.5 h-3.5 animate-spin loading-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
									</svg>
								{/if}
							</div>

							<!-- Active Worktrees -->
							{#if filteredWorktrees().length > 0}
								<div class="dropdown-section">
									<div class="section-header">
										<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
										</svg>
										<span>Active Worktrees</span>
									</div>
									{#each filteredWorktrees() as wt}
									{@const displayName = getWorktreeDisplayName(wt)}
									{@const showBranchBadge = displayName !== wt.branch_name}
										<button
											type="button"
											class="dropdown-item worktree-item"
											class:selected={selectedWorktreeId === wt.id}
											onclick={() => selectWorktree(wt)}
										>
											<div class="worktree-info">
												<span class="item-name">{displayName}</span>
												{#if showBranchBadge}
													<span class="branch-badge" title="Branch: {wt.branch_name}">
														<svg class="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<circle cx="6" cy="6" r="2" stroke-width="2"/>
															<circle cx="12" cy="18" r="2" stroke-width="2"/>
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 8v4c0 2 2 4 6 4v2" />
														</svg>
														{wt.branch_name}
													</span>
												{/if}
											</div>
											<div class="worktree-status">
												{#if wt.git_status}
													<span class="dirty-status" class:clean={wt.git_status.is_clean}>
														{getDirtyStatus(wt)}
													</span>
												{/if}
												{#if selectedWorktreeId === wt.id}
													<svg class="w-4 h-4 check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
													</svg>
												{/if}
											</div>
										</button>
									{/each}
								</div>
							{/if}

							<!-- Local Branches -->
							{#if localBranches().length > 0}
								<div class="dropdown-section">
									<div class="section-header">
										<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<circle cx="6" cy="6" r="2" stroke-width="2"/>
											<circle cx="12" cy="18" r="2" stroke-width="2"/>
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 8v4c0 2 2 4 6 4v2" />
										</svg>
										<span>Local Branches</span>
									</div>
									{#each localBranches() as branch}
										<button
											type="button"
											class="dropdown-item"
											onclick={() => selectBranch(branch)}
										>
											<span class="item-name">{branch.name}</span>
											{#if branch.current}
												<span class="current-badge">current</span>
											{/if}
										</button>
									{/each}
								</div>
							{/if}

							<!-- Remote Branches -->
							{#if remoteBranches().length > 0}
								<div class="dropdown-section">
									<div class="section-header">
										<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3.055 11H5a2 2 0 012 2v1a2 2 0 002 2 2 2 0 012 2v2.945M8 3.935V5.5A2.5 2.5 0 0010.5 8h.5a2 2 0 012 2 2 2 0 004 0 2 2 0 012-2h1.064M15 20.488V18a2 2 0 012-2h3.064M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
										</svg>
										<span>Remote Branches</span>
									</div>
									{#each remoteBranches() as branch}
										<button
											type="button"
											class="dropdown-item"
											onclick={() => selectBranch(branch)}
										>
											<span class="item-name">{branch.name}</span>
											<span class="remote-badge">remote</span>
										</button>
									{/each}
								</div>
							{/if}

							<!-- Create New Branch -->
							<div class="dropdown-section create-section">
								<button
									type="button"
									class="dropdown-item create-item"
									onclick={openCreateModal}
								>
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
									</svg>
									<span>Create new branch...</span>
								</button>
							</div>

							<!-- Empty State -->
							{#if filteredWorktrees().length === 0 && localBranches().length === 0 && remoteBranches().length === 0 && searchQuery.trim()}
								<div class="dropdown-empty">
									<span>No branches matching "{searchQuery}"</span>
								</div>
							{/if}
						</div>
					{/if}
				</div>

				<!-- Selection Summary -->
				{#if selectedBranchName && !selectedWorktreeId}
					<div class="selection-summary ready">
						<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<circle cx="6" cy="6" r="2" stroke-width="2"/>
							<circle cx="12" cy="18" r="2" stroke-width="2"/>
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 8v4c0 2 2 4 6 4v2" />
						</svg>
						<span>Worktree will be created on send</span>
					</div>
				{:else if selectedBranchName && selectedWorktreeId}
					<div class="selection-summary ready">
						<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>
						<span>Using existing worktree</span>
					</div>
				{/if}
			{/if}
		</div>
	{/if}
</div>

<!-- Create New Branch Modal -->
{#if showCreateModal}
	<div class="modal-backdrop" onclick={() => showCreateModal = false}>
		<div class="modal-content" onclick={(e) => e.stopPropagation()}>
			<h3 class="modal-title">Create New Branch</h3>

			<div class="modal-field">
				<label for="new-branch-name">Branch name</label>
				<input
					id="new-branch-name"
					type="text"
					class="modal-input"
					placeholder="feature/my-feature"
					bind:value={newBranchName}
					onkeydown={(e) => { if (e.key === 'Enter' && newBranchName.trim()) createNewBranch(); }}
				/>
			</div>

			<div class="modal-field">
				<label for="base-branch">Base branch</label>
				<select id="base-branch" class="modal-select" bind:value={baseBranch}>
					{#each branches.filter(b => !b.remote) as branch}
						<option value={branch.name}>{branch.name}</option>
					{/each}
				</select>
			</div>

			<div class="modal-actions">
				<button type="button" class="modal-btn secondary" onclick={() => showCreateModal = false}>
					Cancel
				</button>
				<button
					type="button"
					class="modal-btn primary"
					disabled={!newBranchName.trim()}
					onclick={createNewBranch}
				>
					Create & Select
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.mode-selector {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		padding: 0.5rem;
	}

	/* Segmented Control */
	.segment-control {
		display: flex;
		background: color-mix(in srgb, var(--muted) 50%, transparent);
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
		padding: 0.25rem;
		gap: 0.25rem;
	}

	.segment-btn {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 0.375rem;
		padding: 0.5rem 0.75rem;
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.segment-btn:hover {
		color: var(--foreground);
		background: color-mix(in srgb, var(--accent) 50%, transparent);
	}

	.segment-btn.active {
		background: var(--background);
		color: var(--foreground);
		box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
	}

	:global(.dark) .segment-btn.active {
		background: color-mix(in srgb, var(--card) 100%, transparent);
	}

	.segment-btn.full-width {
		flex: none;
		width: 100%;
	}

	/* Worktree Options */
	.worktree-options {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		padding: 0.75rem;
		background: color-mix(in srgb, var(--muted) 30%, transparent);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
	}

	/* Dropdown Wrapper */
	.dropdown-wrapper {
		position: relative;
	}

	/* Dropdown Trigger */
	.dropdown-trigger {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		width: 100%;
		padding: 0.5rem 0.75rem;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		font-size: 0.8125rem;
		color: var(--foreground);
		cursor: pointer;
		transition: border-color 0.15s, box-shadow 0.15s;
	}

	.dropdown-trigger:hover:not(:disabled) {
		border-color: var(--primary);
	}

	.dropdown-trigger:disabled {
		opacity: 0.7;
		cursor: wait;
	}

	.dropdown-trigger .placeholder {
		color: var(--muted-foreground);
		flex: 1;
		text-align: left;
	}

	.dropdown-trigger .selected-value {
		flex: 1;
		text-align: left;
		font-weight: 500;
	}

	.dropdown-trigger .branch-icon {
		color: var(--primary);
		flex-shrink: 0;
	}

	.dropdown-trigger .chevron {
		transition: transform 0.2s;
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.dropdown-trigger .chevron.open {
		transform: rotate(180deg);
	}

	/* Dropdown Menu */
	.dropdown-menu {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		right: 0;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
		z-index: 50;
		max-height: 320px;
		overflow-y: auto;
	}

	/* Search */
	.search-wrapper {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.5rem 0.75rem;
		border-bottom: 1px solid var(--border);
	}

	.search-icon {
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.search-input {
		flex: 1;
		border: none;
		background: transparent;
		font-size: 0.8125rem;
		color: var(--foreground);
		outline: none;
	}

	.search-input::placeholder {
		color: var(--muted-foreground);
	}

	.loading-icon {
		color: var(--muted-foreground);
	}

	.dropdown-section {
		padding: 0.25rem 0;
	}

	.dropdown-section:not(:last-child) {
		border-bottom: 1px solid var(--border);
	}

	.section-header {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.5rem 0.75rem;
		font-size: 0.625rem;
		font-weight: 600;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		width: 100%;
		padding: 0.5rem 0.75rem;
		font-size: 0.8125rem;
		color: var(--foreground);
		transition: background 0.1s;
		text-align: left;
	}

	.dropdown-item:hover {
		background: var(--accent);
	}

	.dropdown-item.selected {
		background: color-mix(in srgb, var(--primary) 10%, transparent);
	}

	.dropdown-item .item-name {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Worktree item layout */
	.dropdown-item.worktree-item {
		flex-wrap: wrap;
	}

	.worktree-info {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		flex: 1;
		min-width: 0;
	}

	.worktree-info .item-name {
		font-weight: 500;
	}

	.worktree-status {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		flex-shrink: 0;
	}

	.branch-badge {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		font-size: 0.5625rem;
		font-weight: 500;
		padding: 0.125rem 0.375rem;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		background: color-mix(in srgb, var(--primary) 12%, transparent);
		color: var(--primary);
		max-width: 100%;
		overflow: hidden;
	}

	.branch-badge svg {
		flex-shrink: 0;
		opacity: 0.7;
	}

	.dirty-status {
		font-size: 0.625rem;
		font-weight: 600;
		padding: 0.125rem 0.375rem;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		background: color-mix(in srgb, #f59e0b 15%, transparent);
		color: #f59e0b;
	}

	.dirty-status.clean {
		background: color-mix(in srgb, #22c55e 15%, transparent);
		color: #22c55e;
	}

	.current-badge, .remote-badge {
		font-size: 0.5625rem;
		font-weight: 500;
		padding: 0.125rem 0.25rem;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		background: color-mix(in srgb, var(--muted) 50%, transparent);
		color: var(--muted-foreground);
	}

	.dropdown-item .check-icon {
		color: var(--primary);
		flex-shrink: 0;
	}

	.create-section {
		border-top: 1px solid var(--border);
	}

	.create-item {
		color: var(--primary);
	}

	.create-item svg {
		color: var(--primary);
	}

	.dropdown-empty {
		padding: 1rem;
		text-align: center;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	/* Selection Summary */
	.selection-summary {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.5rem 0.625rem;
		background: color-mix(in srgb, var(--muted) 40%, transparent);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.selection-summary.ready {
		background: color-mix(in srgb, var(--primary) 10%, transparent);
		color: var(--primary);
	}

	.selection-summary.ready svg {
		color: var(--primary);
	}

	/* Error/Warning States */
	.warning-text {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		text-align: center;
		padding: 0.5rem;
	}

	.error-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.5rem;
		padding: 0.75rem;
		font-size: 0.75rem;
		color: var(--destructive);
		text-align: center;
	}

	.retry-btn {
		padding: 0.25rem 0.75rem;
		font-size: 0.6875rem;
		background: var(--primary);
		color: var(--primary-foreground);
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		transition: opacity 0.15s;
	}

	.retry-btn:hover {
		opacity: 0.9;
	}

	/* Modal */
	.modal-backdrop {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.5);
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: 100;
	}

	.modal-content {
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		padding: 1.5rem;
		width: 100%;
		max-width: 400px;
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
	}

	.modal-title {
		font-size: 1rem;
		font-weight: 600;
		margin-bottom: 1rem;
		color: var(--foreground);
	}

	.modal-field {
		margin-bottom: 1rem;
	}

	.modal-field label {
		display: block;
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--muted-foreground);
		margin-bottom: 0.375rem;
	}

	.modal-input, .modal-select {
		width: 100%;
		padding: 0.5rem 0.75rem;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		font-size: 0.8125rem;
		color: var(--foreground);
	}

	.modal-input:focus, .modal-select:focus {
		outline: none;
		border-color: var(--primary);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary) 20%, transparent);
	}

	.modal-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
		margin-top: 1.5rem;
	}

	.modal-btn {
		padding: 0.5rem 1rem;
		font-size: 0.8125rem;
		font-weight: 500;
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		transition: all 0.15s;
	}

	.modal-btn.secondary {
		background: var(--muted);
		color: var(--muted-foreground);
	}

	.modal-btn.secondary:hover {
		background: var(--accent);
	}

	.modal-btn.primary {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.modal-btn.primary:hover:not(:disabled) {
		opacity: 0.9;
	}

	.modal-btn.primary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Animations */
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	.animate-spin {
		animation: spin 1s linear infinite;
	}
</style>

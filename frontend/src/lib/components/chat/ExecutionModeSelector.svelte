<script lang="ts">
	/**
	 * ExecutionModeSelector - Mode selector for empty chat state
	 *
	 * Allows user to choose between Local mode (work on current branch)
	 * or Worktree mode with:
	 *   - Dropdown: Select existing worktree OR available branch to create new worktree from
	 *   - Text input: Only shown when creating new worktree - enter new branch name
	 *
	 * Only shown in empty state - disappears once chat starts.
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
	let lastLoadedProject = $state<string | null>(null);
	let lastSyncedProject = $state<string | null>(null);
	let isGitRepo = $state<boolean | null>(null);
	let lastCheckedGitProject = $state<string | null>(null);

	// UI state
	let showDropdown = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);
	let newBranchName = $state('');

	// Selection state - what user selected from dropdown
	let selectedType = $state<'worktree' | 'branch' | null>(null);
	let selectedWorktree = $state<Worktree | null>(null);
	let selectedBranch = $state<Branch | null>(null);

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

	// Sync worktrees when project changes (clean up stale database records)
	$effect(() => {
		const project = tab.project;
		if (project && project !== lastSyncedProject) {
			syncProjectWorktrees(project);
		}
	});

	// Load branches and worktrees when switching to worktree mode
	$effect(() => {
		const project = tab.project;
		const mode = tab.executionMode;
		if (mode === 'worktree' && project && project !== lastLoadedProject && !loading) {
			loadData(project);
		}
	});

	// Sync selection state from tab (for resuming state)
	$effect(() => {
		if (tab.worktreeId && worktrees.length > 0) {
			// Existing worktree selected
			const wt = worktrees.find(w => w.id === tab.worktreeId);
			if (wt && selectedWorktree?.id !== wt.id) {
				selectedType = 'worktree';
				selectedWorktree = wt;
				selectedBranch = null;
				newBranchName = '';
			}
		} else if (tab.worktreeBaseBranch && branches.length > 0) {
			// Creating new worktree from a base branch
			const br = branches.find(b => b.name === tab.worktreeBaseBranch);
			if (br && selectedBranch?.name !== br.name) {
				selectedType = 'branch';
				selectedBranch = br;
				selectedWorktree = null;
				newBranchName = tab.worktreeBranch || '';
			}
		}
	});

	// Available branches (not already used by worktrees)
	// Also filters out remote branches if we already have them locally (avoid duplicates)
	const availableBranches = $derived(() => {
		const worktreeBranchNames = new Set(worktrees.map(w => w.branch_name));
		// Get local branch names to filter out duplicate remotes
		const localBranchNames = new Set(
			branches.filter(b => !b.remote).map(b => b.name)
		);

		return branches.filter(b => {
			// Skip if already has a worktree
			if (worktreeBranchNames.has(b.name)) return false;

			// Filter out remote branches
			if (b.remote) {
				// Skip HEAD refs and bare "origin"
				if (b.name === 'origin' ||
				    b.name === 'origin/HEAD' ||
				    b.name.endsWith('/HEAD') ||
				    b.name === 'HEAD') return false;

				// Skip if we already have this branch locally
				// Remote branches are "origin/branch-name", extract the branch name
				const localName = b.name.replace(/^[^/]+\//, ''); // Remove "origin/" or any remote prefix
				if (localBranchNames.has(localName)) return false;
			}

			return true;
		});
	});

	// Display text for the dropdown
	const dropdownDisplayText = $derived(() => {
		if (selectedType === 'worktree' && selectedWorktree) {
			return selectedWorktree.branch_name;
		} else if (selectedType === 'branch' && selectedBranch) {
			return selectedBranch.name;
		}
		return '';
	});

	// Whether the new branch name input should be shown
	const showNewBranchInput = $derived(() => {
		return selectedType === 'branch' && selectedBranch !== null;
	});

	// Validation: is the new branch name valid?
	const isValidNewBranch = $derived(() => {
		if (!showNewBranchInput()) return true;
		const name = newBranchName.trim();
		if (!name) return false;
		// Check it doesn't match any existing branch
		return !branches.some(b => b.name.toLowerCase() === name.toLowerCase());
	});

	// Selection summary text
	const selectionSummary = $derived(() => {
		if (selectedType === 'worktree' && selectedWorktree) {
			return 'Start session in existing worktree';
		} else if (selectedType === 'branch' && selectedBranch) {
			if (newBranchName.trim() && isValidNewBranch()) {
				return `Create worktree with new branch "${newBranchName.trim()}" from ${selectedBranch.name}`;
			}
			return `Select a branch name to create from ${selectedBranch.name}`;
		}
		return '';
	});

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
			const result = await syncWorktrees(projectId);
			lastSyncedProject = projectId;
			// Reload if we cleaned up stale worktrees
			if (result.cleaned_up.length > 0 && lastLoadedProject === projectId) {
				loadData(projectId);
			}
		} catch (e) {
			console.warn('[WorktreeSync] Failed to sync worktrees:', e);
		}
	}

	async function loadData(projectId: string) {
		loading = true;
		error = null;
		try {
			const [branchResult, worktreeResult] = await Promise.all([
				getBranches(projectId, true), // Include remote branches
				getWorktrees(projectId)
			]);
			branches = branchResult;
			worktrees = worktreeResult.filter(w => w.exists !== false);
			lastLoadedProject = projectId;
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
		selectedType = null;
		selectedWorktree = null;
		selectedBranch = null;
		newBranchName = '';
		tabs.setTabWorktreeBranch(tab.id, '');
		tabs.clearWorktreeSelection(tab.id);
	}

	function selectWorktree(worktree: Worktree) {
		selectedType = 'worktree';
		selectedWorktree = worktree;
		selectedBranch = null;
		newBranchName = '';
		showDropdown = false;

		// Update tab state
		tabs.selectExistingWorktree(tab.id, worktree.id, worktree.branch_name);
		tabs.setTabWorktreeMode(tab.id, 'existing');
	}

	function selectBranch(branch: Branch) {
		selectedType = 'branch';
		selectedBranch = branch;
		selectedWorktree = null;
		newBranchName = '';
		showDropdown = false;

		// Update tab state - branch selected as base, waiting for new branch name
		tabs.clearWorktreeSelection(tab.id);
		tabs.setTabWorktreeBaseBranch(tab.id, branch.name);
		tabs.setTabWorktreeBranch(tab.id, ''); // Will be set when user types new branch name
		tabs.setTabWorktreeMode(tab.id, 'new');
	}

	function handleNewBranchInput(e: Event) {
		const input = e.target as HTMLInputElement;
		newBranchName = input.value;
		// Update tab with the new branch name that will be created
		// Store the new branch name in worktreeBranch (the branch to create)
		// The base branch (selectedBranch) is stored in worktreeBaseBranch
		if (selectedBranch) {
			tabs.updateTab(tab.id, {
				worktreeBranch: input.value.trim(),
				worktreeBaseBranch: selectedBranch.name
			});
		}
	}

	function handleDropdownClick() {
		if (!loading && tab.project) {
			showDropdown = !showDropdown;
		}
	}

	function handleClickOutside(e: MouseEvent) {
		if (dropdownRef && !dropdownRef.contains(e.target as Node)) {
			showDropdown = false;
		}
	}

	function retryLoad() {
		if (tab.project) {
			lastLoadedProject = null;
			loadData(tab.project);
		}
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

	<!-- Worktree Options -->
	{#if tab.executionMode === 'worktree'}
		<div class="worktree-options">
			{#if !tab.project}
				<div class="warning-text">Select a project to use Worktree mode</div>
			{:else if error}
				<div class="error-state">
					<span>{error}</span>
					<button type="button" class="retry-btn" onclick={retryLoad}>Retry</button>
				</div>
			{:else}
				<!-- Branch/Worktree Dropdown -->
				<div class="dropdown-wrapper" bind:this={dropdownRef}>
					<button
						id="branch-dropdown"
						type="button"
						class="dropdown-trigger"
						onclick={handleDropdownClick}
						disabled={loading}
					>
						{#if loading}
							<svg class="w-4 h-4 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
							</svg>
							<span class="placeholder">Loading...</span>
						{:else if dropdownDisplayText()}
							<span class="selected-value">{dropdownDisplayText()}</span>
							{#if selectedType === 'worktree'}
								<span class="type-badge worktree">worktree</span>
							{:else if selectedType === 'branch'}
								<span class="type-badge branch">branch</span>
							{/if}
						{:else}
							<span class="placeholder">Select a branch or existing worktree...</span>
						{/if}
						<svg class="w-4 h-4 chevron" class:open={showDropdown} fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
						</svg>
					</button>

					<!-- Dropdown Menu -->
					{#if showDropdown}
						<div class="dropdown-menu">
							<!-- Existing Worktrees Section -->
							{#if worktrees.length > 0}
								<div class="dropdown-section">
									<div class="section-header">
										<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
										</svg>
										<span>Existing Worktrees</span>
									</div>
									{#each worktrees as worktree}
										<button
											type="button"
											class="dropdown-item"
											class:selected={selectedWorktree?.id === worktree.id}
											onclick={() => selectWorktree(worktree)}
										>
											<span class="item-name">{worktree.branch_name}</span>
											{#if worktree.session_count && worktree.session_count > 0}
												<span class="session-count">{worktree.session_count} sessions</span>
											{/if}
											{#if selectedWorktree?.id === worktree.id}
												<svg class="w-4 h-4 check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
												</svg>
											{/if}
										</button>
									{/each}
								</div>
							{/if}

							<!-- Available Branches Section (Create New Worktree) -->
							{#if availableBranches().length > 0}
								<div class="dropdown-section">
									<div class="section-header">
										<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
										</svg>
										<span>Create New Worktree From</span>
									</div>
									{#each availableBranches() as branch}
										<button
											type="button"
											class="dropdown-item"
											class:selected={selectedBranch?.name === branch.name}
											onclick={() => selectBranch(branch)}
										>
											<span class="item-name">{branch.name}</span>
											{#if branch.remote}
												<span class="remote-badge">remote</span>
											{/if}
											{#if selectedBranch?.name === branch.name}
												<svg class="w-4 h-4 check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
												</svg>
											{/if}
										</button>
									{/each}
								</div>
							{/if}

							<!-- Empty State -->
							{#if worktrees.length === 0 && availableBranches().length === 0}
								<div class="dropdown-empty">
									<span>No branches or worktrees available</span>
								</div>
							{/if}
						</div>
					{/if}
				</div>

				<!-- New Branch Name Input (only shown when creating new worktree from branch) -->
				{#if showNewBranchInput()}
					<div class="new-branch-input-wrapper">
						<input
							id="new-branch-name"
							type="text"
							class="text-input"
							class:invalid={newBranchName.trim() && !isValidNewBranch()}
							placeholder="Enter new branch name..."
							value={newBranchName}
							oninput={handleNewBranchInput}
						/>
						{#if newBranchName.trim() && !isValidNewBranch()}
							<span class="validation-error">Branch name already exists</span>
						{/if}
					</div>
				{/if}

				<!-- Selection Summary -->
				{#if selectionSummary()}
					<div class="selection-summary" class:ready={selectedType === 'worktree' || (selectedType === 'branch' && isValidNewBranch() && newBranchName.trim())}>
						{#if selectedType === 'worktree'}
							<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
							</svg>
						{:else}
							<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
							</svg>
						{/if}
						<span>{selectionSummary()}</span>
					</div>
				{/if}
			{/if}
		</div>
	{/if}
</div>

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
		border-radius: 0.625rem;
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
		border-radius: 0.5rem;
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
		border-radius: 0.5rem;
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
		border-radius: 0.5rem;
		font-size: 0.8125rem;
		color: var(--foreground);
		cursor: pointer;
		transition: border-color 0.15s, box-shadow 0.15s;
	}

	.dropdown-trigger:hover:not(:disabled) {
		border-color: var(--primary);
	}

	.dropdown-trigger:disabled {
		opacity: 0.5;
		cursor: not-allowed;
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

	.dropdown-trigger .chevron {
		transition: transform 0.2s;
		color: var(--muted-foreground);
	}

	.dropdown-trigger .chevron.open {
		transform: rotate(180deg);
	}

	.type-badge {
		font-size: 0.5625rem;
		font-weight: 600;
		padding: 0.125rem 0.375rem;
		border-radius: 0.25rem;
		text-transform: uppercase;
		letter-spacing: 0.025em;
	}

	.type-badge.worktree {
		background: color-mix(in srgb, var(--primary) 15%, transparent);
		color: var(--primary);
	}

	.type-badge.branch {
		background: color-mix(in srgb, #22c55e 15%, transparent);
		color: #22c55e;
	}

	/* Dropdown Menu */
	.dropdown-menu {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		right: 0;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
		z-index: 50;
		max-height: 280px;
		overflow-y: auto;
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

	.dropdown-item .session-count {
		font-size: 0.625rem;
		color: var(--muted-foreground);
	}

	.dropdown-item .remote-badge {
		font-size: 0.5625rem;
		font-weight: 500;
		padding: 0.125rem 0.25rem;
		border-radius: 0.25rem;
		background: color-mix(in srgb, var(--muted) 50%, transparent);
		color: var(--muted-foreground);
	}

	.dropdown-item .check-icon {
		color: var(--primary);
	}

	.dropdown-empty {
		padding: 1rem;
		text-align: center;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	/* New Branch Input */
	.new-branch-input-wrapper {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.text-input {
		width: 100%;
		padding: 0.5rem 0.75rem;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		font-size: 0.8125rem;
		color: var(--foreground);
		transition: border-color 0.15s, box-shadow 0.15s;
	}

	.text-input:focus {
		outline: none;
		border-color: var(--primary);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary) 20%, transparent);
	}

	.text-input.invalid {
		border-color: var(--destructive);
	}

	.text-input.invalid:focus {
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--destructive) 20%, transparent);
	}

	.text-input::placeholder {
		color: var(--muted-foreground);
	}

	.validation-error {
		font-size: 0.6875rem;
		color: var(--destructive);
	}

	/* Selection Summary */
	.selection-summary {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.5rem 0.625rem;
		background: color-mix(in srgb, var(--muted) 40%, transparent);
		border-radius: 0.375rem;
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
		border-radius: 0.25rem;
		transition: opacity 0.15s;
	}

	.retry-btn:hover {
		opacity: 0.9;
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

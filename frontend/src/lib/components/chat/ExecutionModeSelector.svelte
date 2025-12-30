<script lang="ts">
	/**
	 * ExecutionModeSelector - Mode selector for empty chat state
	 *
	 * Allows user to choose between Local mode (work on current branch)
	 * or Work Tree mode (create new worktree or use existing one).
	 *
	 * Only shown in empty state - disappears once chat starts.
	 */
	import { tabs, type ChatTab, type ExecutionMode } from '$lib/stores/tabs';
	import { getBranches, getWorktrees, syncWorktrees, type Branch, type Worktree } from '$lib/api/git';
	import { onMount } from 'svelte';

	interface Props {
		tab: ChatTab;
	}

	let { tab }: Props = $props();

	// State
	let branches = $state<Branch[]>([]);
	let worktrees = $state<Worktree[]>([]);
	let loadingBranches = $state(false);
	let loadingWorktrees = $state(false);
	let branchesError = $state<string | null>(null);
	let worktreesError = $state<string | null>(null);
	let showBaseBranchDropdown = $state(false);
	let showWorktreeDropdown = $state(false);
	let baseBranchDropdownRef = $state<HTMLDivElement | null>(null);
	let worktreeDropdownRef = $state<HTMLDivElement | null>(null);
	let lastLoadedProject = $state<string | null>(null);
	let lastSyncedProject = $state<string | null>(null);
	let lastLoadedWorktreesProject = $state<string | null>(null);

	// Sync worktrees when project changes (clean up stale database records)
	$effect(() => {
		const project = tab.project;

		// Sync worktrees whenever the project changes (regardless of mode)
		// This cleans up stale database records for worktrees deleted outside the app
		if (project && project !== lastSyncedProject) {
			syncProjectWorktrees(project);
		}
	});

	// Load branches when project changes or mode switches to worktree (for new worktree creation)
	$effect(() => {
		const project = tab.project;
		const mode = tab.executionMode;
		const worktreeMode = tab.worktreeMode;

		// Only load branches if in worktree mode, creating new, have a project, and haven't loaded for this project yet
		if (mode === 'worktree' && worktreeMode === 'new' && project && project !== lastLoadedProject && !loadingBranches) {
			loadBranches(project);
		}
	});

	// Load existing worktrees when in worktree mode and selecting existing
	$effect(() => {
		const project = tab.project;
		const mode = tab.executionMode;
		const worktreeMode = tab.worktreeMode;

		// Load worktrees if in worktree mode, using existing, have a project, and haven't loaded for this project yet
		if (mode === 'worktree' && worktreeMode === 'existing' && project && project !== lastLoadedWorktreesProject && !loadingWorktrees) {
			loadWorktrees(project);
		}
	});

	async function syncProjectWorktrees(projectId: string) {
		try {
			const result = await syncWorktrees(projectId);
			lastSyncedProject = projectId;

			if (result.cleaned_up.length > 0) {
				// Stale worktree records cleaned up silently
				// If we already loaded worktrees, reload them
				if (lastLoadedWorktreesProject === projectId) {
					loadWorktrees(projectId);
				}
			}
		} catch (e) {
			// Don't block on sync errors - just log them
			console.warn('[WorktreeSync] Failed to sync worktrees:', e);
		}
	}

	async function loadBranches(projectId: string) {
		loadingBranches = true;
		branchesError = null;

		try {
			const result = await getBranches(projectId, false);
			branches = result;
			lastLoadedProject = projectId;
		} catch (e) {
			console.error('Failed to load branches:', e);
			branchesError = e instanceof Error ? e.message : 'Failed to load branches';
			branches = [];
		} finally {
			loadingBranches = false;
		}
	}

	async function loadWorktrees(projectId: string) {
		loadingWorktrees = true;
		worktreesError = null;

		try {
			const result = await getWorktrees(projectId);
			// Filter to only show worktrees that exist on disk
			worktrees = result.filter(w => w.exists !== false);
			lastLoadedWorktreesProject = projectId;
		} catch (e) {
			console.error('Failed to load worktrees:', e);
			worktreesError = e instanceof Error ? e.message : 'Failed to load worktrees';
			worktrees = [];
		} finally {
			loadingWorktrees = false;
		}
	}

	// Force reload branches (for retry button)
	function retryLoadBranches() {
		if (tab.project) {
			lastLoadedProject = null; // Reset to allow reload
			loadBranches(tab.project);
		}
	}

	// Force reload worktrees (for retry button)
	function retryLoadWorktrees() {
		if (tab.project) {
			lastLoadedWorktreesProject = null;
			loadWorktrees(tab.project);
		}
	}

	function setExecutionMode(mode: ExecutionMode) {
		tabs.setTabExecutionMode(tab.id, mode);

		// Clear branch fields when switching to local
		if (mode === 'local') {
			tabs.setTabWorktreeBranch(tab.id, '');
			tabs.clearWorktreeSelection(tab.id);
		}
	}

	function setWorktreeMode(mode: 'new' | 'existing') {
		tabs.setTabWorktreeMode(tab.id, mode);

		// Clear selection when switching modes
		if (mode === 'new') {
			tabs.clearWorktreeSelection(tab.id);
		} else {
			tabs.setTabWorktreeBranch(tab.id, '');
		}
	}

	function setWorktreeBranch(e: Event) {
		const input = e.target as HTMLInputElement;
		tabs.setTabWorktreeBranch(tab.id, input.value);
	}

	function selectBaseBranch(branch: string) {
		tabs.setTabWorktreeBaseBranch(tab.id, branch);
		showBaseBranchDropdown = false;
	}

	function selectExistingWorktree(worktree: Worktree) {
		tabs.selectExistingWorktree(tab.id, worktree.id, worktree.branch_name);
		showWorktreeDropdown = false;
	}

	// Close dropdowns when clicking outside
	function handleClickOutside(e: MouseEvent) {
		if (baseBranchDropdownRef && !baseBranchDropdownRef.contains(e.target as Node)) {
			showBaseBranchDropdown = false;
		}
		if (worktreeDropdownRef && !worktreeDropdownRef.contains(e.target as Node)) {
			showWorktreeDropdown = false;
		}
	}

	onMount(() => {
		document.addEventListener('click', handleClickOutside);
		return () => document.removeEventListener('click', handleClickOutside);
	});
</script>

<div class="mode-selector">
	<!-- Segmented Control -->
	<div class="segment-control">
		<button
			type="button"
			class="segment-btn"
			class:active={tab.executionMode === 'local'}
			onclick={() => setExecutionMode('local')}
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
			</svg>
			<span>Local</span>
		</button>
		<button
			type="button"
			class="segment-btn"
			class:active={tab.executionMode === 'worktree'}
			onclick={() => setExecutionMode('worktree')}
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" />
			</svg>
			<span>Work Tree</span>
		</button>
	</div>

	<!-- Work Tree Options -->
	{#if tab.executionMode === 'worktree'}
		<div class="worktree-options">
			<!-- Worktree Mode Sub-selection -->
			<div class="worktree-mode-toggle">
				<button
					type="button"
					class="mode-toggle-btn"
					class:active={tab.worktreeMode === 'new'}
					onclick={() => setWorktreeMode('new')}
				>
					<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
					</svg>
					<span>New</span>
				</button>
				<button
					type="button"
					class="mode-toggle-btn"
					class:active={tab.worktreeMode === 'existing'}
					onclick={() => setWorktreeMode('existing')}
				>
					<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
					</svg>
					<span>Existing</span>
				</button>
			</div>

			{#if tab.worktreeMode === 'new'}
				<!-- New Worktree: Base Branch Selector -->
				<div class="option-row">
					<label class="option-label">Base branch</label>
					<div class="selector-wrapper" bind:this={baseBranchDropdownRef}>
						<button
							type="button"
							class="branch-selector-btn"
							onclick={() => showBaseBranchDropdown = !showBaseBranchDropdown}
							disabled={!tab.project}
						>
							<svg class="w-3.5 h-3.5 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 5l7 7-7 7M5 5l7 7-7 7" />
							</svg>
							<span>{tab.worktreeBaseBranch || 'main'}</span>
							<svg class="w-3 h-3 opacity-50 chevron" class:open={showBaseBranchDropdown} fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
							</svg>
						</button>

						{#if showBaseBranchDropdown}
							<div class="branch-dropdown">
								{#if loadingBranches}
									<div class="dropdown-loading">
										<svg class="w-4 h-4 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
										</svg>
										<span>Loading branches...</span>
									</div>
								{:else if branchesError}
									<div class="dropdown-error">
										<span>{branchesError}</span>
										<button type="button" class="retry-btn" onclick={retryLoadBranches}>Retry</button>
									</div>
								{:else if branches.length === 0}
									<div class="dropdown-empty">No branches found</div>
								{:else}
									{#each branches as branch}
										<button
											type="button"
											class="dropdown-item"
											class:selected={branch.name === tab.worktreeBaseBranch}
											onclick={() => selectBaseBranch(branch.name)}
										>
											<svg class="w-3.5 h-3.5 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 5l7 7-7 7M5 5l7 7-7 7" />
											</svg>
											<span>{branch.name}</span>
											{#if branch.name === tab.worktreeBaseBranch}
												<svg class="w-3.5 h-3.5 ml-auto text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
												</svg>
											{/if}
										</button>
									{/each}
								{/if}
							</div>
						{/if}
					</div>
				</div>

				<!-- New Worktree: New Branch Name Input -->
				<div class="option-row">
					<label class="option-label">New branch</label>
					<input
						type="text"
						class="branch-input"
						placeholder="feature/my-feature"
						value={tab.worktreeBranch}
						oninput={setWorktreeBranch}
						disabled={!tab.project}
					/>
				</div>
			{:else}
				<!-- Existing Worktree: Worktree Selector -->
				<div class="option-row">
					<label class="option-label">Worktree</label>
					<div class="selector-wrapper" bind:this={worktreeDropdownRef}>
						<button
							type="button"
							class="branch-selector-btn"
							onclick={() => showWorktreeDropdown = !showWorktreeDropdown}
							disabled={!tab.project}
						>
							<svg class="w-3.5 h-3.5 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
							</svg>
							<span>{tab.selectedWorktreeBranch || 'Select worktree...'}</span>
							<svg class="w-3 h-3 opacity-50 chevron" class:open={showWorktreeDropdown} fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
							</svg>
						</button>

						{#if showWorktreeDropdown}
							<div class="branch-dropdown">
								{#if loadingWorktrees}
									<div class="dropdown-loading">
										<svg class="w-4 h-4 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
										</svg>
										<span>Loading worktrees...</span>
									</div>
								{:else if worktreesError}
									<div class="dropdown-error">
										<span>{worktreesError}</span>
										<button type="button" class="retry-btn" onclick={retryLoadWorktrees}>Retry</button>
									</div>
								{:else if worktrees.length === 0}
									<div class="dropdown-empty">
										<span>No worktrees found</span>
										<span class="dropdown-hint">Create a new worktree first</span>
									</div>
								{:else}
									{#each worktrees as worktree}
										<button
											type="button"
											class="dropdown-item"
											class:selected={worktree.id === tab.worktreeId}
											onclick={() => selectExistingWorktree(worktree)}
										>
											<svg class="w-3.5 h-3.5 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 5l7 7-7 7M5 5l7 7-7 7" />
											</svg>
											<span class="worktree-name">{worktree.branch_name}</span>
											{#if worktree.session_count && worktree.session_count > 0}
												<span class="session-count">{worktree.session_count} session{worktree.session_count > 1 ? 's' : ''}</span>
											{/if}
											{#if worktree.id === tab.worktreeId}
												<svg class="w-3.5 h-3.5 ml-auto text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
												</svg>
											{/if}
										</button>
									{/each}
								{/if}
							</div>
						{/if}
					</div>
				</div>

				<!-- Show selected worktree info -->
				{#if tab.worktreeId && tab.selectedWorktreeBranch}
					<div class="selected-info">
						<svg class="w-3.5 h-3.5 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>
						<span>Ready to start session on <code>{tab.selectedWorktreeBranch}</code></span>
					</div>
				{/if}
			{/if}

			{#if !tab.project}
				<div class="warning-text">
					Select a project to use Work Tree mode
				</div>
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

	/* Work Tree Options */
	.worktree-options {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		padding: 0.5rem;
		background: color-mix(in srgb, var(--muted) 30%, transparent);
		border-radius: 0.5rem;
	}

	/* Worktree Mode Toggle (New/Existing) */
	.worktree-mode-toggle {
		display: flex;
		background: color-mix(in srgb, var(--muted) 40%, transparent);
		border-radius: 0.375rem;
		padding: 0.125rem;
		gap: 0.125rem;
	}

	.mode-toggle-btn {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 0.25rem;
		padding: 0.375rem 0.5rem;
		border-radius: 0.25rem;
		font-size: 0.6875rem;
		font-weight: 500;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.mode-toggle-btn:hover {
		color: var(--foreground);
	}

	.mode-toggle-btn.active {
		background: var(--background);
		color: var(--foreground);
		box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
	}

	:global(.dark) .mode-toggle-btn.active {
		background: color-mix(in srgb, var(--card) 100%, transparent);
	}

	.option-row {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}

	.option-label {
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--muted-foreground);
		min-width: 70px;
	}

	/* Branch Selector */
	.selector-wrapper {
		position: relative;
		flex: 1;
	}

	.branch-selector-btn {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		width: 100%;
		padding: 0.375rem 0.5rem;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 0.375rem;
		font-size: 0.75rem;
		color: var(--foreground);
		transition: border-color 0.15s;
	}

	.branch-selector-btn:hover:not(:disabled) {
		border-color: var(--primary);
	}

	.branch-selector-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.branch-selector-btn span {
		flex: 1;
		text-align: left;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.chevron {
		transition: transform 0.15s;
	}

	.chevron.open {
		transform: rotate(180deg);
	}

	/* Branch Dropdown */
	.branch-dropdown {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		right: 0;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
		z-index: 50;
		max-height: 200px;
		overflow-y: auto;
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		width: 100%;
		padding: 0.5rem 0.75rem;
		font-size: 0.75rem;
		color: var(--foreground);
		transition: background 0.1s;
	}

	.dropdown-item:hover {
		background: var(--accent);
	}

	.dropdown-item.selected {
		background: color-mix(in srgb, var(--primary) 15%, transparent);
	}

	.worktree-name {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.session-count {
		font-size: 0.625rem;
		color: var(--muted-foreground);
		background: color-mix(in srgb, var(--muted) 50%, transparent);
		padding: 0.125rem 0.375rem;
		border-radius: 0.25rem;
	}

	.dropdown-loading,
	.dropdown-empty {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 0.25rem;
		padding: 0.75rem;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		text-align: center;
	}

	.dropdown-hint {
		font-size: 0.6875rem;
		opacity: 0.7;
	}

	.dropdown-error {
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
		padding: 0.25rem 0.5rem;
		font-size: 0.6875rem;
		background: var(--primary);
		color: var(--primary-foreground);
		border-radius: 0.25rem;
		transition: opacity 0.15s;
	}

	.retry-btn:hover {
		opacity: 0.9;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	.animate-spin {
		animation: spin 1s linear infinite;
	}

	/* Branch Input */
	.branch-input {
		flex: 1;
		padding: 0.375rem 0.5rem;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 0.375rem;
		font-size: 0.75rem;
		color: var(--foreground);
		transition: border-color 0.15s;
	}

	.branch-input:focus {
		outline: none;
		border-color: var(--primary);
	}

	.branch-input:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.branch-input::placeholder {
		color: var(--muted-foreground);
	}

	/* Selected Info */
	.selected-info {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.375rem 0.5rem;
		background: color-mix(in srgb, var(--primary) 10%, transparent);
		border-radius: 0.375rem;
		font-size: 0.6875rem;
		color: var(--foreground);
	}

	.selected-info code {
		font-family: monospace;
		font-size: 0.625rem;
		background: color-mix(in srgb, var(--muted) 50%, transparent);
		padding: 0.125rem 0.25rem;
		border-radius: 0.25rem;
	}

	/* Warning */
	.warning-text {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		text-align: center;
		padding: 0.25rem;
	}
</style>

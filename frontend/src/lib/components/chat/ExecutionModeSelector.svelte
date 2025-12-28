<script lang="ts">
	/**
	 * ExecutionModeSelector - Mode selector for empty chat state
	 *
	 * Allows user to choose between Local mode (work on current branch)
	 * or Work Tree mode (create isolated branch for focused work).
	 *
	 * Only shown in empty state - disappears once chat starts.
	 */
	import { tabs, type ChatTab, type ExecutionMode } from '$lib/stores/tabs';
	import { getBranches, syncWorktrees, type Branch } from '$lib/api/git';
	import { onMount } from 'svelte';

	interface Props {
		tab: ChatTab;
	}

	let { tab }: Props = $props();

	// State
	let branches = $state<Branch[]>([]);
	let loadingBranches = $state(false);
	let branchesError = $state<string | null>(null);
	let showBaseBranchDropdown = $state(false);
	let baseBranchDropdownRef = $state<HTMLDivElement | null>(null);
	let lastLoadedProject = $state<string | null>(null);
	let lastSyncedProject = $state<string | null>(null);

	// Sync worktrees when project changes (clean up stale database records)
	$effect(() => {
		const project = tab.project;

		// Sync worktrees whenever the project changes (regardless of mode)
		// This cleans up stale database records for worktrees deleted outside the app
		if (project && project !== lastSyncedProject) {
			syncProjectWorktrees(project);
		}
	});

	// Load branches when project changes or mode switches to worktree
	$effect(() => {
		const project = tab.project;
		const mode = tab.executionMode;

		// Only load if in worktree mode, have a project, and haven't loaded for this project yet
		if (mode === 'worktree' && project && project !== lastLoadedProject && !loadingBranches) {
			loadBranches(project);
		}
	});

	async function syncProjectWorktrees(projectId: string) {
		try {
			const result = await syncWorktrees(projectId);
			lastSyncedProject = projectId;

			if (result.cleaned_up.length > 0) {
				console.log(`[WorktreeSync] Cleaned up ${result.cleaned_up.length} stale worktree records for project ${projectId}:`, result.cleaned_up);
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

	// Force reload branches (for retry button)
	function retryLoadBranches() {
		if (tab.project) {
			lastLoadedProject = null; // Reset to allow reload
			loadBranches(tab.project);
		}
	}

	function setExecutionMode(mode: ExecutionMode) {
		tabs.setTabExecutionMode(tab.id, mode);

		// Clear branch fields when switching to local
		if (mode === 'local') {
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

	// Close dropdown when clicking outside
	function handleClickOutside(e: MouseEvent) {
		if (baseBranchDropdownRef && !baseBranchDropdownRef.contains(e.target as Node)) {
			showBaseBranchDropdown = false;
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
			<!-- Base Branch Selector -->
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

			<!-- New Branch Name Input -->
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

	.dropdown-loading,
	.dropdown-empty {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 0.5rem;
		padding: 0.75rem;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		text-align: center;
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

	/* Warning */
	.warning-text {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		text-align: center;
		padding: 0.25rem;
	}
</style>

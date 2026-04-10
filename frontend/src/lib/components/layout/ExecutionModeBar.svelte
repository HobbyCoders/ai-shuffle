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
	import { activeSession } from '$lib/stores/active-session';
	import type { ExecutionMode } from '$lib/stores/types';
	import { syncWorktrees, createWorktree as createWorktreeApi, type Worktree, type Branch } from '$lib/api/git';
	import { worktrees as worktreeStore, branches as branchStore, git, currentBranch as gitCurrentBranch } from '$lib/stores/git';
	import { ChevronDown, GitBranch, Monitor, GitFork, Plus, Loader2 } from 'lucide-svelte';
	import { untrack } from 'svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';

	// --- UI state ---
	let showBranchDropdown = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);
	let loading = $state(false);
	let lastSyncedProject = $state<string | null>(null);

	// --- New branch modal state ---
	let showCreateModal = $state(false);
	let creatingWorktree = $state(false);
	let localWorktreeError = $state<string | null>(null);

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

	// Current branch display name
	const currentBranch = $derived($activeSession.selectedWorktreeBranch || $activeSession.worktreeBranch || 'Select branch');

	// Combined worktree error from store or local eager-creation
	const worktreeError = $derived($activeSession.worktreeError || localWorktreeError);

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
		activeSession.setExecutionMode(mode);
	}

	function selectWorktree(wt: Worktree) {
		activeSession.selectWorktreeBranch(wt.branch_name, wt.id);
		showBranchDropdown = false;
	}

	function selectBranch(branch: Branch) {
		showBranchDropdown = false;
		const branchName = branch.remote ? branch.name.replace(/^[^\/]+\//, '') : branch.name;

		// Update session state
		activeSession.setWorktreeBranch(branchName);
		activeSession.setWorktreeMode('new');
		activeSession.setWorktreeCreateNewBranch(false);

		// Eagerly create the worktree directory
		eagerCreateWorktree(branchName, false);
	}

	async function eagerCreateWorktree(branchName: string, createNew: boolean, base?: string) {
		if (!$activeSession.project) return;
		creatingWorktree = true;
		localWorktreeError = null;
		activeSession.clearWorktreeError();
		try {
			const response = await createWorktreeApi($activeSession.project, {
				branch_name: branchName,
				create_new_branch: createNew,
				base_branch: base
			});
			if (response.success && response.worktree) {
				activeSession.selectWorktreeBranch(branchName, response.worktree.id);
				// Refresh so new worktree appears in list
				await git.loadWorktrees();
			} else {
				localWorktreeError = response.message || `Failed to create worktree for branch '${branchName}'`;
			}
		} catch (err: any) {
			const rawMsg = err?.detail || err?.message || '';
			localWorktreeError = formatLocalWorktreeError(rawMsg, branchName, err?.status);
		} finally {
			creatingWorktree = false;
		}
	}

	/** Format worktree creation errors into actionable user-facing messages */
	function formatLocalWorktreeError(raw: string, branchName: string, httpStatus?: number): string {
		const lower = raw.toLowerCase();

		// Git-specific errors
		if (lower.includes('already checked out') || lower.includes('is already used by worktree')) {
			return `Branch '${branchName}' already has an active worktree. Select it from the dropdown instead.`;
		}
		if (lower.includes('not a valid branch') || lower.includes('branch not found') ||
			lower.includes('unknown revision') || (lower.includes('pathspec') && lower.includes('did not match'))) {
			return `Branch '${branchName}' doesn't exist. Fetch from remote first or create a new branch.`;
		}
		if (lower.includes('already exists') && (lower.includes('path') || lower.includes('folder') || lower.includes('directory'))) {
			return `A folder already exists at the worktree path for '${branchName}'. Try a different branch name.`;
		}
		if (lower.includes('.lock') || lower.includes('unable to lock') || lower.includes('index.lock')) {
			return 'A git operation is in progress. Wait a moment and try again.';
		}
		if (lower.includes('permission denied') || lower.includes('access denied')) {
			return 'Permission denied creating worktree. Check folder permissions on the server.';
		}
		if (lower.includes('not a git repository')) {
			return 'The project directory is not a git repository. Initialize git first.';
		}
		if (lower.includes('dirty') || lower.includes('uncommitted') || lower.includes('unstaged')) {
			return 'Uncommitted changes detected. Commit or stash your changes before creating a worktree.';
		}

		// HTTP-level errors
		if (httpStatus === 404) {
			return `Project not found. The project may have been deleted or moved.`;
		}
		if (httpStatus === 401 || httpStatus === 403) {
			return 'Authentication error. Try refreshing the page and logging in again.';
		}
		if (httpStatus && httpStatus >= 500) {
			return raw
				? `Server error creating worktree: ${raw}`
				: 'Server error creating worktree. Check the backend logs for details.';
		}

		// Network errors (no HTTP status = fetch itself failed)
		if (lower.includes('failed to fetch') || lower.includes('networkerror') || lower.includes('network')) {
			return 'Network error — cannot reach the server. Check your connection.';
		}
		if (lower.includes('timeout') || lower.includes('aborted')) {
			return 'Request timed out creating worktree. The server may be overloaded.';
		}

		// If we have a raw message, include it with context
		if (raw) {
			return `Failed to create worktree for '${branchName}': ${raw}`;
		}

		// True last resort — should rarely hit this after API client improvements
		return `Failed to create worktree for '${branchName}'. Check the backend logs for details.`;
	}

	function toggleDropdown() {
		showBranchDropdown = !showBranchDropdown;
		if (showBranchDropdown && $activeSession.project) {
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
		// Branch was already created by CreateBranchModal, so use create_new_branch=false
		activeSession.setWorktreeBranch(branchName);
		activeSession.setWorktreeMode('new');
		activeSession.setWorktreeCreateNewBranch(false);

		// Create the worktree for the already-created branch
		eagerCreateWorktree(branchName, false);
	}
</script>

<svelte:window onclick={handleClickOutside} />

<div class="execution-mode-bar">
	<div class="mode-bar-row">
		<!-- Segmented pill toggle -->
		<div class="mode-toggle">
			<button
				class="mode-option"
				class:active={$activeSession.executionMode === 'local'}
				onclick={() => setMode('local')}
			>
				<Monitor size={13} strokeWidth={1.5} />
				<span>Local</span>
			</button>
			<button
				class="mode-option"
				class:active={$activeSession.executionMode === 'worktree'}
				onclick={() => setMode('worktree')}
			>
				<GitFork size={13} strokeWidth={1.5} />
				<span>Worktree</span>
			</button>
		</div>

		<!-- Branch selector (shown when worktree mode active) -->
		{#if $activeSession.executionMode === 'worktree'}
			<div class="branch-selector" bind:this={dropdownRef}>
			<button
				class="branch-trigger"
				onclick={toggleDropdown}
				disabled={creatingWorktree}
			>
				{#if creatingWorktree}
					<Loader2 size={12} strokeWidth={1.5} class="animate-spin" />
				{:else}
					<GitBranch size={12} strokeWidth={1.5} />
				{/if}
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

	<!-- Error shown below the pills row so it doesn't overflow on mobile -->
	{#if $activeSession.executionMode === 'worktree' && worktreeError}
		<div class="wt-error-row">
			<div class="wt-error-banner">
				<svg class="wt-error-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" width="14" height="14">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z" />
				</svg>
				<span class="wt-error-text">{worktreeError}</span>
				<button
					type="button"
					class="wt-error-dismiss"
					onclick={() => { localWorktreeError = null; activeSession.clearWorktreeError(); }}
					aria-label="Dismiss error"
				>&times;</button>
			</div>
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
	.execution-mode-bar {
		position: relative;
		z-index: 10;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 6px;
		padding: 8px 0 4px;
		max-width: 100%;
		overflow: visible;
	}

	.mode-bar-row {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 12px;
		max-width: 100%;
	}

	/* Segmented pill */
	.mode-toggle {
		display: inline-flex;
		align-items: center;
		background: color-mix(in oklch, var(--foreground) 8%, var(--background));
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 2px;
		gap: 2px;
		flex-shrink: 0;
	}

	.mode-option {
		display: flex;
		align-items: center;
		gap: 5px;
		padding: 4px 12px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius) - 2px);
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

	.wt-error-banner {
		display: flex;
		align-items: flex-start;
		gap: 6px;
		padding: 6px 10px;
		border-radius: 8px;
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--destructive) 25%, transparent);
		max-width: 100%;
		min-width: 0;
	}

	.wt-error-icon {
		flex-shrink: 0;
		color: var(--destructive);
		margin-top: 1px;
	}

	.wt-error-text {
		flex: 1;
		min-width: 0;
		font-size: 0.6875rem;
		line-height: 1.4;
		color: var(--destructive);
		word-break: break-word;
	}

	.wt-error-dismiss {
		flex-shrink: 0;
		background: none;
		border: none;
		color: var(--destructive);
		font-size: 1rem;
		line-height: 1;
		cursor: pointer;
		padding: 0 2px;
		opacity: 0.7;
		transition: opacity 150ms ease;
	}

	.wt-error-dismiss:hover {
		opacity: 1;
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

	/* Mobile: tighter branch name limit */
	@media (max-width: 639px) {
		.branch-name {
			max-width: min(120px, 30vw);
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

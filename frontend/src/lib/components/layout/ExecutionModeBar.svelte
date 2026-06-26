<script lang="ts">
	/**
	 * ExecutionModeBar - Local/Worktree/Cloud selector with branch management
	 *
	 * Positioned below the chat input island. Shows a single dropdown
	 * pill for execution mode (the current mode's icon + label + chevron;
	 * clicking opens an upward menu of the available modes) with an inline
	 * branch selector when worktree mode is active. Includes "New Branch"
	 * creation modal.
	 *
	 * Uses the git store for worktree/branch data and syncWorktrees()
	 * to clean up stale DB records on project change.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import type { ExecutionMode } from '$lib/stores/types';
	import { syncWorktrees, createWorktree as createWorktreeApi, type Worktree, type Branch } from '$lib/api/git';
	import { worktrees as worktreeStore, branches as branchStore, git, currentBranch as gitCurrentBranch, gitStatus } from '$lib/stores/git.svelte';
	import { ChevronDown, GitBranch, Monitor, GitFork, Plus, Loader2, Cloud, Server } from 'lucide-svelte';
	import { untrack } from 'svelte';
	import { listInstalls, offloadTargets as computeOffloadTargets, type Install } from '$lib/api/installs';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';

	// --- UI state ---
	let showBranchDropdown = $state(false);
	let dropdownRef = $state<HTMLDivElement | null>(null);
	// Separate dropdown for the execution-mode picker (coexists with the branch
	// dropdown above; its own container ref so outside-click closes it independently).
	let showModeDropdown = $state(false);
	let modeDropdownRef = $state<HTMLDivElement | null>(null);
	let loading = $state(false);
	let lastSyncedProject = $state<string | null>(null);

	// --- New branch modal state ---
	let showCreateModal = $state(false);
	let creatingWorktree = $state(false);
	let localWorktreeError = $state<string | null>(null);

	// --- Cloud offload targets (registered installs) ---
	// `installs` is null while loading. Cloud offload targets a *registered*
	// install picked from a device dropdown (auto-registered on the shared DB).
	// The Cloud pill stays hidden until at least one valid target (online +
	// reachable base_url) resolves.
	let installs = $state<Install[] | null>(null);
	let cloudLoaded = $state(false);

	// Device-picker dropdown (shown next to the mode pill when Cloud is active).
	let showDeviceDropdown = $state(false);
	let deviceDropdownRef = $state<HTMLDivElement | null>(null);

	const offloadTargets = $derived(installs ? computeOffloadTargets(installs) : []);
	const cloudAvailable = $derived(offloadTargets.length > 0);

	async function loadCloudConfig() {
		if (cloudLoaded) return;
		cloudLoaded = true;
		// Load the registered offload targets so we know whether to show Cloud mode.
		try {
			const resp = await listInstalls();
			installs = resp.installs;
		} catch {
			installs = [];
		}
	}

	$effect(() => {
		// Load offload targets on mount so we know whether to show the Cloud option.
		loadCloudConfig();
		// Once it resolves: if Cloud isn't available but a stale persisted preference
		// left this session in cloud mode, fall back to local — the Cloud pill is
		// hidden, so the toggle would otherwise have no active option.
		if (cloudLoaded && !cloudAvailable && untrack(() => $activeSession.executionMode) === 'cloud') {
			setMode('local');
		}
	});

	// Auto-pick a default offload target when entering Cloud mode so the user can
	// fire off a session without a manual selection. Prefers the currently-chosen
	// target if still valid; else the first online reachable device.
	$effect(() => {
		if ($activeSession.executionMode !== 'cloud') return;
		const targets = offloadTargets;
		if (targets.length === 0) return;
		const current = untrack(() => $activeSession.cloudTargetInstanceId);
		if (current && targets.some((t) => t.id === current)) return;
		// No valid selection yet: default to the first device.
		activeSession.setCloudTarget(targets[0].id);
	});

	// The device whose name shows on the picker pill.
	const selectedTarget = $derived(
		offloadTargets.find((t) => t.id === $activeSession.cloudTargetInstanceId) ?? null
	);
	const deviceLabel = $derived(selectedTarget ? selectedTarget.device_name : 'Pick device');

	function selectDevice(install: Install | null) {
		activeSession.setCloudTarget(install ? install.id : null);
		showDeviceDropdown = false;
	}

	// --- Git-repo gating ---
	// Worktree/Cloud modes and the branch selector only make sense for a git
	// repository. Reuse the existing git-status signal (loaded by MainPanel's git
	// store effect for the active project) rather than inventing a new one — the
	// same `is_git_repo !== false` convention GitHeaderActions uses, so non-git
	// controls only hide once the backend confirms the folder isn't a repo (during
	// the brief load window status is null and we optimistically show everything).
	const isGitRepo = $derived($gitStatus?.is_git_repo !== false);

	$effect(() => {
		// A non-git project can only run Local. If a stale persisted preference (or
		// a switch from a git project) left this session in a hidden Worktree/Cloud
		// mode, fall back to Local so the toggle always has a visible active option.
		if (!isGitRepo && untrack(() => $activeSession.executionMode) !== 'local') {
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

	// --- Execution-mode dropdown ---

	type ModeOption = {
		mode: ExecutionMode;
		label: string;
		icon: typeof Monitor;
		title?: string;
	};

	// All mode options keyed by mode, so the trigger can render the current one and
	// the menu can render the available subset from a single source of truth.
	const MODE_OPTIONS: Record<ExecutionMode, ModeOption> = {
		local: { mode: 'local', label: 'Local', icon: Monitor },
		worktree: { mode: 'worktree', label: 'Worktree', icon: GitFork },
		cloud: {
			mode: 'cloud',
			label: 'Cloud',
			icon: Cloud,
			title: 'Offload this session to another registered AI Shuffle device (GitHub repos only)'
		}
	};

	// The currently-active mode option (drives the trigger pill's icon + label).
	const currentMode = $derived(MODE_OPTIONS[$activeSession.executionMode] ?? MODE_OPTIONS.local);

	// The modes offered in the menu, gated the same way the old segmented toggle was:
	// Local is always available; Worktree only for a git repo; Cloud only when a
	// second install is configured.
	const availableModes = $derived.by(() => {
		const modes: ModeOption[] = [MODE_OPTIONS.local];
		if (isGitRepo) {
			modes.push(MODE_OPTIONS.worktree);
			if (cloudAvailable) modes.push(MODE_OPTIONS.cloud);
		}
		return modes;
	});

	function toggleModeDropdown() {
		showModeDropdown = !showModeDropdown;
	}

	function selectMode(mode: ExecutionMode) {
		setMode(mode);
		showModeDropdown = false;
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

	// Close either dropdown on outside click (each guarded to only act when open)
	function handleClickOutside(e: MouseEvent) {
		if (showBranchDropdown && dropdownRef && !dropdownRef.contains(e.target as Node)) {
			showBranchDropdown = false;
		}
		if (showModeDropdown && modeDropdownRef && !modeDropdownRef.contains(e.target as Node)) {
			showModeDropdown = false;
		}
		if (showDeviceDropdown && deviceDropdownRef && !deviceDropdownRef.contains(e.target as Node)) {
			showDeviceDropdown = false;
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
		<!-- Mode dropdown pill: shows the current mode and, on click, opens an
		     upward menu of the available modes (Worktree/Cloud are gated to a git
		     repo / configured second install; for a non-git project only Local is
		     offered and the mode is forced to Local above). -->
		<div class="mode-selector" bind:this={modeDropdownRef}>
			<button
				type="button"
				class="mode-trigger"
				class:open={showModeDropdown}
				onclick={toggleModeDropdown}
				title={currentMode.title}
				aria-haspopup="menu"
				aria-expanded={showModeDropdown}
			>
				<currentMode.icon size={13} strokeWidth={1.5} />
				<span class="mode-trigger-label">{currentMode.label}</span>
				<ChevronDown size={12} strokeWidth={1.5} />
			</button>

			{#if showModeDropdown}
				<div class="mode-dropdown branch-dropdown" role="menu">
					{#each availableModes as opt (opt.mode)}
						<button
							type="button"
							class="branch-item"
							class:active={$activeSession.executionMode === opt.mode}
							onclick={() => selectMode(opt.mode)}
							title={opt.title}
							role="menuitemradio"
							aria-checked={$activeSession.executionMode === opt.mode}
						>
							<opt.icon size={13} strokeWidth={1.5} />
							<span class="branch-item-name">{opt.label}</span>
						</button>
					{/each}
				</div>
			{/if}
		</div>

		<!-- Branch selector (shown when worktree mode active) -->
		{#if $activeSession.executionMode === 'worktree'}
			<div class="branch-selector" bind:this={dropdownRef}>
			<button
				type="button"
				class="branch-trigger"
				class:auto-mode={isAutoMode}
				onclick={toggleDropdown}
				disabled={creatingWorktree}
				title={isAutoMode ? 'Branch will be auto-generated from your message. Click to select a specific branch.' : `Current branch: ${currentBranch}`}
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
		{/if}

		<!-- Device picker (shown when cloud mode active) — choose which registered
		     install runs the offload. Mirrors the branch selector pill. -->
		{#if $activeSession.executionMode === 'cloud' && offloadTargets.length > 0}
			<div class="device-selector" bind:this={deviceDropdownRef}>
				<button
					type="button"
					class="branch-trigger"
					onclick={() => (showDeviceDropdown = !showDeviceDropdown)}
					title={selectedTarget ? `Offload to ${selectedTarget.device_name}` : 'Choose the device to offload to'}
					aria-haspopup="menu"
					aria-expanded={showDeviceDropdown}
				>
					<Server size={12} strokeWidth={1.5} />
					<span class="branch-name">{deviceLabel}</span>
					<ChevronDown size={12} strokeWidth={1.5} />
				</button>

				{#if showDeviceDropdown}
					<div class="branch-dropdown" role="menu">
						<div class="branch-section-label">Devices</div>
						{#each offloadTargets as t (t.id)}
							<button
								type="button"
								class="branch-item"
								class:active={$activeSession.cloudTargetInstanceId === t.id}
								onclick={() => selectDevice(t)}
								role="menuitemradio"
								aria-checked={$activeSession.cloudTargetInstanceId === t.id}
							>
								<Server size={13} strokeWidth={1.5} />
								<span class="branch-item-name">{t.device_name}</span>
								{#if t.platform}<span class="device-platform">{t.platform}</span>{/if}
								<span class="device-online-dot"></span>
							</button>
						{/each}
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

	/* Device picker (cloud mode) — shares the branch pill/dropdown visuals */
	.device-selector {
		position: relative;
		min-width: 0;
	}
	.device-platform {
		font-size: 0.5625rem;
		font-weight: 500;
		padding: 1px 5px;
		border-radius: 4px;
		background: color-mix(in oklch, var(--muted-foreground) 15%, transparent);
		color: var(--muted-foreground);
		text-transform: capitalize;
	}
	.device-online-dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		flex-shrink: 0;
		background: color-mix(in oklch, var(--primary) 70%, #22c55e);
		box-shadow: 0 0 4px color-mix(in oklch, var(--primary) 50%, #22c55e);
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
		.branch-selector,
		.device-selector {
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

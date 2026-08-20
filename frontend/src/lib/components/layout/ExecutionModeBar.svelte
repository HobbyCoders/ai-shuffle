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
	import { listInstalls, getClusterGitState, type Install } from '$lib/api/installs';
	import { worktrees as worktreeStore, branches as branchStore, git, currentBranch as gitCurrentBranch, gitStatus } from '$lib/stores/git.svelte';
	import { ChevronDown, ChevronRight, Cloud, GitBranch, Monitor, GitFork, Plus, Loader2, Check } from 'lucide-svelte';
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
		// Refresh on every open so the online flags are current when the Cloud
		// section is expanded (the list is small and the call is cheap).
		if (showModeDropdown) loadInstalls();
	}

	function selectBaseMode() {
		// Local is the base mode; picking it drops any device choice so the
		// session is created on (and owned by) this install.
		activeSession.setDeviceIntent(null);
		cloudExpanded = false;
		showModeDropdown = false;
	}

	function toggleWorktree() {
		activeSession.setWorktreeIntent(worktreeOn ? 'local' : 'worktree');
		// Leave the menu open so the user sees the checkbox flip and can keep going.
	}

	// --- Cloud (other devices) section ---
	//
	// An inline accordion INSIDE the mode menu, not a flyout submenu: this
	// codebase has no submenu primitive, and a nested floating panel would need
	// its own outside-click/placement handling on mobile.
	let cloudExpanded = $state(false);
	let installs = $state<Install[]>([]);
	let installsLoading = $state(false);

	// Per-install checkout eligibility for the selected project (ruling D6),
	// keyed by install id. `null` = UNKNOWN — no project selected, the git-state
	// call failed, or the project has no cluster git state. Ruling W3-3 is
	// fail-open: unknown means no badges and no extra disabling, so a backend
	// hiccup can never lock the user out of their own devices.
	let eligibility = $state<Record<string, boolean> | null>(null);

	// Only online installs can accept a new cloud session. "This device" stays
	// represented by the Local item above rather than appearing twice.
	const onlineInstalls = $derived(installs.filter(i => !i.is_self && i.online));

	const deviceIntent = $derived($activeSession.deviceIntent);

	// Worktree and device compose freely (D7): picking a device sends the
	// worktree request to that install, which materializes the checkout on its
	// own disk via the durable git-ops queue. Neither choice constrains the other.

	// Only devices the backend explicitly reported as ineligible count — an
	// install missing from the git-state payload stays unknown (fail-open).
	function isIneligible(id: string): boolean {
		return eligibility !== null && eligibility[id] === false;
	}

	// A worktree has to be materialized from a checkout of this project on the
	// target device, so in worktree mode an ineligible device is unusable and
	// reads as disabled. In plain mode the badge is purely informational.
	function isBlocked(install: Install): boolean {
		return !install.online || (worktreeOn && isIneligible(install.id));
	}

	function deviceTitle(install: Install): string {
		if (!install.online) return `${install.device_name} is offline`;
		if (isIneligible(install.id)) {
			return worktreeOn
				? `No checkout of this project on ${install.device_name} — a worktree can't be created there`
				: `No checkout of this project on ${install.device_name}`;
		}
		return `Run this session on ${install.device_name}`;
	}

	async function loadInstalls() {
		installsLoading = true;
		// Eligibility rides along with the registry refresh on every open so the
		// badges match the picked project. Errors are swallowed at the source so a
		// rejection can never surface while the installs call is still in flight.
		const projectId = $activeSession.project;
		const gitStateReq = projectId
			? getClusterGitState(projectId).catch(e => {
				console.warn('[ExecutionModeBar] Failed to load git state:', e);
				return null;
			})
			: Promise.resolve(null);
		try {
			installs = (await listInstalls()).installs;
		} catch (e) {
			console.warn('[ExecutionModeBar] Failed to list devices:', e);
			installs = [];
		} finally {
			installsLoading = false;
		}
		const gitState = await gitStateReq;
		if (!gitState || gitState.project_v2_id === null) {
			eligibility = null;
			return;
		}
		const map: Record<string, boolean> = {};
		for (const row of gitState.installs) map[row.id] = row.eligible;
		eligibility = map;
	}

	function toggleCloud() {
		cloudExpanded = !cloudExpanded;
	}

	function selectDevice(install: Install) {
		if (isBlocked(install)) return;
		activeSession.setDeviceIntent({ id: install.id, name: install.device_name });
		showModeDropdown = false;
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
				title={deviceIntent ? `Run this session on ${deviceIntent.name}` : 'Run this session on this device'}
				aria-haspopup="menu"
				aria-expanded={showModeDropdown}
			>
				{#if deviceIntent}
					<Cloud size={13} strokeWidth={1.5} />
				{:else}
					<Monitor size={13} strokeWidth={1.5} />
				{/if}
				<span class="mode-trigger-label">{deviceIntent ? deviceIntent.name : 'Local'}</span>
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
						class="branch-item"
						class:active={!deviceIntent}
						onclick={() => selectBaseMode()}
						title="Run this session on this device"
						role="menuitemradio"
						aria-checked={!deviceIntent}
					>
						<Monitor size={13} strokeWidth={1.5} />
						<span class="branch-item-name">Local</span>
					</button>

					<!-- Cloud: inline accordion listing the other installs. Creating
					     the session there stamps it with that device's ownership. -->
					<button
						type="button"
						class="branch-item cloud-item"
						onclick={toggleCloud}
						title="Run this session on another device"
						aria-expanded={cloudExpanded}
					>
						<Cloud size={13} strokeWidth={1.5} />
						<span class="branch-item-name">Cloud</span>
						<ChevronRight size={12} strokeWidth={1.5} class={cloudExpanded ? 'cloud-caret open' : 'cloud-caret'} />
					</button>

					{#if cloudExpanded}
						<div class="cloud-section">
							{#if installsLoading}
								<div class="branch-loading">
									<Loader2 size={14} class="animate-spin" />
									<span>Loading...</span>
								</div>
							{:else if onlineInstalls.length === 0}
								<p class="branch-empty">No online devices</p>
							{:else}
								{#each onlineInstalls as install (install.id)}
									<button
										type="button"
										class="branch-item device-item"
										class:active={deviceIntent?.id === install.id}
										class:disabled={isBlocked(install)}
										onclick={() => selectDevice(install)}
										title={deviceTitle(install)}
										role="menuitemradio"
										aria-checked={deviceIntent?.id === install.id}
										aria-disabled={isBlocked(install)}
									>
										<Cloud size={13} strokeWidth={1.5} />
										<span class="branch-item-name">{install.device_name}</span>
										{#if isIneligible(install.id)}
											<span class="device-nocheckout">no checkout</span>
										{/if}
									</button>
								{/each}
							{/if}
						</div>
					{/if}

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
	/* There is no standalone bar any more: this component renders inline in the
	   ContextPillBar, which is now the composer island's top pill row. It flows as
	   a pill group next to the agent pill — the mode/device pill plus, in worktree
	   mode, the branch and base-branch pills. align-self keeps the row vertically
	   centered against the agent pill; the optional error wraps underneath (the
	   pill row wraps, so a full-width banner drops to its own line). */
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
		gap: 6px;
		max-width: 100%;
	}

	/* Mode dropdown pill — the composer island's `.pill` metrics (mockup) */
	.mode-selector {
		position: relative;
		flex-shrink: 0;
		min-width: 0;
	}

	.mode-trigger {
		display: flex;
		align-items: center;
		gap: 6px;
		height: 26px;
		padding: 0 9px;
		background: var(--scrim);
		border: 1px solid var(--border);
		border-radius: 999px;
		color: var(--muted-foreground);
		font-size: 12px;
		cursor: pointer;
		transition: border-color 0.13s ease, color 0.13s ease, background-color 0.13s ease;
		white-space: nowrap;
		max-width: 100%;
	}

	.mode-trigger:hover,
	.mode-trigger.open {
		border-color: var(--border-strong);
		color: var(--foreground);
	}

	.mode-trigger:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	.mode-trigger :global(svg) {
		flex-shrink: 0;
		color: var(--subtle-foreground);
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

	/* Cloud accordion: an inline section inside the mode menu (no flyout). */
	.cloud-section {
		padding-left: 12px;
		border-left: 1px solid var(--border);
		margin-left: 10px;
	}

	.cloud-item :global(svg.cloud-caret) {
		transition: transform 120ms ease;
	}

	.cloud-item :global(svg.cloud-caret.open) {
		transform: rotate(90deg);
	}

	/* Unavailable rows stay in the menu (so the reason is visible via title)
	   but read and behave as inert. */
	.branch-item.disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}

	.branch-item.disabled:hover {
		background: transparent;
	}

	/* The device is reachable but has no checkout of this project. The outline is
	   deliberately subtle because the row stays selectable in plain mode. */
	.device-nocheckout {
		font-size: 0.5625rem;
		font-weight: 500;
		padding: 1px 5px;
		border-radius: 4px;
		border: 1px solid color-mix(in oklch, var(--muted-foreground) 25%, transparent);
		color: var(--muted-foreground);
		opacity: 0.75;
	}

	/* The mode name is the pill's subject, so it takes the foreground weight the
	   mockup's `.pill .k` uses. */
	.mode-trigger-label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--foreground);
		font-weight: 500;
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
		gap: 5px;
		height: 26px;
		padding: 0 9px;
		background: var(--scrim);
		border: 1px solid var(--border);
		border-radius: 999px;
		color: var(--muted-foreground);
		font-size: 12px;
		cursor: pointer;
		transition: border-color 0.13s ease, color 0.13s ease, background-color 0.13s ease;
		max-width: 100%;
		min-width: 0;
	}

	.branch-trigger:hover {
		border-color: var(--border-strong);
		color: var(--foreground);
	}

	.branch-trigger:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	.branch-trigger :global(svg) {
		color: var(--subtle-foreground);
	}

	.branch-trigger.auto-mode {
		border-style: dashed;
		color: var(--muted-foreground);
		opacity: 0.8;
	}

	.branch-trigger.auto-mode:hover {
		opacity: 1;
	}

	/* A git ref reads as code — the mockup's `.branchtxt` treatment. */
	.branch-name {
		max-width: min(180px, 40vw);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--font-mono);
		font-size: 11px;
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

		.cloud-item :global(svg.cloud-caret) {
			transition: none;
		}
	}
</style>

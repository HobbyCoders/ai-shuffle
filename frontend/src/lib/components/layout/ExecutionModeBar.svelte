<script lang="ts">
	/**
	 * ExecutionModeBar - Local/Worktree selector with branch management
	 *
	 * Positioned below the chat input island. Shows a single dropdown
	 * pill for execution mode (the current mode's icon + label + chevron;
	 * clicking opens an upward menu with Local as the base mode and an
	 * orthogonal Worktree checkbox) with an inline branch selector when
	 * worktree mode is active. Includes "New Branch" creation modal. In Auto
	 * mode the branch chip reads "Auto from {base}" and the base-branch picker
	 * lives in that same dropdown.
	 *
	 * Uses the git store for worktree/branch data and syncWorktrees()
	 * to clean up stale DB records on project change. Worktree creation is
	 * deferred to send time (POST /sessions/with-worktree via message-sender);
	 * picking a branch here only stages it.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import { syncWorktrees, type Worktree, type Branch } from '$lib/api/git';
	import { listInstalls, getClusterGitState, type Install } from '$lib/api/installs';
	import {
		peerRunners,
		checkoutEligibility,
		isIneligible,
		isBlocked,
		runnerTitle,
		emptyRunnersCopy
	} from './execution-mode-eligibility';
	import { worktrees as worktreeStore, branches as branchStore, git, currentBranch as gitCurrentBranch } from '$lib/stores/git.svelte';
	import { ChevronDown, ChevronRight, Server, GitBranch, Monitor, GitFork, Plus, Loader2, Check } from 'lucide-svelte';
	import { untrack, type Snippet } from 'svelte';

	function keepInViewport(node: HTMLElement) {
		const reposition = () => {
			node.style.translate = '';
			const rect = node.getBoundingClientRect();
			const shift = rect.left < 12 ? 12 - rect.left : Math.min(0, window.innerWidth - 12 - rect.right);
			node.style.translate = `${shift}px 0`;
		};
		const observer = new ResizeObserver(reposition);
		observer.observe(node);
		window.addEventListener('resize', reposition);
		reposition();
		return { destroy() { observer.disconnect(); window.removeEventListener('resize', reposition); } };
	}
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import Banner from '$lib/components/Banner.svelte';

	interface Props {
		/** Stack the mode and worktree selectors as full-width mobile-sheet rows. */
		sheetMode?: boolean;
		/**
		 * Chips the parent wants rendered between the runner chip and the worktree
		 * chips (the repository chip and, in local mode, the checkout branch chip)
		 * so the row reads runner · repo · branch · worktree in DOM order.
		 */
		between?: Snippet;
	}

	let { sheetMode = false, between }: Props = $props();

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

	// --- Git-repo gating ---
	// Worktree mode and the branch selector only make sense for a git
	// repository. Reuse the existing git-status signal (loaded by MainPanel's git
	// store effect for the active project) rather than inventing a new one — the
	// same `is_git_repo !== false` convention GitHeaderActions uses, so non-git
	// controls only hide once the backend confirms the folder isn't a repo (during
	// the brief load window status is null and we optimistically show everything).
	// A cache miss retains the previous checkout's status while loading. Only
	// use hydrated data belonging to this composer to constrain its intent.
	const isGitRepo = $derived(!($git.hydrated
		&& ($git.projectId === $activeSession.projectV2Id || $git.projectId === $activeSession.project)
		&& $git.worktreeId === $activeSession.worktreeId
		&& $git.sessionId === $activeSession.sessionId
		&& $git.status?.is_git_repo === false));

	$effect(() => {
		// Non-git drafts run Local, but this is not a new account-wide preference.
		// Existing sessions retain their own checkout binding.
		if (!isGitRepo && !$activeSession.sessionId && $activeSession.worktreeIntent !== 'local') {
			activeSession.setWorktreeIntent('local', { persist: false });
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
	const branchChipLabel = $derived(isAutoMode ? `Auto from ${baseBranch}` : currentBranch);

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
		const project = $activeSession.projectV2Id ? null : $activeSession.project;
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
		// Local is the base mode; picking it drops any runner choice so the
		// session is created on (and owned by) this install.
		activeSession.setDeviceIntent(null);
		cloudExpanded = false;
		showModeDropdown = false;
	}

	function toggleWorktree() {
		activeSession.setWorktreeIntent(worktreeOn ? 'local' : 'worktree');
		// Leave the menu open so the user sees the checkbox flip and can keep going.
	}

	// --- Cloud (other runners) section ---
	//
	// An inline accordion INSIDE the mode menu, not a flyout submenu: this
	// codebase has no submenu primitive, and a nested floating panel would need
	// its own outside-click/placement handling on mobile. The list is `installs`
	// (full processes that can host a session), not `mobile_devices` (phones).
	let cloudExpanded = $state(false);
	let installs = $state<Install[]>([]);
	let installsLoading = $state(false);

	// Per-install checkout eligibility for the selected project (ruling D6),
	// keyed by install id. `null` = UNKNOWN — no project selected, the git-state
	// call failed, or the project has no cluster git state. Ruling W3-3 is
	// fail-open: unknown means no badges and no extra disabling, so a backend
	// hiccup can never lock the user out of their own runners.
	let eligibility = $state<Record<string, boolean> | null>(null);

	// Every other install, including offline. Local already represents "this
	// device"; greying (not hiding) offline peers is `isBlocked`.
	const peers = $derived(peerRunners(installs));

	const deviceIntent = $derived($activeSession.deviceIntent);

	// Worktree and runner compose freely (D7): picking a runner sends the
	// worktree request to that install, which materializes the checkout on its
	// own disk via the durable git-ops queue. Neither choice constrains the other.

	async function loadInstalls() {
		installsLoading = true;
		// Eligibility rides along with the registry refresh on every open so the
		// badges match the picked project. Errors are swallowed at the source so a
		// rejection can never surface while the installs call is still in flight.
		const projectId = $activeSession.project;
		const projectV2Id = $activeSession.projectV2Id;
		const gitStateReq = (projectId || projectV2Id)
			? getClusterGitState(projectV2Id ? null : projectId || null, projectV2Id || null).catch(e => {
				console.warn('[ExecutionModeBar] Failed to load git state:', e);
				return null;
			})
			: Promise.resolve(null);
		try {
			installs = (await listInstalls()).installs;
		} catch (e) {
			console.warn('[ExecutionModeBar] Failed to list runners:', e);
			installs = [];
		} finally {
			installsLoading = false;
		}
		const gitState = await gitStateReq;
		eligibility = checkoutEligibility(gitState);
	}

	function toggleCloud() {
		cloudExpanded = !cloudExpanded;
	}

	function selectDevice(install: Install) {
		if (isBlocked(install, worktreeOn, eligibility)) return;
		activeSession.setDeviceIntent({ id: install.id, name: install.device_name });
		showModeDropdown = false;
	}

	function selectWorktree(wt: Worktree) {
		if (wt.resource_kind === 'checkout') {
			// The worktree list contains only this runner's checkouts. Clear any
			// previous runner/legacy selection before staging the logical branch.
			activeSession.setDeviceIntent(null);
			activeSession.clearWorktreeSelection();
			activeSession.setWorktreeBranch(wt.branch_name);
			activeSession.setWorktreeMode('new');
			activeSession.setWorktreeCreateNewBranch(false);
		} else {
			activeSession.setDeviceIntent(null);
			activeSession.selectWorktreeBranch(wt.branch_name, wt.id);
		}
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
		if (showBranchDropdown && ($activeSession.projectV2Id || $activeSession.project)) {
			refreshWorktrees();
		}
	}

	function selectBaseBranch(branch: Branch) {
		const name = branch.remote ? branch.name.replace(/^[^\/]+\//, '') : branch.name;
		activeSession.setWorktreeBaseBranch(name);
		showBranchDropdown = false;
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

<div class="execution-mode-bar" class:sheet-mode={sheetMode}>
	<div class="mode-bar-row">
		<!-- Runner chip: this device or a cloud runner. Opens an upward menu with
		     Local as a radio plus the Cloud accordion. The worktree modifier is
		     its own checkbox chip further along the row. -->
		<div class="mode-selector" bind:this={modeDropdownRef}>
			<button
				type="button"
				class="ctx-chip mode-trigger"
				class:open={showModeDropdown}
				onclick={toggleModeDropdown}
				title={deviceIntent ? `Run this session on ${deviceIntent.name}` : 'Run this session on this device'}
				aria-label="Runner: {deviceIntent ? deviceIntent.name : 'Local'}"
				aria-haspopup="menu"
				aria-expanded={showModeDropdown}
			>
				{#if deviceIntent}
					<Server size={12} strokeWidth={1.6} />
				{:else}
					<Monitor size={12} strokeWidth={1.6} />
				{/if}
				<span class="ctx-chip-label mode-trigger-label">{deviceIntent ? deviceIntent.name : 'Local'}</span>
				{#if sheetMode}
					<ChevronDown size={12} strokeWidth={1.5} />
				{/if}
			</button>

			{#if showModeDropdown}
				<div use:keepInViewport class="mode-dropdown branch-dropdown" role="menu" aria-label="Runner selection">
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

					<!-- Cloud: inline accordion listing the other runners. Creating
					     the session there stamps it with that runner's ownership. -->
					<button
						type="button"
						class="branch-item cloud-item"
						onclick={toggleCloud}
						title="Run this session on another runner"
						aria-expanded={cloudExpanded}
					>
						<Server size={13} strokeWidth={1.5} />
						<span class="branch-item-name">Other workers</span>
						<ChevronRight size={12} strokeWidth={1.5} class={cloudExpanded ? 'cloud-caret open' : 'cloud-caret'} />
					</button>

					{#if cloudExpanded}
						<div class="cloud-section">
							{#if installsLoading}
								<div class="branch-loading">
									<Loader2 size={14} class="animate-spin" />
									<span>Loading...</span>
								</div>
							{:else if peers.length === 0}
								<p class="branch-empty">{emptyRunnersCopy}</p>
							{:else}
								{#each peers as install (install.id)}
									<button
										type="button"
										class="branch-item device-item"
										class:active={deviceIntent?.id === install.id}
										class:disabled={isBlocked(install, worktreeOn, eligibility)}
										onclick={() => selectDevice(install)}
										title={runnerTitle(install, worktreeOn, eligibility)}
										role="menuitemradio"
										aria-checked={deviceIntent?.id === install.id}
										aria-disabled={isBlocked(install, worktreeOn, eligibility)}
									>
										<Server size={13} strokeWidth={1.5} />
										<span class="branch-item-name">{install.device_name}</span>
										{#if !install.online}
											<span class="device-nocheckout">offline</span>
										{:else if isIneligible(eligibility, install.id)}
											<span class="device-nocheckout">no checkout</span>
										{/if}
									</button>
								{/each}
							{/if}
						</div>
					{/if}
				</div>
			{/if}
		</div>

		<!-- Repository chip and, in local mode, the checkout branch chip — owned
		     by the parent row so they can sit between the runner and the worktree
		     chips. -->
		<div class="branch-wt-row">
		{@render between?.()}

		<!-- Branch selector (shown when worktree mode active) -->
		{#if $activeSession.worktreeIntent === 'worktree'}
			<div class="branch-selector" bind:this={dropdownRef}>
			<button
				type="button"
				class="ctx-chip branch-trigger"
				class:auto-mode={isAutoMode}
				class:open={showBranchDropdown}
				onclick={toggleDropdown}
				aria-label="Worktree branch: {branchChipLabel}"
				aria-haspopup="menu"
				aria-expanded={showBranchDropdown}
				title={isAutoMode ? `Auto from ${baseBranch}. Branch will be auto-generated from your message.` : `Current branch: ${currentBranch}`}
			>
				<GitBranch size={12} strokeWidth={1.6} />
				<span class="ctx-chip-label branch-name">{branchChipLabel}</span>
				{#if sheetMode}
					<ChevronDown size={12} strokeWidth={1.5} />
				{/if}
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

						{#if isAutoMode}
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
							<div class="branch-divider"></div>
						{/if}

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
									{#if wt.status_error}
										<span class="broken-badge" title={`Git status unavailable: ${wt.status_error}`}>!</span>
									{:else if wt.git_status && !wt.git_status.is_clean}
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

		<!-- Worktree modifier: a checkbox chip. Orthogonal to the runner choice
		     (D7) and only offered for a git repository. -->
		{#if isGitRepo}
			<label class="ctx-chip wt-chip" title="Run in an isolated git worktree.">
				<input type="checkbox" checked={worktreeOn} onchange={toggleWorktree} />
				<span class="ctx-chip-cb" class:checked={worktreeOn} aria-hidden="true">
					<Check size={9} strokeWidth={2.6} />
				</span>
				<span class="ctx-chip-label">worktree</span>
			</label>
		{/if}
		</div>
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
	onClose={() => showCreateModal = false}
	onCreated={handleBranchCreated}
/>

<style>
	/* There is no standalone bar any more: this component renders inline in the
	   ContextPillBar, which is now the composer island's top pill row. It flows as
	   a pill group next to the agent pill — the mode/device pill plus, in worktree
	   mode, the merged Auto/branch chip. align-self keeps the row vertically
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

	.execution-mode-bar.sheet-mode {
		display: contents;
		z-index: auto;
		width: 100%;
		align-items: stretch;
		align-self: stretch;
	}

	.sheet-mode .mode-bar-row {
		display: contents;
		width: 100%;
		flex-direction: column;
		align-items: stretch;
		gap: 8px;
	}

	.branch-wt-row {
		display: contents;
	}

	.sheet-mode .branch-wt-row {
		grid-column: 1 / -1;
		display: grid;
		/* The repository snippet spans both columns; branch and worktree share
		   the next row without competing with a full-width repository chip. */
		grid-template-columns: minmax(0, 1fr) auto;
		align-items: start;
		gap: 8px;
		min-width: 0;
		width: 100%;
	}

	.sheet-mode .mode-selector {
		position: relative;
		width: 100%;
	}

	.sheet-mode .branch-wt-row .branch-selector,
	.sheet-mode .branch-wt-row :global(.branch-selector) {
		position: relative;
		grid-column: 1;
		min-width: 0;
		width: auto;
	}

	.sheet-mode .mode-trigger,
	.sheet-mode .branch-trigger {
		width: 100%;
		height: 44px;
		padding: 0 12px;
		gap: 8px;
		border: 1px solid var(--border-subtle);
		border-radius: var(--composer-control-radius, 12px);
		background: var(--composer-control-bg, color-mix(in oklch, var(--muted) 55%, var(--card)));
		font-size: 0.8rem;
	}

	.sheet-mode .wt-chip {
		grid-column: 2;
		width: auto;
		height: 44px;
		padding: 0 12px;
		gap: 8px;
		border: 1px solid var(--border-subtle);
		border-radius: var(--composer-control-radius, 12px);
		background: var(--composer-control-bg, color-mix(in oklch, var(--muted) 55%, var(--card)));
		font-size: 0.8rem;
	}

	.sheet-mode .mode-trigger-label,
	.sheet-mode .branch-name {
		flex: 1;
		max-width: none;
		text-align: left;
	}

	.sheet-mode .branch-dropdown,
	.sheet-mode .mode-dropdown {
		position: static;
		width: 100%;
		min-width: 0;
		max-width: none;
		max-height: min(180px, calc(var(--app-viewport-height, 100dvh) * 0.28));
		touch-action: pan-y;
		overscroll-behavior: contain;
		margin-top: 6px;
	}

	.mode-bar-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		max-width: 100%;
	}

	/* Runner chip — `.ctx-chip` (app.css) carries the look. */
	.mode-selector {
		position: relative;
		flex-shrink: 0;
		min-width: 0;
	}

	.mode-trigger :global(svg) {
		flex-shrink: 0;
		color: var(--subtle-foreground);
	}

	/* The worktree checkbox chip's tick must beat the generic muted svg colour. */
	.wt-chip :global(.ctx-chip-cb svg) {
		color: var(--primary-foreground);
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

	.mode-trigger-label {
		max-width: min(160px, 40vw);
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

	.branch-trigger :global(svg) {
		flex-shrink: 0;
		color: var(--subtle-foreground);
	}

	/* "Auto": no branch staged yet — the name is generated from the first
	   message, so the chip reads as a placeholder rather than a choice. */
	.branch-trigger.auto-mode .branch-name {
		font-style: italic;
	}

	.branch-name {
		max-width: min(180px, 40vw);
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

	.broken-badge {
		font-size: 0.625rem;
		font-weight: 600;
		color: var(--destructive);
		padding: 1px 4px;
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
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

<script lang="ts">
	/**
	 * ExecutionModeBar - Local/Worktree selector with branch management
	 *
	 * Positioned below the chat input island. Shows a single dropdown
	 * pill for execution mode (the current mode's icon + label + chevron;
	 * clicking opens an upward flat list of workers, Local first) and an
	 * orthogonal worktree checkbox chip, with an inline branch selector when
	 * worktree mode is active. Includes "New Branch" creation modal. In Auto
	 * mode the branch chip reads "Auto from {base}" and the base-branch picker
	 * lives in that same dropdown.
	 *
	 * Uses read-only git-store worktree/branch data. Stale bindings stay visible
	 * for diagnosis; opening the picker never deletes registry rows or branches. Worktree creation is
	 * deferred to send time (POST /sessions/with-worktree via message-sender);
	 * picking a branch here only stages it.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import { type Worktree, type Branch } from '$lib/api/git';
	import { getClusterGitState, type Install } from '$lib/api/installs';
	import { workerInstalls, refreshWorkerPresence, watchWorkerPresence, presenceOf } from '$lib/stores/worker-presence';
	import {
		peerRunners,
		checkoutEligibility,
		isBlocked,
		runnerTitle
	} from './execution-mode-eligibility';
	import { workerRowNote } from '$lib/components/chat/runner-badge-menu';
	import WorkerStatusDot from '$lib/components/ui/WorkerStatusDot.svelte';
	import WorkerMenuList, { type WorkerMenuRow } from '$lib/components/ui/WorkerMenuList.svelte';

	// Local's row id; install ids are UUID-like, so this never collides.
	const LOCAL_ROW_ID = '__local__';
	import { worktrees as worktreeStore, branches as branchStore, git, currentBranch as gitCurrentBranch } from '$lib/stores/git.svelte';
	import { ChevronDown, Server, GitBranch, Monitor, GitFork, Plus, Loader2, Check } from 'lucide-svelte';
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
	let forgetConfirm = $state<string | null>(null);
	let forgetting = $state<string | null>(null);
	let forgetError = $state<string | null>(null);

	// The Forget confirm opens with Cancel focused, so a stray Enter or Space
	// backs out instead of confirming (#2072).
	function focusOnMount(node: HTMLElement) {
		node.focus();
	}

	async function forgetWorktree(wt: Worktree) {
		forgetting = wt.id;
		forgetError = null;
		try {
			await git.forgetWorktree(wt.id);
			forgetConfirm = null;
		} catch (error) {
			const e = error as { detail?: string; message?: string };
			forgetError = e.detail || e.message || 'Could not forget this worktree';
		} finally {
			forgetting = null;
		}
	}

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
	// Usable bindings are selectable; stale rows remain visible for diagnosis.
	const filteredWorktrees = $derived($worktreeStore.filter(w => w.exists !== false && !w.binding_warning && !w.status_error));
	const staleWorktrees = $derived($worktreeStore.filter(w => w.exists === false || w.binding_warning || w.status_error));

	// Branches that don't already have a worktree
	const worktreeBranchNames = $derived(new Set($worktreeStore.map(w => w.branch_name)));

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
		showModeDropdown = false;
	}

	function toggleWorktree() {
		activeSession.setWorktreeIntent(worktreeOn ? 'local' : 'worktree');
		// Leave the menu open so the user sees the checkbox flip and can keep going.
	}

	// --- Runner list ---
	//
	// One flat list, matching the active session's runner menu: this worker
	// ("Local") first, then every other install. The list is `installs` (full
	// processes that can host a session), not `mobile_devices` (phones).
	let installsLoading = $state(false);
	// The last open's load failed: list nothing, as before, until a later load
	// succeeds.
	let installsFailed = $state(false);
	// Live presence (shared store, refreshed while watched below), so a worker
	// that goes offline while the menu is open turns red and blocked in place.
	const installs = $derived<Install[]>(installsFailed ? [] : ($workerInstalls ?? []));

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

	// Presence of the picked runner for the chip's dot. Local is this worker and
	// always online; a picked peer not in the loaded list has unknown presence,
	// so the chip shows no dot.
	const deviceIntentOnline = $derived.by((): boolean | null => {
		if (!deviceIntent) return true;
		return presenceOf(installs, [deviceIntent.id]);
	});

	// Keep presence fresh while it is on screen: the open menu, or the chip of
	// a picked peer (which shows its dot with the menu closed).
	const watchPresence = $derived(showModeDropdown || !!deviceIntent);
	$effect(() => {
		if (!watchPresence) return;
		return watchWorkerPresence();
	});

	// A later successful refresh lifts a failed open's empty list.
	$effect(() => {
		if ($workerInstalls) untrack(() => { installsFailed = false; });
	});

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
			await refreshWorkerPresence();
			installsFailed = false;
		} catch (e) {
			console.warn('[ExecutionModeBar] Failed to list runners:', e);
			installsFailed = true;
		} finally {
			installsLoading = false;
		}
		const gitState = await gitStateReq;
		eligibility = checkoutEligibility(gitState);
	}

	// Rows for the shared worker list. Local is this worker: always online,
	// never blocked. Peers keep their blocked state and tooltip.
	const runnerRows = $derived<WorkerMenuRow[]>([
		{
			id: LOCAL_ROW_ID,
			label: 'Local',
			online: true,
			disabled: false,
			title: 'Run this session on this device',
			checked: !deviceIntent
		},
		...peers.map((install): WorkerMenuRow => ({
			id: install.id,
			label: install.device_name,
			online: install.online,
			disabled: isBlocked(install, worktreeOn, eligibility),
			title: runnerTitle(install, worktreeOn, eligibility),
			note: workerRowNote(install, eligibility),
			checked: deviceIntent?.id === install.id
		}))
	]);

	function selectRunnerRow(row: WorkerMenuRow) {
		if (row.id === LOCAL_ROW_ID) return selectBaseMode();
		const install = peers.find((i) => i.id === row.id);
		if (install) selectDevice(install);
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
		<!-- Runner chip: this device or another worker. Opens an upward menu
		     listing every worker as a radio row (WorkerMenuList, shared with the
		     active session's runner menu). The worktree modifier is its own
		     checkbox chip further along the row. -->
		<div class="mode-selector" bind:this={modeDropdownRef}>
			<button
				type="button"
				class="ctx-chip mode-trigger"
				class:open={showModeDropdown}
				onclick={toggleModeDropdown}
				title={deviceIntent ? `Run this session on ${deviceIntent.name}` : 'Run this session on this device'}
				aria-label="Runner: {deviceIntent ? deviceIntent.name : 'Local'}{deviceIntentOnline === false ? ' (offline)' : ''}"
				aria-haspopup="menu"
				aria-expanded={showModeDropdown}
			>
				{#if deviceIntent}
					<Server size={12} strokeWidth={1.6} />
				{:else}
					<Monitor size={12} strokeWidth={1.6} />
				{/if}
				{#if deviceIntentOnline !== null}
					<WorkerStatusDot online={deviceIntentOnline} />
				{/if}
				<span class="ctx-chip-label mode-trigger-label">{deviceIntent ? deviceIntent.name : 'Local'}</span>
				{#if sheetMode}
					<ChevronDown size={12} strokeWidth={1.5} />
				{/if}
			</button>

			{#if showModeDropdown}
				<!-- One flat list of workers, the same rows the active session's
				     runner menu shows: this worker first, then every peer. Picking
				     a peer stamps the new session with that runner's ownership. -->
				<div use:keepInViewport class="mode-dropdown branch-dropdown" role="menu" aria-label="Runner selection">
					<WorkerMenuList
						items={runnerRows}
						role="menuitemradio"
						loading={installsLoading && peers.length === 0 && !installsFailed}
						error={installsFailed ? 'Could not load workers. Try again.' : null}
						onRetry={loadInstalls}
						emptyText={!installsLoading && peers.length === 0 ? 'No other workers available.' : null}
						onSelect={selectRunnerRow}
					/>
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

						{#if staleWorktrees.length > 0}
							<div class="branch-divider"></div>
							<div class="branch-section-label">Needs cleanup / binding review</div>
							{#each staleWorktrees as wt (wt.id)}
								<div class="stale-worktree">
									<button type="button" class="branch-item" disabled>
										<GitBranch size={13} strokeWidth={1.5} />
										<span class="branch-item-name">{wt.branch_name}</span>
										<span class="broken-badge">{wt.binding_warning ? 'Branch changed' : wt.exists === false ? 'Missing / invalid' : 'Status unavailable'}</span>
									</button>
									{#if wt.binding_warning && wt.git_status}
										<p class="stale-detail">On {wt.git_status.current_branch ?? 'detached HEAD'} · {wt.git_status.modified_count + wt.git_status.staged_count + wt.git_status.untracked_count} changed</p>
									{/if}
									<p class="stale-detail">{wt.binding_warning || wt.status_error || 'Directory missing; needs cleanup. Existing files are preserved.'}</p>
									{#if !$git.workerTarget && !wt.binding_warning && (wt.exists === false || wt.status_error?.includes('not its own Git working tree'))}
										{#if forgetConfirm === wt.id}
											<p class="stale-detail">Forget this registration? This removes Git's record of this worktree and its entry in AI Shuffle. Checkout files, branches and chat history are kept.</p>
											<button type="button" class="branch-item" disabled={forgetting !== null} onclick={() => forgetWorktree(wt)}>{forgetting === wt.id ? 'Forgetting…' : 'Confirm forget'}</button>
											<button type="button" class="branch-item" use:focusOnMount disabled={forgetting !== null} onclick={(event) => { event.stopPropagation(); forgetConfirm = null; forgetError = null; }}>Cancel</button>
											{#if forgetError}<p class="stale-detail" role="alert">{forgetError}</p>{/if}
										{:else}
											<button type="button" class="branch-item" disabled={forgetting !== null} onclick={(event) => { event.stopPropagation(); forgetConfirm = wt.id; forgetError = null; }} aria-label={`Forget ${wt.branch_name}`}>Forget</button>
										{/if}
									{/if}
								</div>
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
						{#if filteredWorktrees.length === 0 && staleWorktrees.length === 0 && filteredBranches.length === 0}
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

	.mode-trigger-label {
		max-width: min(160px, 40vw);
	}

	/* Mode dropdown — reuses .branch-dropdown visuals; anchored to the pill's left
	   edge (it's the leftmost pill) and opens upward like the branch dropdown. */
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

	/* Runner menu: the same surface as the active session's runner menu
	   (ContextPillBar `.runner-dropdown`), so the two pickers read as one
	   control. It must come AFTER `.branch-dropdown`: the menu carries both
	   classes at equal specificity, so source order decides. The sheet-mode
	   and phone rules further down still win (higher specificity / later). */
	.mode-dropdown {
		left: 0;
		right: auto;
		min-width: 220px;
		max-width: min(320px, calc(100vw - 24px));
		max-height: 320px;
		background: var(--popover);
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
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

	.branch-item:hover:not(:disabled) {
		background: var(--hover-overlay);
	}

	.branch-item:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.branch-item:active:not(:disabled) { background: var(--selection-bg); }
	.branch-item:disabled { cursor: not-allowed; opacity: 0.8; }
	.stale-worktree { border-left: 2px solid var(--warning); margin: 6px 4px; }
	.stale-detail {
		margin: 0;
		padding: 0 10px 8px;
		font-size: 0.6875rem;
		line-height: 1.5;
		color: var(--muted-foreground);
		overflow-wrap: anywhere;
	}
	@media (max-width: 480px) { .branch-item { min-height: 44px; } }
	@media (prefers-reduced-motion: reduce) { .branch-dropdown { animation: none; } }

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

		/* Runner menu: keep it anchored to its OWN trigger (.mode-selector stays
		   position: relative), not the shrink-wrapped .execution-mode-bar the
		   shared .branch-dropdown rule above uses — that collapsed it to a
		   sliver. Same anchoring and width as the active session's runner menu
		   (left edge of the chip, 220px floor); keepInViewport shifts it back on
		   screen if it would overflow. */
		.mode-dropdown {
			left: 0;
			right: auto;
			width: auto;
			min-width: 220px;
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

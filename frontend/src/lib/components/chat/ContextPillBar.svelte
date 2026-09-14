<script lang="ts">
	/**
	 * ContextPillBar - the loose row of context chips that floats above the
	 * chat composer box: agent · | · runner · repository · branch · worktree.
	 *
	 * New session (no messages yet): every chip is a selector, because the
	 * execution context (runner / repository / branch / worktree) has to be
	 * chosen before the first send. The runner and worktree chips come from
	 * ExecutionModeBar; the repository chip and the local-mode branch chip are
	 * rendered here and handed to it as its `between` snippet so the DOM order
	 * matches the visual order. GitHub auth gates the runner/branch/worktree
	 * controls, as before.
	 *
	 * Active chat: the same row, now describing the session's context. The
	 * agent chip stays a selector and the branch chip stays the checkout
	 * selector where it was available before; the runner chip is read-only
	 * unless the session runs on another install, in which case it keeps the
	 * "Move this session" menu (re-pin / continue-here / unpin). A degraded
	 * session (queued work, no runner online) shows a read-only warning chip.
	 *
	 * `sheetMode` renders the same selectors as stacked rows inside the mobile
	 * controls sheet. The chip dropdowns open upward since this row sits just
	 * above the input.
	 */
	import { onDestroy } from 'svelte';
	import ProfileSelector from './ProfileSelector.svelte';
	import WorkspaceFolderSelector from './WorkspaceFolderSelector.svelte';
	import BranchSelector from './BranchSelector.svelte';
	import ExecutionModeBar from '$lib/components/layout/ExecutionModeBar.svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import { computeContextBadge, shouldShowBranchPill, shouldShowEmptyStateBranchPill } from './contextPills';
	import { gitStateQuery, runnerBadgeMenuItems } from './runner-badge-menu';
	import type { RunnerBadgeMenuItem } from './runner-badge-menu';
	import { activeSession } from '$lib/stores/active-session';
	import { clusterProjects, projects, sessions, appData } from '$lib/stores/app-data.svelte';
	import { githubAuthenticated } from '$lib/stores/auth.svelte';
	import { git, gitStatus } from '$lib/stores/git.svelte';
	import { openHeaderDropdown, openDropdown, closeDropdown } from '$lib/stores/header-dropdowns';
	import { listInstalls, getClusterGitState, type Install } from '$lib/api/installs';
	import { adoptSession, moveSession } from '$lib/api/sessions';
	import { notify } from '$lib/stores/notifications';
	import { GitBranch, Monitor, MonitorOff, Check, Loader2 } from 'lucide-svelte';
	import { checkoutEligibility, type EligibilityMap } from '$lib/components/layout/execution-mode-eligibility';

	interface Props {
		/** Render the selectors as stacked, full-width controls inside the mobile sheet. */
		sheetMode?: boolean;
	}

	let { sheetMode = false }: Props = $props();

	const projectName = $derived(
		$projects.find((p) => p.id === $activeSession.project)?.name ??
			$clusterProjects.find((p) => p.project_v2_id === $activeSession.projectV2Id)?.name ??
			null
	);

	// The sessions-board summary row for the active session — source of the
	// server-derived ownership/health fields (degraded, is_remote, device name +
	// online) the read-only context chips read.
	const sessionRow = $derived(
		$sessions.find((s) => s.id === $activeSession.sessionId) ?? null
	);

	// V2 Q7: the active session's server-derived degraded flag (queued work, no
	// runner online).
	const degraded = $derived(sessionRow?.degraded ?? false);

	// New-session state: no messages yet. Every chip is a selector here.
	const isEmptyState = $derived($activeSession.messages.length === 0);

	const isWorktreeSession = $derived(!!($activeSession.worktreeId || $activeSession.worktreeBranch));
	// Auto naming starts before the session has a worktree ID or branch.
	const isWorktreeSelected = $derived(isWorktreeSession || $activeSession.isCreatingWorktree);

	// Interactive branch chip in an active chat. Remote / degraded sessions show
	// read-only context instead.
	const showBranch = $derived(
		shouldShowBranchPill({
			messageCount: $activeSession.messages.length,
			project: $activeSession.project || null,
			githubAuthenticated: $githubAuthenticated,
			isRemote: sessionRow?.is_remote ?? false,
			degraded
		})
	);

	// New-session screen: show the checkout-branch chip in LOCAL mode, where the
	// ExecutionModeBar has no branch picker of its own. In worktree mode that bar
	// owns branch selection (which branch to base the worktree on), so we don't
	// add a second branch control. See contextPills.shouldShowEmptyStateBranchPill.
	const showEmptyStateBranch = $derived(
		shouldShowEmptyStateBranchPill({
			messageCount: $activeSession.messages.length,
			project: $activeSession.project || null,
			githubAuthenticated: $githubAuthenticated,
			worktreeIntent: $activeSession.worktreeIntent,
			// Reuse the git store's repo signal — a non-git project has no branches.
			isGitRepo: $gitStatus?.is_git_repo !== false
		})
	);

	// Read-only context (runner / degraded / unauthenticated branch) for an
	// active chat — see contextPills.computeContextBadge for the gating.
	const badge = $derived(
		computeContextBadge({
			messageCount: $activeSession.messages.length,
			projectName,
			isWorktree: isWorktreeSession,
			selectedWorktreeBranch: $activeSession.selectedWorktreeBranch,
			worktreeBranch: $activeSession.worktreeBranch,
			githubAuthenticated: $githubAuthenticated,
			degraded,
			isRemote: sessionRow?.is_remote ?? false,
			deviceName: sessionRow?.device_name ?? null,
			deviceOnline: sessionRow?.device_online ?? false
		})
	);

	// Branch shown read-only in an active chat when the interactive chip is not
	// available: the worktree's branch, else the checkout's current branch.
	const readOnlyBranch = $derived.by(() => {
		if (badge?.branch) return badge.branch;
		if (isWorktreeSession) {
			return $activeSession.selectedWorktreeBranch || $activeSession.worktreeBranch || null;
		}
		if ($activeSession.project) return $gitStatus?.current_branch ?? null;
		return null;
	});

	let showCreateBranchModal = $state(false);

	const showRunnerMenu = $derived($openHeaderDropdown === 'runner');
	let runnerMenuRef = $state<HTMLDivElement | null>(null);
	let runnerInstalls = $state<Install[]>([]);
	let runnerEligibility = $state<EligibilityMap>(null);
	let runnerMenuLoading = $state(false);
	let runnerActionPending = $state(false);

	const runnerMenu = $derived(
		runnerBadgeMenuItems({
			ownerId: sessionRow?.instance_id ?? null,
			installs: runnerInstalls,
			eligibility: runnerEligibility,
			hasProject: !!(sessionRow?.project_id || sessionRow?.project_v2_id),
			isWorktree: isWorktreeSession
		})
	);

	async function loadRunnerMenu() {
		runnerMenuLoading = true;
		const q = gitStateQuery({
			project_id: sessionRow?.project_id,
			project_v2_id: sessionRow?.project_v2_id
		});
		const gitStateReq = q
			? ('project_id' in q
					? getClusterGitState(q.project_id)
					: getClusterGitState(null, q.project_v2_id)
				).catch((e) => {
					console.warn('[ContextPillBar] Failed to load git state:', e);
					return null;
				})
			: Promise.resolve(null);
		try {
			runnerInstalls = (await listInstalls()).installs;
		} catch (e) {
			console.warn('[ContextPillBar] Failed to list runners:', e);
			runnerInstalls = [];
		}
		const gitState = await gitStateReq;
		runnerEligibility = checkoutEligibility(gitState);
		runnerMenuLoading = false;
	}

	function toggleRunnerMenu() {
		if (showRunnerMenu) {
			closeDropdown('runner');
			return;
		}
		openDropdown('runner');
		void loadRunnerMenu();
	}

	async function runMenuAction(item: RunnerBadgeMenuItem) {
		if (!item.action || item.disabled || runnerActionPending) return;
		const sessionId = $activeSession.sessionId;
		if (!sessionId) return;
		runnerActionPending = true;
		try {
			if (item.action.kind === 'adopt') {
				await adoptSession(sessionId);

			} else {
				await moveSession(sessionId, item.action.targetInstanceId);
			}
			closeDropdown('runner');
			await appData.loadSessions();
		} catch (e) {
			const detail =
				e && typeof e === 'object' && 'detail' in e && typeof (e as { detail: unknown }).detail === 'string'
					? (e as { detail: string }).detail
					: 'Could not change this session\'s runner';
			notify.error(detail);
		} finally {
			runnerActionPending = false;
		}
	}

	function handleRunnerClickOutside(e: MouseEvent) {
		if (showRunnerMenu && runnerMenuRef && !runnerMenuRef.contains(e.target as Node)) {
			closeDropdown('runner');
		}
	}

	function handleRunnerKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && showRunnerMenu) closeDropdown('runner');
	}

	onDestroy(() => closeDropdown('runner'));

	// The voice-call start affordance no longer lives here (ruling VM19): it is
	// an action button in the composer's controls row (VoiceCallButton).
</script>

<svelte:window onclick={handleRunnerClickOutside} onkeydown={handleRunnerKeydown} />

{#snippet repoChip()}
	{#if projectName}
		<WorkspaceFolderSelector {projectName} {sheetMode} />
	{/if}
{/snippet}

<div class="context-pill-bar" class:sheet-mode={sheetMode} role="toolbar" aria-label="Session context">
	<ProfileSelector {sheetMode} />
	<span class="ctx-sep" aria-hidden="true"></span>

	{#if isEmptyState}
		{#if $githubAuthenticated}
			<ExecutionModeBar {sheetMode}>
				{#snippet between()}
					{@render repoChip()}
					{#if showEmptyStateBranch}
						<BranchSelector
							{sheetMode}
							projectName={sheetMode ? projectName : null}
							onClickNewBranch={() => (showCreateBranchModal = true)}
						/>
					{/if}
				{/snippet}
			</ExecutionModeBar>
		{:else}
			{@render repoChip()}
		{/if}
	{:else}
		<!-- Runner: read-only "Local", the remote runner with its move menu, or
		     the degraded warning. -->
		{#if badge?.degraded}
			<span class="ctx-chip readonly degraded" title={badge.title}>
				<MonitorOff size={12} strokeWidth={1.6} aria-hidden="true" />
				<span class="ctx-chip-label">Read-only — no runner online</span>
			</span>
		{:else if badge?.runner}
			<div class="runner-badge-wrap" bind:this={runnerMenuRef}>
				<button
					type="button"
					class="ctx-chip runner"
					class:open={showRunnerMenu}
					title={badge.title}
					aria-label="Runner: {badge.runner.deviceName}{badge.runner.online ? '' : ' (offline)'}"
					aria-haspopup="menu"
					aria-expanded={showRunnerMenu}
					onclick={toggleRunnerMenu}
				>
					{#if badge.runner.online}
						<Monitor size={12} strokeWidth={1.6} aria-hidden="true" />
					{:else}
						<MonitorOff size={12} strokeWidth={1.6} aria-hidden="true" />
					{/if}
					<span class="ctx-chip-label">{badge.runner.deviceName}</span>
				</button>
				{#if showRunnerMenu}
					<div class="runner-dropdown" role="menu" aria-label="Move this session">
						{#if runnerMenuLoading}
							<div class="runner-loading">
								<Loader2 size={14} strokeWidth={1.5} class="spin" />
								<span>Loading…</span>
							</div>
						{:else}
							{#each runnerMenu as item (item.id)}
								<button
									type="button"
									class="runner-item"
									class:disabled={item.disabled}
									role="menuitem"
									disabled={item.disabled || runnerActionPending}
									title={item.title}
									onclick={() => runMenuAction(item)}
								>
									<span class="ctx-chip-label">{item.label}</span>
								</button>
							{/each}
						{/if}
					</div>
				{/if}
			</div>
		{:else}
			<span class="ctx-chip readonly" title="Runs on this device">
				<Monitor size={12} strokeWidth={1.6} aria-hidden="true" />
				<span class="ctx-chip-label">Local</span>
			</span>
		{/if}

		{@render repoChip()}

		<div class="branch-wt-row">
		{#if showBranch}
			<BranchSelector
				{sheetMode}
				projectName={sheetMode ? projectName : null}
				onClickNewBranch={() => (showCreateBranchModal = true)}
			/>
		{:else if readOnlyBranch}
			<span class="ctx-chip readonly" title="Branch: {readOnlyBranch}">
				<GitBranch size={12} strokeWidth={1.6} aria-hidden="true" />
				<span class="ctx-chip-label branch-name">{readOnlyBranch}</span>
			</span>
		{/if}

		{#if $activeSession.project && !badge?.degraded}
			<span
				class="ctx-chip readonly wt-indicator"
				title={isWorktreeSelected ? 'Runs in an isolated git worktree' : 'Runs in the main checkout'}
			>
				<span class="ctx-chip-cb" class:checked={isWorktreeSelected} aria-hidden="true">
					<Check size={9} strokeWidth={2.6} />
				</span>
				<span class="ctx-chip-label">worktree</span>
			</span>
		{/if}
		</div>
	{/if}
</div>

{#if $activeSession.project}
	<CreateBranchModal
		open={showCreateBranchModal}
		projectId={$activeSession.project}
		onClose={() => (showCreateBranchModal = false)}
		onCreated={() => git.refreshStatus()}
	/>
{/if}

<style>
	/* The chip row floats above the composer box with a visible gap (mockup
	   `.ctx`). Its dropdowns open upward; the stacking level keeps them below
	   the input area's @/slash autocomplete panels. */
	.context-pill-bar {
		position: relative;
		z-index: 10;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		margin: 0 0 10px 2px;
		min-width: 0;
	}

	.context-pill-bar.sheet-mode {
		z-index: auto;
		width: 100%;
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		align-items: stretch;
		gap: 6px;
		margin: 0;
	}

	.sheet-mode .ctx-sep {
		display: none;
	}

	.runner-badge-wrap {
		position: relative;
		min-width: 0;
	}

	/* Read-only chips describe the context without inviting a click. */
	.ctx-chip.readonly {
		cursor: default;
	}

	.branch-name {
		max-width: min(180px, 38vw);
	}

	.branch-wt-row {
		display: contents;
	}

	.sheet-mode .branch-wt-row {
		grid-column: 1 / -1;
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
	}

	.sheet-mode .ctx-chip,
	.sheet-mode .runner-badge-wrap {
		width: 100%;
	}

	.sheet-mode .branch-wt-row :global(.branch-selector) {
		flex: 1 1 0;
		min-width: 0;
		width: auto;
	}

	.sheet-mode .branch-wt-row .ctx-chip.readonly:not(.wt-indicator) {
		flex: 1 1 0;
		min-width: 0;
		width: auto;
	}

	.sheet-mode .wt-indicator {
		width: auto;
		flex: 0 0 auto;
	}

	.sheet-mode .ctx-chip {
		height: 44px;
		padding: 0 12px;
		gap: 8px;
		border: 1px solid var(--border-subtle);
		border-radius: var(--composer-control-radius, 12px);
		background: var(--composer-control-bg, color-mix(in oklch, var(--muted) 55%, var(--card)));
		font-size: 0.8rem;
	}

	.sheet-mode .runner-dropdown {
		position: static;
		width: 100%;
		max-width: none;
		margin-top: 6px;
		max-height: 180px;
		touch-action: pan-y;
	}

	.runner-dropdown {
		position: absolute;
		bottom: calc(100% + 6px);
		left: 0;
		min-width: 220px;
		max-width: min(320px, calc(100vw - 24px));
		max-height: 320px;
		overflow-y: auto;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
		box-shadow: var(--shadow-l);
		padding: 4px;
		z-index: 100;
	}

	.runner-loading {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 10px;
		color: var(--muted-foreground);
		font-size: 0.75rem;
	}

	.runner-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 7px 10px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.58);
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		text-align: left;
	}

	.runner-item:hover:not(:disabled) {
		background: var(--hover-overlay);
	}

	.runner-item.disabled,
	.runner-item:disabled {
		opacity: 0.45;
		cursor: default;
	}

	:global(.spin) {
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	/* Remote-placed session: a primary tint so the runner chip is
	   distinguishable from the local one. */
	.ctx-chip.runner {
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, var(--muted));
	}

	.ctx-chip.runner :global(svg) {
		color: var(--primary);
	}

	/* V2 Q7 degraded session: read-only, no runner online. Warning accent so it
	   reads as an advisory (informational — there is no adopt/action here). */
	.ctx-chip.degraded {
		color: var(--warning);
		background: color-mix(in oklch, var(--warning) 12%, var(--muted));
	}

	.ctx-chip.degraded :global(svg) {
		color: var(--warning);
	}

	/* The tick inside the read-only worktree indicator must beat the generic
	   muted svg colour on chips. */
	.wt-indicator :global(.ctx-chip-cb svg) {
		color: var(--primary-foreground);
	}
</style>

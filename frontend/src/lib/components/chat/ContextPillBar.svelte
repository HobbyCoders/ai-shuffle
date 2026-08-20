<script lang="ts">
	/**
	 * ContextPillBar - A row of context pills shown directly above the chat input.
	 *
	 * Hosts the session's context selectors (profile + branch) that used to live
	 * in the page header, decluttering the header. The branch pill is shown only
	 * when a git project is available and GitHub CLI is authenticated (matching
	 * the header's previous gate); the profile pill is always available.
	 *
	 * When the interactive branch pill is suppressed (GitHub CLI not
	 * authenticated) but the session still has project/worktree context worth
	 * showing, a context badge takes its place. A remote-runner badge is a
	 * menu (re-pin / continue-here / unpin); otherwise it is read-only.
	 *
	 * In the empty/new-session state the ExecutionModeBar (Local/Worktree toggle)
	 * is hosted here too, next to the profile pill, so all pre-send session
	 * controls share one row above the input. In local mode the interactive
	 * BranchSelector pill sits alongside it (the ExecutionModeBar only carries its
	 * own branch picker in worktree mode). Once the chat is active the toggle gives
	 * way to the read-only branch pill / context badge.
	 *
	 * The pill dropdowns open upward since this bar sits just above the input.
	 */
	import { onDestroy } from 'svelte';
	import ProfileSelector from './ProfileSelector.svelte';
	import BranchSelector from './BranchSelector.svelte';
	import ExecutionModeBar from '$lib/components/layout/ExecutionModeBar.svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import { computeContextBadge, shouldShowBranchPill, shouldShowEmptyStateBranchPill } from './contextPills';
	import { gitStateQuery, runnerBadgeMenuItems } from './runner-badge-menu';
	import type { RunnerBadgeMenuItem } from './runner-badge-menu';
	import { activeSession } from '$lib/stores/active-session';
	import { projects, sessions, appData } from '$lib/stores/app-data.svelte';
	import { githubAuthenticated } from '$lib/stores/auth.svelte';
	import { git, gitStatus } from '$lib/stores/git.svelte';
	import { openHeaderDropdown, openDropdown, closeDropdown } from '$lib/stores/header-dropdowns';
	import { listInstalls, getClusterGitState, type Install } from '$lib/api/installs';
	import { adoptSession, unpinSession, moveSession } from '$lib/api/sessions';
	import { notify } from '$lib/stores/notifications';
	import { FolderClosed, GitBranch, Monitor, MonitorOff, ChevronDown, Loader2 } from 'lucide-svelte';
	import type { EligibilityMap } from '$lib/components/layout/execution-mode-eligibility';

	const projectName = $derived(
		$projects.find((p) => p.id === $activeSession.project)?.name ?? null
	);

	// The sessions-board summary row for the active session — source of the
	// server-derived ownership/health fields (degraded, is_remote, device name +
	// online) the read-only context badge reads.
	const sessionRow = $derived(
		$sessions.find((s) => s.id === $activeSession.sessionId) ?? null
	);

	// V2 Q7: the active session's server-derived degraded flag (queued work, no
	// runner online).
	const degraded = $derived(sessionRow?.degraded ?? false);

	// New-session state: no messages yet. Here the execution-mode toggle (+ branch
	// picker) is shown next to the profile pill so the user can choose Local vs
	// Worktree before sending. GitHub auth is required for worktree controls.
	const isEmptyState = $derived($activeSession.messages.length === 0);

	// Interactive branch pill in an active chat. Remote / degraded occupy this
	// slot with the runner menu (or the read-only degraded badge) instead.
	const showBranch = $derived(
		shouldShowBranchPill({
			messageCount: $activeSession.messages.length,
			project: $activeSession.project || null,
			githubAuthenticated: $githubAuthenticated,
			isRemote: sessionRow?.is_remote ?? false,
			degraded
		})
	);

	// New-session screen: show the branch pill in LOCAL mode, where the
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

	// Read-only context badge (project/worktree indicator) shown in place of the
	// interactive branch pill when that pill is suppressed but context remains —
	// see contextPills.computeContextBadge for the full gating. Mutually exclusive
	// with showBranch by construction.
	const badge = $derived(
		computeContextBadge({
			messageCount: $activeSession.messages.length,
			projectName,
			isWorktree: !!($activeSession.worktreeId || $activeSession.worktreeBranch),
			selectedWorktreeBranch: $activeSession.selectedWorktreeBranch,
			worktreeBranch: $activeSession.worktreeBranch,
			githubAuthenticated: $githubAuthenticated,
			degraded,
			isRemote: sessionRow?.is_remote ?? false,
			deviceName: sessionRow?.device_name ?? null,
			deviceOnline: sessionRow?.device_online ?? false
		})
	);

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
			isPinned: sessionRow?.is_pinned ?? false,
			installs: runnerInstalls,
			eligibility: runnerEligibility,
			hasProject: !!(sessionRow?.project_id || sessionRow?.project_v2_id),
			isWorktree: !!($activeSession.worktreeId || $activeSession.worktreeBranch)
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
					: getClusterGitState(q.project_v2_id, 'project_v2_id')
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
		if (!gitState || gitState.project_v2_id === null) {
			runnerEligibility = null;
		} else {
			const map: Record<string, boolean> = {};
			for (const row of gitState.installs) map[row.id] = row.eligible;
			runnerEligibility = map;
		}
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
			} else if (item.action.kind === 'unpin') {
				await unpinSession(sessionId);
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
	// an action button in ChatInput's send-button cluster (VoiceCallButton),
	// next to the STT mic and the send button.
</script>

<svelte:window onclick={handleRunnerClickOutside} onkeydown={handleRunnerKeydown} />

<div class="context-pill-bar">
	<ProfileSelector />
	{#if isEmptyState}
		{#if $githubAuthenticated}
			<ExecutionModeBar />
		{/if}
		{#if showEmptyStateBranch}
			<BranchSelector
				{projectName}
				onClickNewBranch={() => (showCreateBranchModal = true)}
			/>
		{/if}
	{:else if showBranch}
		<BranchSelector
			{projectName}
			onClickNewBranch={() => (showCreateBranchModal = true)}
		/>
	{:else if badge}
		{#if badge.runner && !badge.degraded}
			<div class="runner-badge-wrap" bind:this={runnerMenuRef}>
				<button
					type="button"
					class="context-badge runner"
					class:open={showRunnerMenu}
					title={badge.title}
					aria-haspopup="menu"
					aria-expanded={showRunnerMenu}
					onclick={toggleRunnerMenu}
				>
					{#if projectName}
						<FolderClosed size={13} strokeWidth={1.5} />
						<span class="pill-label project-name">{projectName}</span>
						<span class="separator">/</span>
					{/if}
					{#if badge.runner.online}
						<Monitor size={13} strokeWidth={1.5} />
					{:else}
						<MonitorOff size={13} strokeWidth={1.5} />
					{/if}
					<span class="pill-label">{badge.runner.deviceName}</span>
					<ChevronDown size={11} strokeWidth={1.5} />
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
									<span class="pill-label">{item.label}</span>
								</button>
							{/each}
						{/if}
					</div>
				{/if}
			</div>
		{:else}
			<span
				class="context-badge"
				class:runner={badge.runner}
				class:degraded={badge.degraded}
				title={badge.title}
			>
				{#if badge.degraded}
					<MonitorOff size={13} strokeWidth={1.5} />
					<span class="pill-label">Read-only — no runner online</span>
				{:else}
					{#if projectName}
						<FolderClosed size={13} strokeWidth={1.5} />
						<span class="pill-label project-name">{projectName}</span>
						{#if badge.branch || badge.runner}<span class="separator">/</span>{/if}
					{/if}
					{#if badge.runner}
						{#if badge.runner.online}
							<Monitor size={13} strokeWidth={1.5} />
						{:else}
							<MonitorOff size={13} strokeWidth={1.5} />
						{/if}
						<span class="pill-label">{badge.runner.deviceName}</span>
					{/if}
					{#if badge.branch}
						<GitBranch size={13} strokeWidth={1.5} />
						<span class="pill-label branch-name">{badge.branch}</span>
					{/if}
				{/if}
			</span>
		{/if}
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
	/* The composer island's top pill row (mockup `.pillrow`). It sits INSIDE the
	   glass island now, so it carries no surface of its own — just the row
	   metrics. Its dropdowns (agent, execution mode, branch) open upward and out
	   of the island; the explicit stacking level keeps them below the input area's
	   @/slash autocomplete panels, which is the order the old markup had.
	   `flex-wrap` lets the execution-mode bar's worktree error banner drop to its
	   own line instead of squeezing the pills. */
	.context-pill-bar {
		position: relative;
		z-index: 10;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		padding: 9px 10px 0;
		min-width: 0;
	}

	.runner-badge-wrap {
		position: relative;
		min-width: 0;
	}

	/* Project/worktree context badge — same pill metrics as the BranchSelector.
	   A span is read-only; a button (remote runner) opens the re-pin menu. */
	.context-badge {
		display: flex;
		align-items: center;
		gap: 6px;
		height: 26px;
		max-width: 100%;
		min-width: 0;
		padding: 0 9px;
		background: var(--scrim);
		border: 1px solid var(--border);
		border-radius: 999px;
		color: var(--muted-foreground);
		font-size: 12px;
		white-space: nowrap;
		cursor: default;
	}

	.context-badge :global(svg) {
		color: var(--subtle-foreground);
	}

	button.context-badge {
		cursor: pointer;
		font: inherit;
	}

	button.context-badge:hover,
	button.context-badge.open {
		border-color: var(--primary);
		color: var(--foreground);
	}

	.runner-dropdown {
		position: absolute;
		bottom: calc(100% + 6px);
		left: 0;
		min-width: 220px;
		max-width: min(320px, calc(100vw - 24px));
		max-height: 320px;
		overflow-y: auto;
		background: var(--card);
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
		background: color-mix(in oklch, var(--foreground) 8%, transparent);
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

	/* Remote-placed session: subtle accent so the runner indicator is
	   distinguishable from a local/worktree branch badge. */
	.context-badge.runner {
		color: var(--primary);
		border-color: color-mix(in oklch, var(--primary) 35%, var(--border));
		background: color-mix(in oklch, var(--primary) 8%, transparent);
	}

	.context-badge.runner :global(svg) {
		color: var(--primary);
	}

	/* V2 Q7 degraded session: read-only, no runner online. Warning accent so it
	   reads as an advisory (informational — there is no adopt/action here). */
	.context-badge.degraded {
		color: var(--warning);
		border-color: color-mix(in oklch, var(--warning) 40%, var(--border));
		background: color-mix(in oklch, var(--warning) 10%, transparent);
	}

	.context-badge.degraded :global(svg) {
		color: var(--warning);
	}


	.context-badge :global(svg) {
		flex-shrink: 0;
	}

	.pill-label {
		line-height: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		min-width: 0;
	}

	.project-name {
		max-width: 120px;
	}

	/* The branch segment reads as a ref, so it gets the mono treatment the
	   mockup's `.branchtxt` uses. */
	.branch-name {
		max-width: min(180px, 38vw);
		font-family: var(--font-mono);
		font-size: 11px;
	}

	.separator {
		color: var(--muted-foreground);
		opacity: 0.4;
		flex-shrink: 0;
	}
</style>

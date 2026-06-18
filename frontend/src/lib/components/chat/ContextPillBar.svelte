<script lang="ts">
	/**
	 * ContextPillBar - A row of context pills shown directly above the chat input.
	 *
	 * Hosts the session's context selectors (profile + branch) that used to live
	 * in the page header, decluttering the header. The branch pill is shown only
	 * when a git project is available and GitHub CLI is authenticated (matching
	 * the header's previous gate); the profile pill is always available.
	 *
	 * When the interactive branch pill is suppressed but the session still has
	 * project/worktree context worth showing — GitHub CLI not authenticated, or
	 * the worktree was cleaned up — a read-only context badge takes its place.
	 * This badge previously lived in the page header; it moves here so all of the
	 * session's context indicators sit together above the input.
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
	import ProfileSelector from './ProfileSelector.svelte';
	import BranchSelector from './BranchSelector.svelte';
	import ExecutionModeBar from '$lib/components/layout/ExecutionModeBar.svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import { computeContextBadge, shouldShowEmptyStateBranchPill } from './contextPills';
	import { activeSession } from '$lib/stores/active-session';
	import { projects } from '$lib/stores/app-data.svelte';
	import { githubAuthenticated } from '$lib/stores/auth.svelte';
	import { git } from '$lib/stores/git.svelte';
	import { FolderClosed, GitBranch, Lock } from 'lucide-svelte';

	const projectName = $derived(
		$projects.find((p) => p.id === $activeSession.project)?.name ?? null
	);

	// New-session state: no messages yet. Here the execution-mode toggle (+ branch
	// picker) is shown next to the profile pill so the user can choose Local vs
	// Worktree before sending. GitHub auth is required for worktree controls.
	const isEmptyState = $derived($activeSession.messages.length === 0);

	// Show the interactive branch pill in an active chat (messages present).
	// Mirrors the header's previous gate: project + GitHub auth + a live
	// (non-cleaned-up) session.
	const showBranch = $derived(
		!!$activeSession.project &&
			$githubAuthenticated &&
			!$activeSession.worktreeDeleted &&
			$activeSession.messages.length > 0
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
			executionMode: $activeSession.executionMode
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
			executionMode: $activeSession.executionMode,
			selectedWorktreeBranch: $activeSession.selectedWorktreeBranch,
			worktreeBranch: $activeSession.worktreeBranch,
			worktreeDeleted: $activeSession.worktreeDeleted,
			githubAuthenticated: $githubAuthenticated
		})
	);

	let showCreateBranchModal = $state(false);
</script>

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
		<span
			class="context-badge"
			class:deleted={badge.deleted}
			aria-disabled={badge.deleted ? 'true' : undefined}
			title={badge.title}
		>
			{#if projectName}
				<FolderClosed size={13} strokeWidth={1.5} />
				<span class="pill-label project-name">{projectName}</span>
				{#if badge.branch}<span class="separator">/</span>{/if}
			{/if}
			{#if badge.branch}
				<GitBranch size={13} strokeWidth={1.5} />
				<span class="pill-label branch-name">{badge.branch}</span>
			{/if}
			{#if badge.deleted}
				<Lock size={10} strokeWidth={1.5} />
			{/if}
		</span>
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
	.context-pill-bar {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
		padding: 0 2px 8px;
		min-width: 0;
	}

	/* Read-only project/worktree context badge — same pill metrics as the
	   BranchSelector, but non-interactive (no hover affordance, default cursor). */
	.context-badge {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 28px;
		max-width: 100%;
		min-width: 0;
		padding: 0 10px;
		background: color-mix(in oklch, var(--foreground) 6%, var(--background));
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		color: var(--muted-foreground);
		font-size: 0.75rem;
		font-weight: 500;
		white-space: nowrap;
		cursor: default;
	}

	/* Cleaned-up worktree: muted + dashed border to read as read-only/historical. */
	.context-badge.deleted {
		opacity: 0.7;
		border-style: dashed;
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

	.branch-name {
		max-width: min(180px, 38vw);
	}

	.separator {
		color: var(--muted-foreground);
		opacity: 0.4;
		flex-shrink: 0;
	}
</style>

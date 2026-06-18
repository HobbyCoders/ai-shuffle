<script lang="ts">
	/**
	 * ContextPillBar - A row of context pills shown directly above the chat input.
	 *
	 * Hosts the session's context selectors (profile + branch) that used to live
	 * in the page header, decluttering the header. The branch pill is shown only
	 * when a git project is available and GitHub CLI is authenticated (matching
	 * the header's previous gate); the profile pill is always available.
	 *
	 * The pill dropdowns open upward since this bar sits just above the input.
	 */
	import ProfileSelector from './ProfileSelector.svelte';
	import BranchSelector from './BranchSelector.svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { projects } from '$lib/stores/app-data.svelte';
	import { githubAuthenticated } from '$lib/stores/auth.svelte';
	import { git } from '$lib/stores/git.svelte';

	const projectName = $derived(
		$projects.find((p) => p.id === $activeSession.project)?.name ?? null
	);

	// Show the branch pill only in an active chat (messages present). In the
	// empty/new-session state the ExecutionModeBar below the input owns branch +
	// execution-mode selection, so a second branch control there would be
	// redundant (and misleading in worktree mode). Mirrors the header's previous
	// gate: project + GitHub auth + a live (non-cleaned-up) session.
	const showBranch = $derived(
		!!$activeSession.project &&
			$githubAuthenticated &&
			!$activeSession.worktreeDeleted &&
			$activeSession.messages.length > 0
	);

	let showCreateBranchModal = $state(false);
</script>

<div class="context-pill-bar">
	<ProfileSelector />
	{#if showBranch}
		<BranchSelector
			{projectName}
			onClickNewBranch={() => (showCreateBranchModal = true)}
		/>
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
</style>

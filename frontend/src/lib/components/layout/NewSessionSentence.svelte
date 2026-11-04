<script lang="ts">
	/**
	 * NewSessionSentence — the new-session screen's context row under the
	 * composer: one quiet control each for the agent, the worker, the project
	 * and, for a git project, its branch and worktree.
	 *
	 *   Overseer ⌄  Workstation ⌄  my-app ⌄  New branch from main ⌄  Worktree
	 *   Overseer ⌄  Workstation ⌄  No project ⌄
	 *
	 * Each is a real button that opens the menu its chat chip opens
	 * (ProfileSelector, RunsOnPicker, WorkspaceFolderSelector, ExecutionModeBar's
	 * branch menu or BranchSelector, WorkspaceSelector), in their `sentence`
	 * variants, whose menus float to <body>; the worktree one toggles the
	 * worktree. Which parts show follows ContextPillBar's rules for a new
	 * session: branch and worktree only for a project that is a git repository,
	 * the checkout branch only in local mode with GitHub signed in. The worker's
	 * online dot lives in its menu.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import { clusterProjects, projects } from '$lib/stores/app-data.svelte';
	import { githubAuthenticated } from '$lib/stores/auth.svelte';
	import { git } from '$lib/stores/git.svelte';
	import { composerIsGitRepo, shouldShowEmptyStateBranchPill } from '$lib/components/chat/contextPills';
	import ProfileSelector from '$lib/components/chat/ProfileSelector.svelte';
	import WorkspaceFolderSelector from '$lib/components/chat/WorkspaceFolderSelector.svelte';
	import BranchSelector from '$lib/components/chat/BranchSelector.svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import ExecutionModeBar from './ExecutionModeBar.svelte';
	import RunsOnPicker from './RunsOnPicker.svelte';
	import WorkspaceSelector from './WorkspaceSelector.svelte';
	import NewChatSetupCallout from './NewChatSetupCallout.svelte';

	const hasProject = $derived(!!($activeSession.project || $activeSession.projectV2Id));
	const projectName = $derived(
		$projects.find((p) => p.id === $activeSession.project)?.name ??
			$clusterProjects.find((p) => p.project_v2_id === $activeSession.projectV2Id)?.name ??
			null
	);
	// The checkout branch word: local mode only (worktree mode's branch word is
	// ExecutionModeBar's), the same gate as the chip row's empty-state branch chip.
	const showCheckoutBranch = $derived(
		shouldShowEmptyStateBranchPill({
			messageCount: 0,
			project: $activeSession.project || null,
			githubAuthenticated: $githubAuthenticated,
			worktreeIntent: $activeSession.worktreeIntent,
			isGitRepo: composerIsGitRepo($git, $activeSession)
		})
	);
	let showCreateBranchModal = $state(false);

	function selectProject(projectId: string, projectV2Id?: string | null) {
		activeSession.setProject(projectId, projectV2Id ?? null, { user: true });
	}
</script>

<section class="ns-context" aria-label="New chat">
	<ProfileSelector variant="card" />
	<div class="ns-sentence" role="group" aria-label="Where this session runs">
		<RunsOnPicker />
		{#if hasProject}
			<ExecutionModeBar variant="sentence">
				{#snippet between()}
					{#if projectName}<WorkspaceFolderSelector variant="sentence" {projectName} onClear={() => activeSession.clearProject({ user: true })} />{:else}<span class="sentence-tok ns-pending">Project</span>{/if}
					{#if showCheckoutBranch}
						<BranchSelector variant="sentence" onClickNewBranch={() => (showCreateBranchModal = true)} />
					{/if}
				{/snippet}
			</ExecutionModeBar>
		{:else}
			<WorkspaceSelector
				variant="sentence"
				selectedProject={$activeSession.project}
				selectedProjectV2Id={$activeSession.projectV2Id}
				onSelectProject={selectProject}
			/>
		{/if}
	</div>
</section>
{#if hasProject}
	<!-- The picked worker lacks the project: say so, and that Send sets it up first. -->
	<NewChatSetupCallout />
{/if}

{#if showCheckoutBranch}
	<CreateBranchModal
		open={showCreateBranchModal}
		onClose={() => (showCreateBranchModal = false)}
		onCreated={() => git.refreshStatus()}
	/>
{/if}

<style>
	/* The row wraps rather than clips on a narrow window: nothing above it moves.
	   On a phone it stays one line that scrolls sideways; its menus float to
	   <body>, so the scroller cannot clip them. */
	.ns-context {
		position: relative;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 2px;
		min-width: 0;
		margin: 8px 0 0 6px;
	}
	/* The group names its controls; they flow in the row itself. */
	.ns-sentence { display: contents; }
	.ns-pending { color: var(--subtle-foreground); cursor: default; }
	@media (max-width: 480px) {
		.ns-context {
			margin: 2px 0 0;
			flex-wrap: nowrap;
			overflow-x: auto;
			scrollbar-width: none;
		}
		.ns-context::-webkit-scrollbar { display: none; }
		.ns-context :global(button) { flex: none; }
	}
</style>

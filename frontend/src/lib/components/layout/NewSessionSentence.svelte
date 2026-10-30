<script lang="ts">
	/**
	 * NewSessionSentence — where a new session will run, as one sentence under
	 * the agent's name on the new-session screen:
	 *
	 *   Runs on <worker> in <project> on <a new branch from main> in <a worktree>.
	 *   Runs on <worker> with <no project>.
	 *
	 * Every underlined word is a real button that opens the menu its composer
	 * chip opens (RunsOnPicker, WorkspaceFolderSelector, ExecutionModeBar's
	 * branch menu or BranchSelector, WorkspaceSelector), in their `sentence`
	 * variants: the trigger is the word, and the menu drops down centred under
	 * it. The worktree word toggles the worktree. Which parts show follows the
	 * composer chip row's rules for a new session (ContextPillBar): branch and
	 * worktree only for a project that is a git repository, the checkout branch
	 * only in local mode with GitHub signed in. The worker's online dot lives in
	 * its menu, never in the sentence.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import { clusterProjects, projects } from '$lib/stores/app-data.svelte';
	import { githubAuthenticated } from '$lib/stores/auth.svelte';
	import { git } from '$lib/stores/git.svelte';
	import { composerIsGitRepo, shouldShowEmptyStateBranchPill } from '$lib/components/chat/contextPills';
	import WorkspaceFolderSelector from '$lib/components/chat/WorkspaceFolderSelector.svelte';
	import BranchSelector from '$lib/components/chat/BranchSelector.svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import ExecutionModeBar from './ExecutionModeBar.svelte';
	import RunsOnPicker from './RunsOnPicker.svelte';
	import WorkspaceSelector from './WorkspaceSelector.svelte';

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

<div class="ns-sentence" role="group" aria-label="Where this session runs">
	Runs on <RunsOnPicker />
	{#if hasProject}
		<ExecutionModeBar variant="sentence">
			{#snippet between()}
				in {#if projectName}<WorkspaceFolderSelector variant="sentence" {projectName} onClear={() => activeSession.clearProject({ user: true })} />{:else}<span class="ns-pending">a project</span>{/if}
				{#if showCheckoutBranch}
					on <BranchSelector variant="sentence" onClickNewBranch={() => (showCreateBranchModal = true)} />
				{/if}
			{/snippet}
			{#snippet after()}<span class="ns-stop" aria-hidden="true">.</span>{/snippet}
		</ExecutionModeBar>
	{:else}
		<!-- inline-flex: the picker's trailing markup whitespace (its add-project
		     dialog) must not show as a gap before the full stop. -->
		with <span class="ns-word"><WorkspaceSelector
			variant="sentence"
			selectedProject={$activeSession.project}
			selectedProjectV2Id={$activeSession.projectV2Id}
			onSelectProject={selectProject}
		/><span class="ns-stop" aria-hidden="true">.</span></span>
	{/if}
</div>

{#if showCheckoutBranch}
	<CreateBranchModal
		open={showCreateBranchModal}
		onClose={() => (showCreateBranchModal = false)}
		onCreated={() => git.refreshStatus()}
	/>
{/if}

<style>
	.ns-sentence {
		max-width: 46rem;
		margin: 0;
		font-size: 0.90625rem;
		line-height: 2;
		color: var(--muted-foreground);
		text-align: center;
		text-wrap: balance;
	}
	.ns-pending { color: var(--subtle-foreground); }
	.ns-word { display: inline-flex; align-items: baseline; }
</style>

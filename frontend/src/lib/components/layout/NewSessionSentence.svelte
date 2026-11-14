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
	 *
	 * Cmd/Ctrl+P (AppPage's shortcut) opens the project picker from here while
	 * the row is on screen: on "No project" to add one, or on the project word
	 * to switch it (with a "No project" row). Focus returns to the message
	 * editor after a pick or Escape.
	 */
	import { onMount } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { caretOffset, placeCaret } from '$lib/services/caret';
	import { provideProjectPicker } from '$lib/services/project-picker';
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
	const clearProject = () => activeSession.clearProject({ user: true });

	let row = $state<HTMLElement | null>(null);
	let picker = $state<ReturnType<typeof WorkspaceSelector> | null>(null);

	/** Open the picker on the row's project control; false when the row is not usable now. */
	function openPicker(returnFocus: HTMLElement | (() => void) | null): boolean {
		if ($activeSession.sessionId || !row?.isConnected || !picker) return false;
		// Hidden under another page (Settings, Agents): not on screen.
		if (typeof row.checkVisibility === 'function' && !row.checkVisibility()) return false;
		// A dialog over the screen (Settings search, a modal) owns the keys.
		if (document.querySelector('[aria-modal="true"]')) return false;
		const anchor = row.querySelector<HTMLElement>('[data-project-control]');
		picker.openPicker({ anchor, returnFocus });
		return true;
	}

	/** "Switch project…" in the folders menu: open once its click has finished (it would close the picker as an outside click). */
	function switchFrom(trigger: HTMLElement) {
		setTimeout(() => openPicker(trigger));
	}

	/** The message box on this screen (the one on show, if a hidden copy is mounted). */
	function composerEditor(): HTMLElement | null {
		const editors = document.querySelectorAll<HTMLElement>('[data-message-editor] [contenteditable="true"]');
		return [...editors].find(el => typeof el.checkVisibility !== 'function' || el.checkVisibility()) ?? null;
	}

	/**
	 * After a pick or Escape, back to the message box with the caret where it
	 * was (at the end when focus was elsewhere). Found again by then if the
	 * pick re-rendered it.
	 */
	function backToComposer(): () => void {
		const focused = document.activeElement;
		const editor = focused instanceof HTMLElement && focused.closest('[data-message-editor]') ? focused : null;
		const offset = editor ? caretOffset(editor) : null;
		return () => {
			const target = editor?.isConnected ? editor : composerEditor();
			if (!target) return;
			target.focus({ preventScroll: true });
			placeCaret(target, offset);
		};
	}

	onMount(() => provideProjectPicker(() => openPicker(backToComposer())));
</script>

<section class="ns-context" aria-label="New chat" bind:this={row}>
	<ProfileSelector variant="card" />
	<div class="ns-sentence" role="group" aria-label="Where this session runs">
		<RunsOnPicker />
		{#if hasProject}
			<ExecutionModeBar variant="sentence">
				{#snippet between()}
					{#if projectName}<WorkspaceFolderSelector variant="sentence" {projectName} onClear={clearProject} onSwitch={switchFrom} />{:else}<span class="sentence-tok ns-pending" data-project-control>Project</span>{/if}
					{#if showCheckoutBranch}
						<BranchSelector variant="sentence" onClickNewBranch={() => (showCreateBranchModal = true)} />
					{/if}
				{/snippet}
			</ExecutionModeBar>
		{/if}
		<!-- One picker for both: its own "No project" word, or (headless) opened on
		     the project word to switch. Keyed so a pick remounts it closed. -->
		{#key hasProject}
			<WorkspaceSelector
				bind:this={picker}
				variant="sentence"
				headless={hasProject}
				selectedProject={$activeSession.project}
				selectedProjectV2Id={$activeSession.projectV2Id}
				onSelectProject={selectProject}
				onClearProject={clearProject}
			/>
		{/key}
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

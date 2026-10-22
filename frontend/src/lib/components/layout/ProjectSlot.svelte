<script lang="ts">
	/**
	 * ProjectSlot — the new-session screen's optional project (#371 step 4).
	 *
	 * No project (the default): a quiet "+ Add a project · Optional" row. Once
	 * one is picked it unfolds into the project strip: the project chip (whose
	 * menu also holds extra folders), branch and worktree — the existing
	 * ContextPillBar chips, without the agent and runner the card above owns.
	 * ✕ clears the project and the session is back to having none.
	 */
	import { X } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { platform } from '$lib/stores/platform.svelte';
	import ContextPillBar from '$lib/components/chat/ContextPillBar.svelte';
	import WorkspaceSelector from './WorkspaceSelector.svelte';

	const hasProject = $derived(!!($activeSession.project || $activeSession.projectV2Id));

	function selectProject(projectId: string, projectV2Id?: string | null) {
		activeSession.setProject(projectId, projectV2Id ?? null);
	}
</script>

<div class="project-slot" class:has-project={hasProject}>
	{#if hasProject}
		<div class="project-strip" role="group" aria-label="Project for this session">
			<div class="strip-chips">
				<ContextPillBar strip sheetMode={platform.isMobile} />
			</div>
			<button
				type="button"
				class="strip-clear"
				onclick={() => activeSession.clearProject()}
				aria-label="Remove project"
				title="Remove project — start without one"
			>
				<X size={14} strokeWidth={2} aria-hidden="true" />
			</button>
		</div>
	{:else}
		<WorkspaceSelector
			variant="add"
			selectedProject={$activeSession.project}
			selectedProjectV2Id={$activeSession.projectV2Id}
			onSelectProject={selectProject}
		/>
	{/if}
</div>

<style>
	.project-slot { width: 100%; max-width: 48rem; margin: 0 auto; min-height: 34px; }
	.project-strip {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 6px 6px 8px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 10px) * 1.2);
		background: color-mix(in oklch, var(--card) 60%, transparent);
		animation: strip-unfold 320ms cubic-bezier(0.16, 1, 0.3, 1) both;
		transform-origin: top center;
	}
	.strip-chips { flex: 1; min-width: 0; }
	/* The strip sits below the composer, so its chip menus open downward. */
	.project-strip :global(.branch-dropdown),
	.project-strip :global(.workspace-dropdown),
	.project-strip :global(.selector-dropdown) {
		top: calc(100% + 6px);
		bottom: auto;
	}
	.strip-clear {
		flex: none;
		display: grid;
		place-items: center;
		width: 26px;
		height: 26px;
		border: none;
		border-radius: calc(var(--radius, 10px) * 0.6);
		background: transparent;
		color: var(--subtle-foreground);
		cursor: pointer;
		transition: background 150ms ease, color 150ms ease;
	}
	.strip-clear:hover { background: var(--hover-overlay); color: var(--foreground); }
	.strip-clear:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	@keyframes strip-unfold {
		from { opacity: 0; transform: translateY(-6px) scaleY(0.96); }
	}
	@media (prefers-reduced-motion: reduce) {
		.project-strip { animation: none; }
		.strip-clear { transition: none; }
	}
</style>

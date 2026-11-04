<script lang="ts">
	/**
	 * NewSessionHero — the new-session screen's single focal point: a question,
	 * directly above the composer it belongs to.
	 *
	 *   What should we build in my-app?
	 *   What should we work on?          (no project)
	 *
	 * Agent, worker and project are quiet controls under the composer
	 * (NewSessionSentence, the `New session` region); ChatInput mounts them.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import { clusterProjects, profiles, profilesLoaded, projects } from '$lib/stores/app-data.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { UI_MOUNT } from '$lib/utils/ui-signature';

	const projectName = $derived(
		$projects.find((p) => p.id === $activeSession.project)?.name ??
			$clusterProjects.find((p) => p.project_v2_id === $activeSession.projectV2Id)?.name ??
			null
	);
	const noAgents = $derived($profilesLoaded && $profiles.length === 0);
</script>

<section class="ns-hero" data-ui-mount={UI_MOUNT.newSessionHero}>
	<h1 class="ns-title">
		{#if projectName}What should we build in <span class="ns-project">{projectName}</span>?{:else}What should we work on?{/if}
	</h1>
	{#if noAgents}
		<button type="button" class="no-agents" onclick={() => layout.openFeaturePage('profile')}>
			No agents yet — create one to start
		</button>
	{/if}
</section>

<style>
	.ns-hero {
		display: flex;
		flex-direction: column;
		align-items: center;
		width: 100%;
		margin: 0 auto;
		padding: 0 0 20px;
		text-align: center;
		animation: ns-hero-in 320ms cubic-bezier(0.16, 1, 0.3, 1) both;
	}
	.ns-title {
		margin: 0;
		max-width: 100%;
		color: var(--foreground);
		font-size: var(--fs-display);
		font-weight: 500;
		line-height: 1.25;
		letter-spacing: -0.015em;
		overflow-wrap: anywhere;
		text-wrap: balance;
	}
	.no-agents {
		margin-top: 8px;
		padding: 0;
		border: none;
		background: none;
		color: var(--primary);
		font: inherit;
		font-size: var(--fs-small);
		cursor: pointer;
	}
	.no-agents:hover { text-decoration: underline; }
	.no-agents:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; border-radius: 4px; }

	/* Fills only: a filled transform would make the hero a stacking context. */
	@keyframes ns-hero-in { from { opacity: 0; } }

	@media (max-width: 480px) {
		.ns-title { font-size: var(--fs-title); line-height: 1.3; }
	}
	@media (prefers-reduced-motion: reduce) {
		.ns-hero { animation: none; }
	}
</style>

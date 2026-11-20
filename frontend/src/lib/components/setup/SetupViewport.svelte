<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { SetupStepId } from './setup-steps';

	let { step, children }: { step: SetupStepId; children: Snippet } = $props();
	let viewport: HTMLDivElement;

	$effect(() => {
		// Keep the same scroll owner, but start each new step at its heading.
		if (step && viewport) viewport.scrollTop = 0;
	});
</script>

<div class="setup-viewport ui-surface ui-onboarding" bind:this={viewport}>
	<div class="ui-onboarding-card">
		{@render children()}
	</div>
</div>

<style>
	/* The app locks document scrolling. Give the entire wizard one viewport;
	   auto margins center a fitting card and collapse when it overflows. */
	.setup-viewport.ui-onboarding {
		display: flex;
		flex-direction: column;
		min-height: 0;
		height: 100vh;
		height: var(--app-viewport-height, 100dvh);
		overflow-y: auto;
		overscroll-behavior-y: contain;
	}
	.ui-onboarding-card {
		flex: none;
		margin: auto;
	}
</style>

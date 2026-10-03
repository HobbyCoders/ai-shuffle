<script lang="ts">
	/**
	 * BuildPage: the Agents page. It hosts ProfileCard, which draws the agent
	 * list and the editor beside it. MCP servers, hooks and plugins live in
	 * Settings. ProfileCard loads as its own chunk because it pulls in the
	 * editor, forms and validation.
	 */
	import LazyPanel from '$lib/components/deck/lazy/LazyPanel.svelte';
	import { createLazyComponent } from '$lib/components/deck/lazy/lazy-component';

	const AGENTS_PANEL = createLazyComponent('build:profile', () =>
		import('$lib/components/deck/cards/ProfileCard.svelte')
	);

	interface Props {
		/** One-shot deep-link: the agent to open in the editor. */
		requestedItemId?: string | null;
	}
	let { requestedItemId }: Props = $props();
</script>

<div class="build-page">
	<LazyPanel handle={AGENTS_PANEL} label="Agents" props={{ initialSelectedId: requestedItemId }} />
</div>

<style>
	.build-page {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
		background: var(--background);
	}
	.build-page > :global(*) {
		flex: 1;
		min-height: 0;
	}
</style>

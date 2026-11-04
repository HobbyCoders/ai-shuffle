<script lang="ts">
	/**
	 * The worktree chip's state glyph, one 14px mark in every row: an empty
	 * circle when the chat runs in the main checkout, a filled check circle when
	 * it runs in a worktree. Decorative: each chip says the state in words too.
	 */
	interface Props {
		on: boolean;
	}

	let { on }: Props = $props();
</script>

<svg
	class="worktree-glyph"
	width="14"
	height="14"
	viewBox="0 0 24 24"
	fill="none"
	stroke="currentColor"
	stroke-width="2"
	stroke-linecap="round"
	stroke-linejoin="round"
	aria-hidden="true"
>
	{#if on}
		<circle cx="12" cy="12" r="10" fill="currentColor" />
		<path d="m8 12.5 2.8 2.8L16 9.5" stroke="var(--background)" stroke-width="2.4" />
	{:else}
		<circle cx="12" cy="12" r="9.5" />
	{/if}
</svg>

<script lang="ts">
	/** ListGroupLabel: a group heading in a collection list ("Workers", "Not connected"), in the one Settings section style. */
	import type { Snippet } from 'svelte';

	interface Props {
		label: string;
		/** Optional trailing content (a count, a small action). */
		children?: Snippet;
		/** A narrow list pane beside a detail, where it reads as a sidebar group, under the pane's own title. */
		compact?: boolean;
	}

	let { label, children, compact = false }: Props = $props();
</script>

<div class="st-lg" class:compact role="presentation">
	<span>{label}</span>
	{#if children}<span class="act">{@render children()}</span>{/if}
</div>

<style>
	.st-lg {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 14px;
		font-weight: 600;
		letter-spacing: -0.005em;
		color: var(--st-fg);
		padding: 40px 0 20px;
	}
	.st-lg:first-child {
		padding-top: 8px;
	}
	.st-lg.compact {
		gap: 6px;
		font-size: 12px;
		font-weight: 500;
		letter-spacing: 0;
		color: var(--st-mfg);
		padding: 16px 12px 6px;
	}
	.st-lg.compact .act {
		margin-left: 0;
		font-weight: 400;
		color: var(--st-subtle);
	}
	.act {
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		gap: 4px;
	}
</style>

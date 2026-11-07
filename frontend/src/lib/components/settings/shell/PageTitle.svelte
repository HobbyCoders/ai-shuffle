<script lang="ts">
	import type { Snippet } from 'svelte';
	let { title, count, actions }: { title: string; count?: string | number; actions?: Snippet } = $props();
</script>
<div class="st-page-title">
	<h1 tabindex="-1" data-settings-heading="page">{title}</h1>
	{#if count !== undefined && count !== ''}<span class="count">{count}</span>{/if}
	<div class="actions">{@render actions?.()}</div>
</div>
<style>
	.st-page-title { display: flex; align-items: center; gap: 10px; min-height: 32px; margin-bottom: 28px; }
	h1 { align-self: flex-start; margin: 0; min-width: 0; font-size: 24px; font-weight: 600; line-height: 30px; letter-spacing: -.012em; overflow-wrap: anywhere; }
	h1:focus { outline: none; }
	.count { color: var(--st-mfg); font-size: 13px; line-height: 20px; font-variant-numeric: tabular-nums; }
	.st-page-title :global(.st-btn) { margin-block: 0; }
	.actions { display: flex; align-items: center; gap: 8px; margin-left: auto; flex: none; }
	/* Phone: the header action is drawn at the 44px phone action height
	   (settings.css .phone-action); a long title wraps above it instead of
	   squeezing it. */
	:global(.settings-shell[data-stacked]) .st-page-title { flex-wrap: wrap; row-gap: 8px; }
	@media (max-width: 600px), (pointer: coarse) {
		:global(.settings-shell) .st-page-title { flex-wrap: wrap; row-gap: 8px; }
	}
</style>

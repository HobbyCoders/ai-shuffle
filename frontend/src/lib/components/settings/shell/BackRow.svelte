<script lang="ts">
	/** One parent-labelled back row for every stacked detail and Settings page. */
	import type { Snippet } from 'svelte';
	import { ChevronLeft } from 'lucide-svelte';
	let { label, onclick, children }: { label: string; onclick: () => void; children?: Snippet } = $props();
</script>
<div class="st-back-row st-dtop">
	<button type="button" class="st-back" aria-label={`Back to ${label}`} {onclick}><ChevronLeft /><span>{label}</span></button>
	<div class="back-status">{@render children?.()}</div>
</div>
<style>
	.st-back-row { position: absolute; top: 10px; inset-inline: var(--st-gutter); display: flex; align-items: center; min-height: 32px; gap: 8px; }
	.back-status { display: flex; align-items: center; margin-left: auto; }
	:global(.settings-shell[data-stacked]) .st-back-row { position: sticky; top: 0; z-index: 5; min-height: 56px; margin: 0 calc(-1 * var(--st-gutter)) 20px; padding: 0 14px 0 8px; background: var(--st-bg); border-bottom: 1px solid var(--st-border); }
</style>

<script lang="ts">
	/** Test double for the shared usage ring: a button and the bindable percent. */
	let { percent = $bindable(0) }: { onOpenChange?: (open: boolean) => void; showPercent?: boolean; percent?: number } = $props();
	percent = 42;
	export function openDetails() {}
</script>

<button type="button" aria-label="Context and usage — 42% of context window used">42%</button>

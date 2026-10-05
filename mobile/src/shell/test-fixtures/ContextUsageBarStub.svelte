<script lang="ts">
	/** Test double for the shared usage ring: a button and the bindable percent / over-limit flag. */
	let { percent = $bindable(0), overLimit = $bindable(false) }: { onOpenChange?: (open: boolean) => void; showPercent?: boolean; percent?: number; overLimit?: boolean } = $props();
	percent = 42;
	// A test sets this to drive the panel's over-the-limit wording.
	overLimit = (globalThis as { __usageStubOverLimit?: boolean }).__usageStubOverLimit ?? false;
	export function openDetails() {}
</script>

<button type="button" aria-label="Context and usage — 42% of context window used">42%</button>

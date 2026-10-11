<script lang="ts">
	/** Test double for the shared usage ring: a button and the bindable percent / over-limit / unknown-window values. */
	let {
		percent = $bindable(0),
		overLimit = $bindable(false),
		unknownWindowUsed = $bindable(null)
	}: { onOpenChange?: (open: boolean) => void; showPercent?: boolean; percent?: number; overLimit?: boolean; unknownWindowUsed?: string | null } = $props();
	percent = 42;
	// A test sets these to drive the panel's over-the-limit / unknown-window wording.
	overLimit = (globalThis as { __usageStubOverLimit?: boolean }).__usageStubOverLimit ?? false;
	unknownWindowUsed = (globalThis as { __usageStubUnknownUsed?: string }).__usageStubUnknownUsed ?? null;
	export function openDetails() {}
</script>

<button type="button" aria-label="Context and usage — 42% of context window used">42%</button>

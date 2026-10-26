<script lang="ts">
	/**
	 * Test double for the shared usage ring: a button and the bindable percent /
	 * over-limit / unknown-window / token values. `inline` draws a marker for
	 * the Chat controls "Context and usage" page instead of the button.
	 */
	let {
		percent = $bindable(0),
		overLimit = $bindable(false),
		unknownWindowUsed = $bindable(null),
		usedTokens = $bindable(''),
		windowTokens = $bindable(null),
		inline = false
	}: {
		onOpenChange?: (open: boolean) => void; showPercent?: boolean; percent?: number; overLimit?: boolean;
		unknownWindowUsed?: string | null; usedTokens?: string; windowTokens?: string | null; inline?: boolean;
	} = $props();
	percent = 42;
	// A test sets these to drive the panel's over-the-limit / unknown-window wording.
	overLimit = (globalThis as { __usageStubOverLimit?: boolean }).__usageStubOverLimit ?? false;
	unknownWindowUsed = (globalThis as { __usageStubUnknownUsed?: string }).__usageStubUnknownUsed ?? null;
	usedTokens = unknownWindowUsed ?? '84k';
	windowTokens = unknownWindowUsed ? null : '200k';
	export function openDetails() {}
</script>

{#if inline}
	<div data-testid="usage-inline">Context window details</div>
{:else}
	<button type="button" aria-label="Context and usage — 42% of context window used">42%</button>
{/if}

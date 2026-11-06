<script lang="ts">
	/**
	 * ValueText: a read-only value on the right of a row ("0.8.22",
	 * "Active" with a dot). `children` replaces `value` for richer content.
	 */
	import type { Snippet } from 'svelte';
	import type { StatusTone } from '$lib/settings/types';
	import StatusDot from './StatusDot.svelte';

	interface Props {
		value?: string | number | null;
		mono?: boolean;
		/** Retained for callers; read-only values share the secondary-ink style. */
		tone?: 'muted' | 'fg';
		status?: StatusTone;
		children?: Snippet;
	}

	let { value, mono = false, tone = 'muted', status, children }: Props = $props();
</script>

<span class="st-val" class:st-mono={mono} class:fg={tone === 'fg'}>
	{#if status}<StatusDot tone={status} />{/if}
	{#if children}{@render children()}{:else}{value ?? ''}{/if}
</span>

<style>
	.st-val {
		font-size: 13px;
		color: var(--st-mfg);
		white-space: nowrap;
		display: inline-flex;
		align-items: center;
		gap: 7px;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.st-val.fg {
		color: var(--st-mfg);
	}
	.st-val.st-mono {
		font-size: 13px;
	}
</style>

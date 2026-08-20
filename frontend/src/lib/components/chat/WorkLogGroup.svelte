<script lang="ts">
	/**
	 * WorkLogGroup — the fold that hides a turn's routine steps.
	 *
	 * Two modes, both rendered as flat rows with no card chrome:
	 *  - live: the newest tool row stays on screen and the earlier ones hide
	 *    behind "+N previous tool calls".
	 *  - settled: the whole run — tool rows plus the turn's non-final commentary
	 *    — hides behind one "Worked for {duration}" row.
	 *
	 * The rows themselves are rendered by the caller through the `row` snippet,
	 * so this component owns the fold and nothing else; MessageArea keeps the one
	 * dispatch table that decides which component a message becomes.
	 */
	import type { Snippet } from 'svelte';
	import type { ChatMessage } from '$lib/stores/types';
	import type { WorkLogEntry } from '$lib/utils/work-log';

	interface Props {
		mode: 'live' | 'settled';
		/** Rows behind the fold. */
		folded: WorkLogEntry[];
		/** Rows always visible (live mode's newest row). */
		tail: WorkLogEntry[];
		/** "Worked for 2m 14s" / "Stopped after 12s"; null when unknown. */
		durationLabel?: string | null;
		row: Snippet<[ChatMessage, number]>;
	}

	let { mode, folded, tail, durationLabel = null, row }: Props = $props();

	let open = $state(false);

	const collapsedLabel = $derived(
		mode === 'live'
			? `+${folded.length} previous tool ${folded.length === 1 ? 'call' : 'calls'}`
			: (durationLabel ?? 'Show work log')
	);
</script>

<section class="worklog" class:is-settled={mode === 'settled'} aria-label="Work log">
	{#if open}
		<div class="wlhead">Work Log</div>
		{#each folded as entry (entry.message.id ?? entry.index)}
			{@render row(entry.message, entry.index)}
		{/each}
		{#each tail as entry (entry.message.id ?? entry.index)}
			{@render row(entry.message, entry.index)}
		{/each}
		<button type="button" class="wlrow fold" onclick={() => (open = false)} aria-expanded="true">
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<path d="m18 15-6-6-6 6" />
			</svg>
			Show fewer log entries
		</button>
	{:else}
		<button
			type="button"
			class="wlrow fold"
			class:hairline={mode === 'settled'}
			onclick={() => (open = true)}
			aria-expanded="false"
		>
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<path d="m6 9 6 6 6-6" />
			</svg>
			{collapsedLabel}
		</button>
		{#each tail as entry (entry.message.id ?? entry.index)}
			{@render row(entry.message, entry.index)}
		{/each}
	{/if}
</section>

<style>
	.worklog {
		display: flex;
		flex-direction: column;
		gap: 1px;
		margin: 0 -8px;
		min-width: 0;
	}

	.wlhead {
		padding: 10px 8px 5px;
		font-size: 10.5px;
		font-weight: 600;
		letter-spacing: 0.075em;
		text-transform: uppercase;
		color: var(--subtle-foreground);
	}

	.wlrow.fold {
		display: flex;
		align-items: center;
		gap: 7px;
		width: 100%;
		padding: 6px 8px;
		border: none;
		background: transparent;
		border-radius: 8px;
		text-align: left;
		cursor: pointer;
		font-size: 12px;
		color: var(--subtle-foreground);
		transition: background-color 0.13s ease, color 0.13s ease;
	}

	.wlrow.fold:hover {
		background: var(--scrim);
		color: var(--muted-foreground);
	}

	.wlrow.fold:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.wlrow.fold.hairline {
		border-bottom: 1px solid var(--border);
		border-radius: 8px 8px 0 0;
	}
</style>

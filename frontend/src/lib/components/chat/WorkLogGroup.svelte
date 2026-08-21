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
	import type { WorkLogEntry, WorkLogSequenceEntry } from '$lib/utils/work-log';

	interface Props {
		mode: 'live' | 'settled';
		/** Rows behind the fold. */
		folded: WorkLogEntry[];
		/** Rows always visible (live mode's newest row). */
		tail: WorkLogEntry[];
		/** Ordered settled turn; protected/final rows stay visible while collapsed. */
		sequence?: WorkLogSequenceEntry[];
		/** "Worked for 2m 14s" / "You stopped after 12s"; null when unknown. */
		durationLabel?: string | null;
		/** Action-specific description of the folded work. */
		summary?: string | null;
		row: Snippet<[ChatMessage, number]>;
	}

	let { mode, folded, tail, sequence = [], durationLabel = null, summary = null, row }: Props = $props();
	const retainedSequence = $derived(sequence.filter((entry) => !entry.hiddenWhenCollapsed));

	let open = $state(false);
	let previousMode = $state<'live' | 'settled' | null>(null);

	// A live run may be open while the user inspects it. Once the turn settles,
	// deliberately return to the compact finished presentation.
	$effect(() => {
		const currentMode = mode;
		if (previousMode === 'live' && currentMode === 'settled') open = false;
		previousMode = currentMode;
	});

	const collapsedLabel = $derived(
		mode === 'live'
			? (summary ?? `+${folded.length} previous tool ${folded.length === 1 ? 'call' : 'calls'}`)
			: (durationLabel ?? 'Worked')
	);
	const disclosureLabel = $derived(
		open ? 'Hide work details' : `Show work details: ${collapsedLabel}${summary && mode === 'settled' ? `, ${summary}` : ''}`
	);

	function isAssistantProse(entry: WorkLogSequenceEntry): boolean {
		return entry.message.role === 'assistant' && (entry.message.type === 'text' || !entry.message.type);
	}
</script>

<section class="worklog" class:is-settled={mode === 'settled'} aria-label="Work log" data-work-log-mode={mode}>
	{#if open}
		<div class="wlhead">Work log{#if summary}<span> · {summary}</span>{/if}</div>
		{#if mode === 'settled' && sequence.length > 0}
			<div class="work-sequence is-expanded">
				{#each sequence as entry (entry.message.id ?? entry.index)}
					<div class="sequence-row" class:is-prose={isAssistantProse(entry)}>
						{@render row(entry.message, entry.index)}
					</div>
				{/each}
			</div>
		{:else}
			{#each folded as entry (entry.message.id ?? entry.index)}
				{@render row(entry.message, entry.index)}
			{/each}
			{#each tail as entry (entry.message.id ?? entry.index)}
				{@render row(entry.message, entry.index)}
			{/each}
		{/if}
		<button type="button" class="wlrow fold" onclick={() => (open = false)} aria-expanded="true" aria-label={disclosureLabel}>
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
			aria-label={disclosureLabel}
		>
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<path d="m6 9 6 6 6-6" />
			</svg>
			<span class="fold-label">{collapsedLabel}</span>
			{#if mode === 'settled' && summary}
				<span class="fold-summary" title={summary}>· {summary}</span>
			{/if}
		</button>
		{#if mode === 'settled' && sequence.length > 0}
			<div class="work-sequence is-collapsed">
				{#each retainedSequence as entry (entry.message.id ?? entry.index)}
					<div class="sequence-row" class:is-prose={isAssistantProse(entry)}>
						{@render row(entry.message, entry.index)}
					</div>
				{/each}
			</div>
		{:else}
			{#each tail as entry (entry.message.id ?? entry.index)}
				{@render row(entry.message, entry.index)}
			{/each}
		{/if}
	{/if}
</section>

<style>
	.worklog {
		display: flex;
		flex-direction: column;
		gap: 1px;
		margin: 0;
		min-width: 0;
	}

	.worklog.is-settled {
		animation: worklog-settle 180ms ease-out both;
	}

	.wlhead {
		padding: 10px 8px 5px;
		font-size: 10.5px;
		font-weight: 600;
		letter-spacing: 0.045em;
		color: var(--subtle-foreground);
	}

	.wlhead span {
		font-weight: 400;
		letter-spacing: 0;
		text-transform: none;
	}

	.work-sequence {
		display: flex;
		flex-direction: column;
		gap: 8px;
		min-width: 0;
	}

	.work-sequence.is-collapsed {
		gap: 12px;
	}

	.sequence-row {
		min-width: 0;
	}

	.sequence-row.is-prose {
		margin-top: 12px;
	}

	.work-sequence.is-collapsed .sequence-row.is-prose {
		margin-top: 8px;
	}

	.work-sequence + .wlrow.fold {
		margin-top: 8px;
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
		font-size: 13px;
		line-height: 1.5;
		color: var(--muted-foreground);
		transition: background-color 180ms ease-out, color 180ms ease-out;
	}

	.fold-label {
		flex: none;
		font-variant-numeric: tabular-nums;
	}

	.fold-summary {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 11.5px;
		color: var(--subtle-foreground);
	}

	.wlrow.fold:hover {
		background: var(--scrim);
		color: var(--foreground);
	}

	.wlrow.fold:active {
		background: var(--active-overlay, var(--scrim));
	}

	.wlrow.fold:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.wlrow.fold.hairline {
		border-bottom: 1px solid color-mix(in oklch, var(--border) 72%, transparent);
		border-radius: 8px 8px 0 0;
	}

	@keyframes worklog-settle {
		from { opacity: 0; transform: translateY(2px); }
		to { opacity: 1; transform: translateY(0); }
	}

	@media (prefers-reduced-motion: reduce) {
		.worklog.is-settled { animation: none; }
	}

	@media (max-width: 639px) {
		.wlrow.fold {
			min-height: 44px;
		}
	}
</style>

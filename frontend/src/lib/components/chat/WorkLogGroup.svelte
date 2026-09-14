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
	import MediaGallery from './MediaGallery.svelte';
	import type { GalleryMedia } from '$lib/utils/media-gallery';
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
		/** Token-condensed variant shown instead of `summary` on narrow screens. */
		compactSummary?: string | null;
		gallery?: GalleryMedia[];
		row: Snippet<[ChatMessage, number, ReadonlySet<string>?]>;
	}

	let {
		mode,
		folded,
		tail,
		sequence = [],
		gallery = [],
		durationLabel = null,
		summary = null,
		compactSummary = null,
		row
	}: Props = $props();
	const retainedSequence = $derived(sequence.filter((entry) => !entry.hiddenWhenCollapsed));
	const gallerySources = $derived(new Set(gallery.map(item => item.src)));

	let open = $state(false);
	let previousMode = $state<'live' | 'settled' | null>(null);

	// Reserve space below the live group: the active tool row is replaced every
	// time the next call starts, and a shorter successor would drag the whole
	// transcript below it up and down for the length of the turn. Hold the
	// tallest height seen while live as a min-height; the reservation releases
	// when the turn settles.
	//
	// Measure an inner wrapper, not the section. The section carries the
	// min-height, so observing it can never see a shrink — collapse would
	// leave a hole. Skip the open fold: inspecting history is not a tool
	// swap, and that taller height must not become the floor. If the same
	// tail row shrinks (the user collapsed a tool or worker), drop the
	// floor to the new content height. A shorter successor with a new id
	// still keeps the peak, which is the swap-jitter case.
	let sectionEl = $state<HTMLElement | null>(null);
	let contentEl = $state<HTMLElement | null>(null);
	let reservedPx = $state(0);
	let reservedTailKey = '';

	function tailIdentity(entries: WorkLogEntry[]): string {
		return entries.map((entry) => entry.message.id ?? String(entry.index)).join('\0') || '∅';
	}

	$effect(() => {
		if (!contentEl || typeof ResizeObserver === 'undefined') return;
		const el = contentEl;
		const apply = () => {
			if (mode !== 'live' || open) return;
			const height = el.getBoundingClientRect().height;
			const key = tailIdentity(tail);
			if (height > reservedPx) {
				reservedPx = height;
				reservedTailKey = key;
			} else if (key === reservedTailKey && height < reservedPx) {
				reservedPx = height;
			}
		};
		const observer = new ResizeObserver(apply);
		observer.observe(el);
		apply();
		return () => observer.disconnect();
	});

	$effect(() => {
		if (mode !== 'live') {
			reservedPx = 0;
			reservedTailKey = '';
		}
	});

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

<section
	bind:this={sectionEl}
	class="worklog"
	class:is-settled={mode === 'settled'}
	style:min-height={mode === 'live' && reservedPx > 0 ? `${reservedPx}px` : undefined}
	aria-label="Work log"
	data-work-log-mode={mode}
>
	<div class="worklog-measure" bind:this={contentEl}>
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
			<span class="fold-label">
				{#if mode === 'live' && summary && compactSummary}
					<!-- Phones swap the prose sentence for its token-condensed form;
					     the title attribute keeps the full sentence reachable. -->
					<span class="fold-prose" title={summary}>{summary}</span>
					<span class="fold-compact">{compactSummary}</span>
				{:else}
					{collapsedLabel}
				{/if}
			</span>
			{#if mode === 'settled' && summary}
				<span class="fold-summary" title={summary}>· {#if compactSummary}<span class="fold-prose">{summary}</span><span class="fold-compact">{compactSummary}</span>{:else}{summary}{/if}</span>
			{/if}
		</button>
		{#if mode === 'settled' && sequence.length > 0}
			<div class="work-sequence is-collapsed">
				{#if gallery.length > 1}<MediaGallery items={gallery} />{/if}
				{#each retainedSequence as entry (entry.message.id ?? entry.index)}
					<div class="sequence-row" class:is-prose={isAssistantProse(entry)}>
						{@render row(entry.message, entry.index, gallerySources)}
					</div>
				{/each}
			</div>
		{:else}
			{#each tail as entry (entry.message.id ?? entry.index)}
				{@render row(entry.message, entry.index)}
			{/each}
		{/if}
	{/if}
	</div>
</section>

<style>
	.worklog {
		margin: 0;
		min-width: 0;
	}

	.worklog-measure {
		display: flex;
		flex-direction: column;
		gap: 1px;
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
		flex: 0 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
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


	/* The token-condensed form is the only summary a phone sees; the prose
	   sentence returns at tablet width. Both stay in the DOM so the title
	   tooltip keeps the full sentence reachable everywhere. */
	.fold-compact {
		display: none;
	}

	@media (max-width: 639px) {
		.fold-prose {
			display: none;
		}

		.fold-compact {
			display: inline;
		}
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

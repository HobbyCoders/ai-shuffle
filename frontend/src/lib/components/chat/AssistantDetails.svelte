<script lang="ts">
	/**
	 * AssistantDetails — the one "Details" disclosure under an assistant turn in
	 * the assistant's home. It holds that turn's tool rows, thinking, work-log
	 * rows, Task and swarm cards and notices, collapsed by default.
	 *
	 * Rows render through the caller's `row` snippet (MessageArea's dispatch
	 * table), so an opened Details shows exactly what an ordinary chat would.
	 */
	import type { Snippet } from 'svelte';
	import type { ChatMessage } from '$lib/stores/types';
	import type { WorkLogEntry } from '$lib/utils/work-log';

	interface Props {
		entries: WorkLogEntry[];
		/** The turn is still running: the toggle shows a quiet pulse. */
		live?: boolean;
		row: Snippet<[ChatMessage, number, ReadonlySet<string>?]>;
	}

	let { entries, live = false, row }: Props = $props();
	let open = $state(false);
	const id = $props.id();
	const count = $derived(entries.length);
	const countLabel = $derived(`${count} ${count === 1 ? 'step' : 'steps'}`);
</script>

<section class="assistant-details" class:open data-live={live || undefined} aria-label="Details">
	<button
		type="button"
		class="details-toggle"
		aria-expanded={open}
		aria-controls={id}
		onclick={() => (open = !open)}
	>
		<svg class="chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
			<path d="m9 6 6 6-6 6" />
		</svg>
		<span class="label">Details</span>
		<span class="count">{countLabel}</span>
		{#if live}<span class="pulse" aria-hidden="true"></span><span class="sr-only">, still working</span>{/if}
	</button>
	{#if open}
		<div class="details-body" {id}>
			{#each entries as entry (entry.message.id || entry.index)}
				<div class="details-row">{@render row(entry.message, entry.index)}</div>
			{/each}
		</div>
	{/if}
</section>

<style>
	.assistant-details {
		margin: -2px 0 6px;
		min-width: 0;
	}

	.details-toggle {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		min-height: 28px;
		padding: 3px 10px 3px 7px;
		border: 1px solid transparent;
		border-radius: 999px;
		background: transparent;
		color: var(--subtle-foreground);
		font-size: 12px;
		line-height: 1.4;
		cursor: pointer;
		transition: background-color 160ms ease-out, color 160ms ease-out, border-color 160ms ease-out;
	}

	.details-toggle:hover {
		background: var(--scrim);
		color: var(--foreground);
	}

	.details-toggle:active {
		background: var(--active-overlay, var(--scrim));
	}

	.details-toggle:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.open .details-toggle {
		border-color: color-mix(in oklch, var(--border) 80%, transparent);
		color: var(--muted-foreground);
	}

	.chev {
		flex-shrink: 0;
		transition: transform 180ms cubic-bezier(0.2, 0.8, 0.2, 1);
	}

	.open .chev {
		transform: rotate(90deg);
	}

	.label {
		font-weight: 550;
		letter-spacing: 0.01em;
	}

	.count {
		font-variant-numeric: tabular-nums;
		opacity: 0.8;
	}

	.count::before {
		content: '·';
		margin-right: 6px;
	}

	.pulse {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--primary);
		animation: details-pulse 1.4s ease-in-out infinite;
	}

	.details-body {
		display: flex;
		flex-direction: column;
		gap: 6px;
		margin: 6px 0 4px 12px;
		padding-left: 12px;
		border-left: 1px solid color-mix(in oklch, var(--border) 80%, transparent);
		animation: details-in 180ms ease-out both;
		min-width: 0;
	}

	.details-row {
		min-width: 0;
	}

	@keyframes details-pulse {
		0%, 100% { opacity: 0.35; transform: scale(0.85); }
		50% { opacity: 1; transform: scale(1); }
	}

	@keyframes details-in {
		from { opacity: 0; transform: translateY(-2px); }
		to { opacity: 1; transform: translateY(0); }
	}

	/* Touch: a 44px target around the same compact pill. */
	@media (pointer: coarse) {
		.details-toggle {
			position: relative;
			padding-inline: 10px 12px;
		}

		.details-toggle::after {
			content: '';
			position: absolute;
			inset: -8px -4px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.pulse, .details-body { animation: none; }
		.chev { transition: none; }
	}
</style>

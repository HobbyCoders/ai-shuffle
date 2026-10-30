<!-- Shared by cold history loads and the delayed switch overlay. A load the
     feed released without any history (#913) keeps this same slot and says so,
     with Retry: never an empty or cleared chat. -->
<script lang="ts">
	import { LoaderCircle, CloudOff, RotateCcw } from 'lucide-svelte';
	import { HISTORY_STALL_MESSAGE, type HistoryStallKind } from '$lib/components/layout/chat-empty-state';

	let { stall = null, onRetry }: {
		/** Why the history has not arrived, once the feed stopped waiting for it. */
		stall?: HistoryStallKind | null;
		/** Ask for the history again. */
		onRetry?: () => void;
	} = $props();
</script>

<div class="loading-transcript" class:is-stalled={!!stall} role="status" aria-live="polite" aria-atomic="true">
	{#if stall}
		<div class="stall-notice" data-history-stall={stall}>
			<span class="stall-icon" class:is-error={stall === 'error'} aria-hidden="true">
				{#if stall === 'error'}
					<CloudOff size={16} strokeWidth={1.75} />
				{:else}
					<LoaderCircle size={16} strokeWidth={1.75} class="stall-spin" />
				{/if}
			</span>
			<span class="stall-text">{HISTORY_STALL_MESSAGE[stall]}</span>
			{#if onRetry}
				<button type="button" class="stall-retry" aria-label="Retry loading the conversation" onclick={onRetry}>
					<RotateCcw size={14} strokeWidth={2} aria-hidden="true" />
					<span>Retry</span>
				</button>
			{/if}
		</div>
	{:else}
		<span class="sr-only">Loading conversation…</span>
	{/if}
	{#each [0, 1, 2] as group (group)}
		<div class="sk-turn" aria-hidden="true">
			<div class="sk-line sk-prompt"></div>
			<div class="sk-response">
				<div class="sk-line sk-name"></div>
				<div class="sk-line"></div>
				<div class="sk-line"></div>
				<div class="sk-line sk-short"></div>
			</div>
		</div>
	{/each}
</div>

<style>
	.loading-transcript {
		position: relative;
		width: 100%;
		max-width: 768px;
		margin: 0 auto;
		padding: 26px 24px 0;
		display: flex;
		flex-direction: column;
		gap: 22px;
	}
	.sk-turn { display: flex; flex-direction: column; gap: 12px; }
	.sk-response { display: flex; flex-direction: column; gap: 9px; }
	.sk-line {
		height: 12px;
		border-radius: 6px;
		background: color-mix(in oklch, var(--foreground) 9%, transparent);
		animation: sk-pulse 1.6s ease-in-out infinite;
		transition: opacity 240ms ease-out;
	}
	.sk-prompt {
		height: 34px;
		width: 46%;
		align-self: flex-end;
		border-radius: 14px;
		background: color-mix(in oklch, var(--foreground) 12%, transparent);
	}
	.sk-name { width: 118px; height: 10px; }
	.sk-short { width: 62%; }
	.sk-turn:nth-child(2) .sk-prompt { width: 34%; }
	.sk-turn:nth-child(3) .sk-prompt { width: 52%; }
	.sk-turn:nth-child(2) .sk-short { width: 44%; }
	.sk-turn:nth-child(2) .sk-line { animation-delay: 120ms; }
	.sk-turn:nth-child(3) .sk-line { animation-delay: 240ms; }
	@keyframes sk-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }

	/* Stalled: the skeleton recedes behind one floating line. Nothing here is
	   in flow, so the transcript slot keeps its exact geometry. */
	.is-stalled .sk-line { opacity: 0.4; animation: none; }
	.stall-notice {
		position: absolute;
		top: 50%;
		left: 50%;
		z-index: 1;
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
		max-width: calc(100% - 2rem);
		padding: 0.375rem 0.375rem 0.375rem 0.875rem;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--popover);
		color: var(--muted-foreground);
		box-shadow: var(--shadow-m);
		font-size: 0.8125rem;
		line-height: 1.3;
		white-space: nowrap;
		transform: translate(-50%, -50%);
		animation: stall-in 220ms cubic-bezier(0.16, 1, 0.3, 1);
	}
	.stall-icon { display: inline-flex; flex-shrink: 0; color: var(--subtle-foreground, var(--muted-foreground)); }
	.stall-icon.is-error { color: var(--warning, var(--muted-foreground)); }
	.stall-icon :global(.stall-spin) { animation: stall-spin 1s linear infinite; }
	.stall-text { overflow: hidden; text-overflow: ellipsis; color: var(--foreground); }
	.stall-retry {
		position: relative;
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		flex-shrink: 0;
		min-height: 2rem;
		padding: 0 0.75rem;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--foreground);
		color: var(--background);
		font: inherit;
		font-weight: 600;
		cursor: pointer;
		transition: opacity 150ms ease-out, transform 150ms ease-out;
	}
	/* 44px touch target around the compact pill button. */
	.stall-retry::before { content: ''; position: absolute; inset: -6px -4px; }
	.stall-retry:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.stall-retry:active { transform: scale(0.97); }
	@media (hover: hover) { .stall-retry:hover { opacity: 0.88; } }
	@keyframes stall-in {
		from { opacity: 0; transform: translate(-50%, calc(-50% + 4px)); }
		to { opacity: 1; transform: translate(-50%, -50%); }
	}
	@keyframes stall-spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) {
		.sk-line, .stall-notice, .stall-icon :global(.stall-spin) { animation: none; }
		.stall-retry:active { transform: none; }
	}
</style>

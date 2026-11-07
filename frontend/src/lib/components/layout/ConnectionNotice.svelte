<script lang="ts">
	/**
	 * The chat view's connection-lost notice: the quiet floating "Reconnecting…"
	 * pill, or the escalated warning banner with a Reconnect button (tiering in
	 * connection-notice.ts). Rendered inside ChatView's .message-area-wrapper,
	 * which anchors the pill.
	 *
	 * A tap answers at once: the control shows busy, says who it is connecting
	 * to, and ignores further taps until that handshake ends, so a slow server
	 * is not cut off mid-handshake. A failed tap escalates to the banner with a
	 * plain result and a second try; success hides the notice.
	 */
	import { LoaderCircle } from 'lucide-svelte';
	import Banner from '$lib/components/Banner.svelte';
	import NotificationSlot from '$lib/components/NotificationSlot.svelte';
	import { DOCK_ORDER } from '$lib/stores/notification-dock.svelte';
	import type { ManualReconnectState } from '$lib/stores/session/ws-connection';
	import { connectionNoticeCopy, type ConnectionNotice } from './connection-notice';

	let {
		tier,
		reconnectRequired,
		manual,
		workerName = null,
		onReconnect,
		pillHeight = $bindable(0)
	}: {
		tier: ConnectionNotice;
		reconnectRequired: boolean;
		manual: ManualReconnectState;
		workerName?: string | null;
		onReconnect: () => void;
		pillHeight?: number;
	} = $props();

	const copy = $derived(connectionNoticeCopy({ reconnectRequired, manual, workerName }));

	function tap() {
		if (!copy.busy) onReconnect();
	}
</script>

{#if tier === 'banner'}
	<!-- Escalated: the client gave up, the disconnect has lasted a long visible
	     time, or the user's own retry failed. The manual retry skips backoff. -->
	<NotificationSlot order={DOCK_ORDER.connection}>
		<div
			class="connection-banner"
			class:is-busy={copy.busy}
			class:is-failed={manual === 'failed'}
			data-manual-reconnect={manual}
		>
			<Banner
				severity="warning"
				variant="page"
				message={copy.message}
				action={{ label: copy.actionLabel, onClick: tap, busy: copy.busy }}
			/>
		</div>
	</NotificationSlot>
{/if}
<!-- Short drop: a quiet pill that floats over the transcript so nothing
     shifts. The live region stays mounted so screen readers announce it;
     tapping the pill retries now. -->
<div class="reconnect-status-slot" role="status" aria-live="polite">
	{#if tier === 'pill'}
		<button
			type="button"
			class="reconnect-pill"
			class:is-busy={copy.busy}
			title={copy.busy ? undefined : copy.pillHint}
			aria-disabled={copy.busy || undefined}
			aria-busy={copy.busy || undefined}
			data-manual-reconnect={manual}
			onclick={tap}
			bind:offsetHeight={pillHeight}
		>
			<LoaderCircle size={14} aria-hidden="true" class="reconnect-spin" />
			<span>{copy.pillLabel}</span>
			{#if copy.pillHint}<span class="reconnect-pill-retry">{copy.pillHint}</span>{/if}
		</button>
	{/if}
</div>

<style>
	/* ── Banner wrapper: progress sweep while busy, a nudge on failure ── */
	.connection-banner {
		position: relative;
		overflow: hidden;
		border-radius: var(--radius);
	}
	/* An indeterminate sweep along the top edge: the tap is being worked on,
	   even while a slow server takes seconds to answer. */
	.connection-banner.is-busy::after,
	.reconnect-pill.is-busy::after {
		content: '';
		position: absolute;
		left: 0;
		width: 40%;
		height: 2px;
		pointer-events: none;
		background: linear-gradient(90deg, transparent, var(--warning), transparent);
		animation: reconnect-sweep 1.2s ease-in-out infinite;
	}
	.connection-banner.is-busy::after { top: 0; }
	.connection-banner.is-failed {
		animation: reconnect-nudge 360ms ease-out;
	}

	/* ── Pill: floats over the top of .message-area-wrapper in the mirror sync
	   pill's visual language; empty while connected. ── */
	.reconnect-status-slot {
		position: absolute;
		top: 0.5rem;
		left: 0.75rem;
		right: 0.75rem;
		z-index: 6;
		display: flex;
		justify-content: center;
		pointer-events: none;
	}

	.reconnect-pill {
		pointer-events: auto;
		position: relative;
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
		max-width: 100%;
		min-height: 2rem;
		padding: 0.375rem 0.75rem;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--popover);
		color: var(--muted-foreground);
		box-shadow: var(--shadow-m);
		font: inherit;
		font-size: 0.8125rem;
		line-height: 1.3;
		white-space: nowrap;
		cursor: pointer;
		animation: reconnect-pill-in 160ms ease-out;
		transition: color 150ms ease-out, border-color 150ms ease-out, background-color 150ms ease-out;
	}
	/* Invisible hit area: keeps the pill visually compact while giving touch a
	   44px-tall target (2rem pill + 6px above and below). */
	.reconnect-pill::before {
		content: '';
		position: absolute;
		inset: -6px -4px;
	}
	.reconnect-pill :global(svg) { flex-shrink: 0; }
	.reconnect-pill :global(.reconnect-spin) { animation: reconnect-spin 1s linear infinite; }
	/* The retry hint stays out of the way until hover/focus. Hover only applies
	   on devices that really hover, so a tap on a phone doesn't leave it stuck open. */
	.reconnect-pill-retry {
		max-width: 0;
		overflow: hidden;
		opacity: 0;
		color: var(--foreground);
		transition: max-width 200ms ease-out, opacity 150ms ease-out;
	}
	.reconnect-pill:focus-visible {
		color: var(--foreground);
		border-color: color-mix(in oklch, var(--foreground) 25%, var(--border));
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
	.reconnect-pill:focus-visible .reconnect-pill-retry {
		max-width: 6rem;
		opacity: 1;
	}
	@media (hover: hover) {
		.reconnect-pill:not(.is-busy):hover {
			color: var(--foreground);
			border-color: color-mix(in oklch, var(--foreground) 25%, var(--border));
		}
		.reconnect-pill:hover .reconnect-pill-retry {
			max-width: 6rem;
			opacity: 1;
		}
	}
	.reconnect-pill:not(.is-busy):active { transform: scale(0.97); }
	/* The user's tap is running: foreground text, warning-tinted edge and a
	   faster spinner, so it reads as "working on it", not idle retrying. */
	.reconnect-pill.is-busy {
		overflow: hidden;
		cursor: progress;
		color: var(--foreground);
		border-color: color-mix(in oklab, var(--warning) 55%, transparent);
		background: color-mix(in oklab, var(--warning) 8%, var(--popover));
	}
	.reconnect-pill.is-busy::after { bottom: 0; }
	.reconnect-pill.is-busy :global(.reconnect-spin) {
		color: var(--warning);
		animation-duration: 0.7s;
	}

	@keyframes reconnect-spin { to { transform: rotate(360deg); } }
	@keyframes reconnect-pill-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
	@keyframes reconnect-sweep {
		from { transform: translateX(-100%); }
		to { transform: translateX(250%); }
	}
	@keyframes reconnect-nudge {
		0%, 100% { transform: none; }
		25% { transform: translateX(-4px); }
		60% { transform: translateX(3px); }
	}
	@media (prefers-reduced-motion: reduce) {
		.reconnect-pill,
		.reconnect-pill :global(.reconnect-spin),
		.reconnect-pill-retry,
		.connection-banner.is-failed { animation: none; transition: none; }
		.reconnect-pill:not(.is-busy):active { transform: none; }
		/* Keep a still progress cue instead of the sweep. */
		.connection-banner.is-busy::after,
		.reconnect-pill.is-busy::after {
			animation: none;
			width: 100%;
			opacity: 0.6;
			background: var(--warning);
		}
	}
</style>

<script lang="ts">
	/**
	 * PendingMessages — prompts the user has already sent but the model has not
	 * seen yet.
	 *
	 * There is no send-later fork any more: a send during a live turn always goes
	 * out, and the backend decides when it lands (mid-turn steer, or the start of
	 * the next turn). Until it does, the message renders here as a normal
	 * right-aligned user bubble pinned below the transcript, so the chat reads as
	 * one continuous conversation instead of a separate queue tray.
	 *
	 * The bubble styling deliberately mirrors UserMessage's `.userrow`/`.bubble`
	 * (same padding, radius, surface) but carries none of its machinery — a
	 * pending prompt has no timestamp, no rewind target and no copy actions.
	 *
	 * Under the last ACTIONABLE bubble (no status) sits one quiet action row: the
	 * "Not seen yet" hint, an Interrupt link that stops the current turn (which
	 * delivers the message on the next one), and an "×" that unsends it and puts
	 * its text back in the composer to edit. Other actionable bubbles get the same
	 * "×" on hover. Interrupt is the only queue-preserving way to deliver what is
	 * still pending, so a cancelling or already-sent tail never hides it.
	 *
	 * An unsend is a request, not a fact (#2275): while the server decides, the
	 * bubble reads "Cancelling…" and its controls are disabled; if the agent had
	 * already read it, it reads "Already sent" and loses its controls. The text
	 * only returns to the composer on a confirmed withdrawal (store-side).
	 * Screen-reader announcements of those verdicts live in
	 * PendingStatusAnnouncer, outside the transcript's log region.
	 */
	import type { QueuedMessage } from '$lib/stores/types';
	import { shouldCollapseUserMessage } from '$lib/utils/message-presentation';

	interface Props {
		messages: QueuedMessage[];
		/** Unsend one pending message (server-side removal); its text returns to the composer. */
		onRemove: (id: string) => void;
		/** Stop the live turn so the pending message is delivered next. */
		onInterrupt: () => void;
	}

	let { messages, onRemove, onInterrupt }: Props = $props();

	// The action row's bubble: the last one without a status, or — when every
	// bubble has one — the last bubble (whose row then shows its status only).
	const anchor = $derived.by(() => {
		for (let i = messages.length - 1; i >= 0; i--) if (!messages[i].status) return i;
		return messages.length - 1;
	});

	const STATUS_LABEL = { cancelling: 'Cancelling…', delivered: 'Already sent' } as const;

	// Svelte delegates clicks at the root, where a synthetic click on a disabled
	// button still arrives — so the guard lives here too, not only in `disabled`.
	function unsend(msg: QueuedMessage) {
		if (!msg.status) onRemove(msg.id);
	}

	function interrupt(msg: QueuedMessage) {
		if (!msg.status) onInterrupt();
	}
</script>

{#if messages.length > 0}
	<div class="pending-messages">
		{#each messages as msg, i (msg.id)}
			{@const isAnchor = i === anchor}
			<div class="userrow" role="article">
				<div class="usercol">
					<div class="bubble" class:is-cancelling={msg.status === 'cancelling'}>
						<div class="bubble-text" class:is-clamped={shouldCollapseUserMessage(msg.content)}>{msg.content}</div>
						{#if !isAnchor && msg.status !== 'delivered'}
							<button
								type="button"
								class="hover-remove"
								aria-label="Unsend message and edit it"
								title={msg.status === 'cancelling' ? 'Cancelling…' : 'Unsend and edit'}
								disabled={msg.status === 'cancelling'}
								onclick={() => unsend(msg)}
							>×</button>
						{/if}
					</div>
					{#if isAnchor || msg.status}
						<div class="pending-actions">
							{#if msg.status}
								<span
									class="pending-status"
									class:is-delivered={msg.status === 'delivered'}
								>{STATUS_LABEL[msg.status]}</span>
							{:else}
								<span class="pending-hint">Not seen yet</span>
							{/if}
							{#if isAnchor && msg.status !== 'delivered'}
								<span class="pending-sep" aria-hidden="true">·</span>
								<button
									type="button"
									class="pending-action"
									disabled={msg.status === 'cancelling'}
									onclick={() => interrupt(msg)}
								>Interrupt</button>
								<button
									type="button"
									class="pending-remove"
									aria-label="Unsend message and edit it"
									title={msg.status === 'cancelling' ? 'Cancelling…' : 'Unsend and edit'}
									disabled={msg.status === 'cancelling'}
									onclick={() => unsend(msg)}
								>×</button>
							{/if}
						</div>
					{/if}
				</div>
			</div>
		{/each}
	</div>
{/if}

<style>
	.pending-messages {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	/* ── Bubble: same values as UserMessage's .userrow / .usercol / .bubble ── */
	.userrow {
		display: flex;
		justify-content: flex-end;
		width: 100%;
		min-width: 0;
	}

	.usercol {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		max-width: 80%;
		min-width: 0;
	}

	.bubble {
		position: relative;
		padding: 10px 14px;
		border-radius: 16px;
		background: var(--card);
		border: 1px solid var(--border);
		color: var(--card-foreground);
		font-size: 14px;
		line-height: 1.58;
		min-width: 0;
	}

	.bubble-text {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	/* Long prompts are clamped rather than given disclosure chrome — a pending
	   bubble is transient, and the full text is still in the transcript once it
	   lands. */
	.bubble-text.is-clamped {
		max-height: calc(1.58em * 8);
		overflow: hidden;
		-webkit-mask-image: linear-gradient(to bottom, black 78%, transparent 100%);
		mask-image: linear-gradient(to bottom, black 78%, transparent 100%);
	}

	:global(html.light) .bubble {
		background: var(--muted);
	}

	/* ── Hover "×" on every bubble but the last (which owns the action row) ── */
	.hover-remove {
		position: absolute;
		top: -6px;
		left: -8px;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: 50%;
		background: var(--card);
		color: var(--muted-foreground);
		font-size: 12px;
		line-height: 1;
		cursor: pointer;
		opacity: 0;
		pointer-events: none;
		transition: opacity 0.12s ease, color 0.12s ease;
	}

	.bubble:hover .hover-remove,
	.hover-remove:focus-visible {
		opacity: 1;
		pointer-events: auto;
	}

	.hover-remove:hover:not(:disabled) {
		color: var(--foreground);
	}

	/* ── Unsend in flight / refused ── */
	.bubble.is-cancelling .bubble-text {
		opacity: 0.6;
	}

	.pending-status.is-delivered {
		color: var(--warning);
	}

	/* Light --warning is ~3.6:1 on the page at this 11px size; pulling it toward
	   the foreground keeps the hue and clears 4.5:1. Dark --warning already does. */
	:global(html.light) .pending-status.is-delivered {
		color: color-mix(in oklab, var(--warning) 72%, var(--foreground));
	}

	.hover-remove:disabled,
	.pending-action:disabled,
	.pending-remove:disabled {
		cursor: not-allowed;
	}

	.pending-action:disabled,
	.pending-remove:disabled,
	.bubble:hover .hover-remove:disabled {
		opacity: 0.45;
	}

	/* ── The quiet metadata row under the last bubble ── */
	.pending-actions {
		display: flex;
		align-items: center;
		gap: 5px;
		margin-top: 4px;
		font-size: 11px;
		color: var(--subtle-foreground);
	}

	.pending-sep {
		color: var(--subtle-foreground);
	}

	.pending-action,
	.pending-remove {
		padding: 0 2px;
		border: none;
		border-radius: 0.25rem;
		background: none;
		font: inherit;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 0.15s ease;
	}

	.pending-remove {
		font-size: 13px;
		line-height: 1;
	}

	.pending-action:hover:not(:disabled),
	.pending-remove:hover:not(:disabled) {
		color: var(--foreground);
	}

	.pending-action:focus-visible,
	.pending-remove:focus-visible,
	.hover-remove:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	@media (max-width: 639px) {
		.usercol {
			max-width: 85%;
		}

		/* No hover on touch — keep the per-bubble control reachable. */
		.hover-remove {
			opacity: 1;
			pointer-events: auto;
		}

		.hover-remove:disabled {
			opacity: 0.45;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.hover-remove,
		.pending-action,
		.pending-remove { transition: none; }
	}
</style>

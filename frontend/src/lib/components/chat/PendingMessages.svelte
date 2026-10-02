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
	 * Under the LAST bubble sits one quiet action row: the "Not seen yet" hint,
	 * an Interrupt link that stops the current turn (which delivers the message
	 * on the next one), and an "×" that unsends it and puts its text back in the
	 * composer to edit. Earlier bubbles get the same "×" on hover.
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
</script>

{#if messages.length > 0}
	<div class="pending-messages">
		{#each messages as msg, i (msg.id)}
			<div class="userrow" role="article">
				<div class="usercol">
					<div class="bubble">
						<div class="bubble-text" class:is-clamped={shouldCollapseUserMessage(msg.content)}>{msg.content}</div>
						{#if i !== messages.length - 1}
							<button
								type="button"
								class="hover-remove"
								aria-label="Unsend message and edit it"
								title="Unsend and edit"
								onclick={() => onRemove(msg.id)}
							>×</button>
						{/if}
					</div>
					{#if i === messages.length - 1}
						<div class="pending-actions">
							<span class="pending-hint">Not seen yet</span>
							<span class="pending-sep" aria-hidden="true">·</span>
							<button type="button" class="pending-action" onclick={onInterrupt}>Interrupt</button>
							<button
								type="button"
								class="pending-remove"
								aria-label="Unsend message and edit it"
								title="Unsend and edit"
								onclick={() => onRemove(msg.id)}
							>×</button>
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

	.hover-remove:hover {
		color: var(--foreground);
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

	.pending-action:hover,
	.pending-remove:hover {
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
	}

	@media (prefers-reduced-motion: reduce) {
		.hover-remove,
		.pending-action,
		.pending-remove { transition: none; }
	}
</style>

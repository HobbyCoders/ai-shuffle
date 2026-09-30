<script lang="ts">
	/** Read-only peer queue: never a user prompt and never an interrupt/unsend action. */
	import { Clock3, MessagesSquare } from 'lucide-svelte';
	import { shouldCollapseUserMessage } from '$lib/utils/message-presentation';

	interface Props {
		messages: ReadonlyArray<{
			id: string;
			content: string;
			senderLabel: string;
		}>;
	}

	let { messages }: Props = $props();
</script>

{#if messages.length > 0}
	<section class="pending-peers" aria-label="Pending peer messages">
		{#each messages as message (message.id)}
			<article class="peer-message" aria-label={`Queued peer message from ${message.senderLabel || 'another session'}`}>
				<header class="peer-heading">
					<MessagesSquare size={14} aria-hidden="true" class="peer-icon" />
					<span class="peer-kind">Agent message</span>
					<span class="peer-status"><Clock3 size={12} aria-hidden="true" />Queued</span>
				</header>
				<div class="peer-body">
					<p class="peer-sender">From <strong>{message.senderLabel || 'Another session'}</strong></p>
					{#if shouldCollapseUserMessage(message.content)}
						<details>
							<summary>Read queued message</summary>
							<p class="peer-content">{message.content}</p>
						</details>
					{:else}
						<p class="peer-content">{message.content}</p>
					{/if}
					<p class="peer-hint">Waiting for recipient · Not delivered yet</p>
				</div>
			</article>
		{/each}
	</section>
{/if}

<style>
	.pending-peers {
		display: grid;
		gap: 16px;
		min-width: 0;
		margin-block: 12px;
	}

	.peer-message { min-width: 0; }

	.peer-heading {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 8px;
		color: var(--foreground);
		font-size: 12px;
		line-height: 1.5;
	}

	.peer-heading :global(.peer-icon) {
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.peer-kind { font-weight: 600; }

	.peer-status {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		margin-left: auto;
		color: var(--muted-foreground);
		font-family: var(--font-mono);
		font-size: 10px;
	}

	.peer-body {
		margin: 6px 0 0 6px;
		padding-left: 16px;
		border-left: 1px dashed var(--border-strong);
		min-width: 0;
	}

	.peer-sender {
		margin: 0 0 6px;
		color: var(--muted-foreground);
		font-size: 11px;
		line-height: 1.5;
		overflow-wrap: anywhere;
	}

	.peer-sender strong { font-weight: 500; color: var(--foreground); }

	.peer-content {
		margin: 0;
		color: var(--foreground);
		font-size: 13px;
		line-height: 1.6;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}

	.peer-hint {
		margin: 8px 0 0;
		color: var(--muted-foreground);
		font-size: 11px;
		line-height: 1.5;
	}

	summary {
		width: fit-content;
		max-width: 100%;
		padding: 6px 8px;
		margin-left: -8px;
		border-radius: 4px;
		color: var(--muted-foreground);
		font-size: 12px;
		cursor: pointer;
		transition: background 0.2s ease-out, color 0.2s ease-out;
	}

	summary:hover { background: var(--accent); color: var(--foreground); }
	summary:active { background: var(--muted); }
	summary:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	details[open] .peer-content { margin-top: 6px; }

	@media (max-width: 639px) {
		summary { min-height: 44px; padding-block: 12px; }
	}

	@media (prefers-reduced-motion: reduce) {
		summary { transition: none; }
	}
</style>

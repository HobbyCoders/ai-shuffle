<script lang="ts">
	import { activeSession } from '$lib/stores/active-session';

	function removeMessage(messageId: string) {
		activeSession.removeQueuedMessage(messageId);
	}

	function clearAll() {
		activeSession.clearQueuedMessages();
	}

	function truncate(text: string, maxLen: number = 60): string {
		if (text.length <= maxLen) return text;
		return text.substring(0, maxLen) + '...';
	}
</script>

{#if $activeSession.queuedMessages && $activeSession.queuedMessages.length > 0}
	<div class="queued-bar">
		<div class="queued-bar-inner">
			<div class="queued-header">
				<svg class="w-3 h-3 queue-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
				</svg>
				<span class="queued-label">Queued ({$activeSession.queuedMessages.length})</span>
				{#if $activeSession.queuedMessages.length > 1}
					<button class="clear-all-btn" onclick={clearAll} title="Clear all queued messages">
						Clear all
					</button>
				{/if}
			</div>
			<div class="queued-chips">
				{#each $activeSession.queuedMessages as msg (msg.id)}
					<div class="queued-chip">
						<span class="chip-text">{truncate(msg.content)}</span>
						<button class="chip-remove" onclick={() => removeMessage(msg.id)} title="Remove from queue">
							<svg class="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 18L18 6M6 6l12 12" />
							</svg>
						</button>
					</div>
				{/each}
			</div>
		</div>
	</div>
{/if}

<style>
	.queued-bar {
		padding: 0 0.75rem;
		flex-shrink: 0;
	}

	@media (min-width: 640px) {
		.queued-bar {
			padding: 0 1rem;
		}
	}

	.queued-bar-inner {
		max-width: 56rem;
		margin: 0 auto;
		padding: 0.375rem 0.625rem;
		background: color-mix(in oklch, var(--card) 80%, transparent);
		border: 1px solid var(--border-subtle);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
	}

	.queued-header {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.queue-icon {
		flex-shrink: 0;
		color: var(--muted-foreground);
	}

	.queued-label {
		font-weight: 500;
	}

	.clear-all-btn {
		margin-left: auto;
		font-size: 0.625rem;
		color: var(--muted-foreground);
		background: none;
		border: none;
		cursor: pointer;
		padding: 0.125rem 0.25rem;
		border-radius: 0.25rem;
		transition: color 0.15s, background 0.15s;
	}

	.clear-all-btn:hover {
		color: var(--destructive);
		background: color-mix(in srgb, var(--destructive) 10%, transparent);
	}

	.queued-chips {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem;
	}

	.queued-chip {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		padding: 0.1875rem 0.375rem;
		background: var(--muted);
		border: 1px solid var(--border-subtle);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		font-size: 0.6875rem;
		color: var(--foreground);
		max-width: 100%;
	}

	.chip-text {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 24rem;
	}

	.chip-remove {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		padding: 15px;
		background: none;
		border: none;
		border-radius: 50%;
		cursor: pointer;
		color: var(--muted-foreground);
		transition: color 0.15s, background 0.15s;
		margin: -15px -15px -15px 0;
	}

	.chip-remove:hover {
		color: var(--destructive);
		background: color-mix(in srgb, var(--destructive) 15%, transparent);
	}
</style>

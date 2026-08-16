<script lang="ts">
	/**
	 * QueuedMessages — tray listing user messages queued to send after the
	 * current turn. Sits above the composer, mirroring the Background/ActiveTasks
	 * tray: the header row is a collapse toggle (chevron at the right edge) that
	 * shrinks the tray to just its header. The "Queued (N)" count stays visible
	 * when collapsed.
	 *
	 * The collapsed state is a chat preference (localStorage + cross-device server
	 * sync — the same store the background-tasks tray uses) so it persists across
	 * turns, reloads, and devices.
	 */
	import { get } from 'svelte/store';
	import { activeSession } from '$lib/stores/active-session';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { queuedMessageEdit } from '$lib/stores/queued-message-edit';

	// Persisted via the chat-preferences store (instant localStorage + debounced
	// server sync), so the minimized state survives reloads and follows the user
	// across devices — identical to the background-tasks tray.
	const collapsed = $derived(chatPreferences.current.queuedMessagesCollapsed);

	// Which chip (if any) is expanded to show its full text + actions. Only one
	// at a time so the tray never balloons.
	let expandedId = $state<string | null>(null);

	function toggleCollapsed() {
		chatPreferences.setQueuedMessagesCollapsed(!collapsed);
	}

	function removeMessage(messageId: string) {
		activeSession.removeQueuedMessage(messageId);
	}

	function editMessage(msg: { id: string; content: string }) {
		// Non-destructive: the queued row STAYS in the queue. ChatInput owns the
		// lifecycle — it marks the edit in the shared store and removes the queued
		// copy only once the edited message is actually sent.
		window.dispatchEvent(
			new CustomEvent('queued-message-edit', { detail: { id: msg.id, content: msg.content } })
		);
		// dispatchEvent runs ChatInput's listener synchronously — including any
		// window.confirm it shows — so by the time we get here the store already
		// reflects whether the user accepted or cancelled the edit.
		if (get(queuedMessageEdit)?.id === msg.id) expandedId = null;
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
				<button
					type="button"
					class="queued-toggle"
					onclick={toggleCollapsed}
					aria-expanded={!collapsed}
					title={collapsed ? 'Expand queued messages' : 'Minimize queued messages'}
					aria-label={collapsed ? 'Expand queued messages' : 'Minimize queued messages'}
				>
					<svg class="w-3 h-3 queue-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
					</svg>
					<span class="queued-label">Queued ({$activeSession.queuedMessages.length})</span>
				</button>
				{#if !collapsed && $activeSession.queuedMessages.length > 1}
					<button type="button" class="clear-all-btn" onclick={clearAll} title="Clear all queued messages">
						Clear all
					</button>
				{/if}
				<button
					type="button"
					class="queued-toggle chevron-btn"
					onclick={toggleCollapsed}
					aria-expanded={!collapsed}
					aria-label={collapsed ? 'Expand queued messages' : 'Minimize queued messages'}
				>
					<svg class="chevron" class:is-collapsed={collapsed} fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7" />
					</svg>
				</button>
			</div>
			{#if !collapsed}
				<div class="queued-chips">
					{#each $activeSession.queuedMessages as msg (msg.id)}
						<div class="queued-chip" class:is-expanded={expandedId === msg.id}>
							<div class="chip-head">
								<button
									type="button"
									class="chip-main"
									aria-expanded={expandedId === msg.id}
									title="Show message"
									onclick={() => (expandedId = expandedId === msg.id ? null : msg.id)}
								>
									<span class="chip-text">{truncate(msg.content)}</span>
									{#if $queuedMessageEdit?.id === msg.id}
										<span
											class="chip-editing-badge"
											title="Loaded in the composer — will be replaced when you send"
										>editing</span>
									{/if}
								</button>
								<button
									type="button"
									class="chip-remove"
									aria-label="Remove from queue"
									title="Remove from queue"
									onclick={() => removeMessage(msg.id)}
								>
									<svg class="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 18L18 6M6 6l12 12" />
									</svg>
								</button>
							</div>
							{#if expandedId === msg.id}
								<div class="chip-full">{msg.content}</div>
								<div class="chip-actions">
									<button type="button" class="chip-action" onclick={() => editMessage(msg)}>Edit</button>
									<button type="button" class="chip-action is-destructive" onclick={() => removeMessage(msg.id)}>Remove</button>
								</div>
							{/if}
						</div>
					{/each}
				</div>
			{/if}
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
		background: color-mix(in oklch, var(--card) 80%, transparent);
		border: 1px solid var(--border-subtle);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		overflow: hidden;
		display: flex;
		flex-direction: column;
	}

	.queued-header {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.375rem 0.625rem;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	/* The label area (and the chevron) are collapse toggles — click either to
	   minimize/expand, mirroring the background-tasks tray. */
	.queued-toggle {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		flex: 1 1 auto;
		min-width: 0;
		padding: 0;
		border: none;
		background: transparent;
		font: inherit;
		color: inherit;
		text-align: left;
		cursor: pointer;
		transition: color 0.12s ease;
	}

	.queued-toggle:hover {
		color: var(--foreground);
	}

	.queued-toggle:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
		border-radius: 0.25rem;
	}

	.chevron-btn {
		flex: 0 0 auto;
	}

	.queue-icon {
		flex-shrink: 0;
		color: var(--muted-foreground);
	}

	.queued-label {
		font-weight: 500;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Expanded → chevron points up (collapse). Collapsed → points down (expand). */
	.chevron {
		flex-shrink: 0;
		width: 0.875rem;
		height: 0.875rem;
		transform: rotate(180deg);
		transition: transform 0.15s ease;
	}

	.chevron.is-collapsed {
		transform: rotate(0deg);
	}

	.clear-all-btn {
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
		padding: 0 0.625rem 0.375rem;
	}

	.queued-chip {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		padding: 0.25rem 0.5rem;
		background: var(--muted);
		border: 1px solid var(--border-subtle);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		font-size: 0.6875rem;
		font-family: inherit;
		color: var(--foreground);
		max-width: 100%;
		transition: border-color 0.15s, background 0.15s;
	}

	.queued-chip:hover {
		border-color: var(--border);
		background: color-mix(in srgb, var(--muted) 80%, var(--foreground) 5%);
	}

	/* Expanded chips take the full tray width so the message body is readable. */
	.queued-chip.is-expanded {
		flex: 1 1 100%;
	}

	.chip-head {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		min-width: 0;
	}

	.chip-main {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		flex: 1 1 auto;
		min-width: 0;
		padding: 0;
		border: none;
		background: transparent;
		font: inherit;
		color: inherit;
		text-align: left;
		cursor: pointer;
	}

	.chip-main:focus-visible,
	.chip-remove:focus-visible,
	.chip-action:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
		border-radius: 0.25rem;
	}

	.chip-text {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 24rem;
	}

	.chip-editing-badge {
		flex-shrink: 0;
		font-size: 0.625rem;
		line-height: 1;
		padding: 0.125rem 0.25rem;
		border-radius: 0.25rem;
		border: 1px solid var(--border-subtle);
		color: var(--muted-foreground);
		background: color-mix(in srgb, var(--foreground) 6%, transparent);
	}

	.chip-remove {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1rem;
		height: 1rem;
		padding: 0;
		border: none;
		background: transparent;
		border-radius: 50%;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 0.15s, background 0.15s;
	}

	.queued-chip:hover .chip-remove {
		color: var(--destructive);
		background: color-mix(in srgb, var(--destructive) 15%, transparent);
	}

	.chip-full {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		max-height: 10rem;
		overflow-y: auto;
		text-align: left;
		font-size: 0.6875rem;
		line-height: 1.4;
		color: var(--muted-foreground);
		border-top: 1px solid var(--border-subtle);
		padding-top: 0.25rem;
	}

	.chip-actions {
		display: flex;
		gap: 0.25rem;
	}

	.chip-action {
		font-size: 0.625rem;
		font-family: inherit;
		color: var(--muted-foreground);
		background: none;
		border: none;
		cursor: pointer;
		padding: 0.125rem 0.25rem;
		border-radius: 0.25rem;
		transition: color 0.15s, background 0.15s;
	}

	.chip-action:hover {
		color: var(--foreground);
		background: color-mix(in srgb, var(--foreground) 8%, transparent);
	}

	.chip-action.is-destructive:hover {
		color: var(--destructive);
		background: color-mix(in srgb, var(--destructive) 10%, transparent);
	}
</style>

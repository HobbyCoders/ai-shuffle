<script lang="ts">
	/**
	 * QueuedMessages — the queue chips that sit directly above the composer
	 * island, listing user messages queued to send after the current turn. Each
	 * chip is the mockup's blue-tinted `.queued` pill (clock glyph + preview +
	 * remove X); clicking one expands it to the full text with Edit / Remove.
	 *
	 * A thin header line above the chips carries the "Queued (N)" count, the
	 * "Clear all" action and the collapse toggle (chevron at the right edge) that
	 * shrinks the stack to just that line.
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
								<svg class="chip-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
									<circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" />
								</svg>
								<button
									type="button"
									class="chip-main"
									aria-expanded={expandedId === msg.id}
									title="Show message"
									onclick={() => (expandedId = expandedId === msg.id ? null : msg.id)}
								>
									<span class="chip-text">{truncate(msg.content, 90)}</span>
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
	/* Aligned to the composer dock below it: same gutters, same 768px measure. */
	.queued-bar {
		padding: 0 0.75rem;
		flex-shrink: 0;
	}

	@media (min-width: 640px) {
		.queued-bar {
			padding: 0 1.5rem;
		}
	}

	/* No card of its own — the chips carry the surface, as in the mockup. */
	.queued-bar-inner {
		max-width: 48rem;
		margin: 0 auto;
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding-bottom: 8px;
	}

	.queued-header {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0 2px;
		font-size: 11px;
		color: var(--subtle-foreground);
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
		color: var(--blue);
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
		font-size: 11px;
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

	/* One chip per row, full width — the mockup stacks them above the island. */
	.queued-chips {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	/* The mockup's `.queued` pill: blue-tinted, fully rounded. It squares off a
	   little when expanded so the message body has proper corners to sit in. */
	.queued-chip {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
		width: 100%;
		padding: 5px 6px 5px 10px;
		background: color-mix(in oklch, var(--blue) 12%, transparent);
		border: 1px solid color-mix(in oklch, var(--blue) 28%, transparent);
		border-radius: 999px;
		font-size: 12px;
		font-family: inherit;
		color: var(--muted-foreground);
		max-width: 100%;
		transition: border-color 0.15s, background 0.15s, border-radius 0.15s;
	}

	.queued-chip:hover {
		border-color: color-mix(in oklch, var(--blue) 45%, transparent);
	}

	.queued-chip.is-expanded {
		border-radius: 14px;
		padding: 7px 8px 8px 12px;
	}

	.chip-head {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}

	.chip-icon {
		flex-shrink: 0;
		color: var(--blue);
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
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--foreground);
	}

	.chip-editing-badge {
		flex-shrink: 0;
		font-size: 10px;
		line-height: 1;
		padding: 0.1875rem 0.3125rem;
		border-radius: 999px;
		border: 1px solid var(--border);
		color: var(--subtle-foreground);
		background: var(--scrim);
	}

	.chip-remove {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		padding: 0;
		border: none;
		background: transparent;
		border-radius: 50%;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 0.13s, background 0.13s;
	}

	.chip-remove:hover {
		color: var(--foreground);
		background: var(--scrim);
	}

	.chip-full {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		max-height: 10rem;
		overflow-y: auto;
		text-align: left;
		font-size: 11.5px;
		line-height: 1.5;
		color: var(--muted-foreground);
		border-top: 1px solid color-mix(in oklch, var(--blue) 22%, transparent);
		padding-top: 0.375rem;
	}

	.chip-actions {
		display: flex;
		gap: 0.25rem;
	}

	.chip-action {
		font-size: 11px;
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

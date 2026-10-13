<script lang="ts">
	/**
	 * Stands in for the New Chat hero when the chat the user asked for could not
	 * be opened (a deep link with an unknown or short id, a deleted chat, or one
	 * this viewer may not read). The composer below is already a new chat; this
	 * says why, and the action puts the ordinary New Chat hero back.
	 */
	import { SearchX, Lock } from 'lucide-svelte';
	import { unavailableChatCopy, type UnavailableChat } from '$lib/stores/unavailable-chat';

	let { chat, onDismiss }: { chat: UnavailableChat; onDismiss: () => void } = $props();

	const copy = $derived(unavailableChatCopy(chat));
</script>

<div class="unavailable" role="alert" aria-labelledby="unavailable-title">
	<div class="badge" aria-hidden="true">
		{#if chat.status === 403}<Lock size={22} strokeWidth={1.5} />{:else}<SearchX size={22} strokeWidth={1.5} />{/if}
	</div>
	<h1 id="unavailable-title" class="title">{copy.title}</h1>
	<p class="detail">{copy.detail}</p>
	<p class="ref">Link: <code title={chat.sessionId}>{chat.sessionId}</code></p>
	<div class="actions">
		<button type="button" class="primary" onclick={onDismiss}>Start a new chat</button>
	</div>
	<p class="hint">Or pick a chat from your list.</p>
</div>

<style>
	.unavailable {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 8px;
		padding: 2rem 1.5rem 1.25rem;
		text-align: center;
		animation: rise 260ms cubic-bezier(0.16, 1, 0.3, 1);
	}
	.badge {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		margin-bottom: 10px;
		border-radius: 50%;
		color: var(--warning, var(--subtle-foreground));
		background: var(--scrim);
		border: 1px solid var(--border);
	}
	.title {
		margin: 0;
		font-family: var(--font-display, Georgia, serif);
		font-size: clamp(1.375rem, 4vw, 2rem);
		font-weight: 700;
		letter-spacing: -0.02em;
		line-height: 1.15;
		color: var(--foreground);
	}
	.detail {
		margin: 0;
		max-width: 44ch;
		font-size: 0.875rem;
		line-height: 1.5;
		color: var(--muted-foreground);
	}
	.ref {
		margin: 0;
		max-width: 100%;
		font-size: 0.75rem;
		color: var(--subtle-foreground, var(--muted-foreground));
	}
	.ref code {
		display: inline-block;
		max-width: min(36ch, 70vw);
		overflow: hidden;
		text-overflow: ellipsis;
		vertical-align: bottom;
		white-space: nowrap;
		padding: 1px 6px;
		border-radius: 6px;
		background: var(--muted);
		font-family: var(--font-mono, ui-monospace, monospace);
	}
	.actions {
		display: flex;
		gap: 8px;
		margin-top: 8px;
	}
	.primary {
		min-height: 36px;
		padding: 8px 16px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.66);
		background: var(--foreground);
		color: var(--background);
		font-size: 0.875rem;
		font-weight: 600;
		cursor: pointer;
		transition: opacity 0.2s ease-out, transform 0.2s ease-out;
	}
	.primary:hover { opacity: 0.88; }
	.primary:active { transform: translateY(1px); }
	.primary:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.hint {
		margin: 0;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}
	@keyframes rise {
		from { opacity: 0; transform: translateY(6px); }
		to { opacity: 1; transform: none; }
	}
	@media (max-width: 639px) {
		.primary { min-height: 44px; }
	}
	@media (prefers-reduced-motion: reduce) {
		.unavailable { animation: none; }
	}
</style>

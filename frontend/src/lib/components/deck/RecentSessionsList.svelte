<script lang="ts">
	/**
	 * RecentSessionsList - Compact list view for recent sessions
	 *
	 * Displays sessions in a scannable vertical list with:
	 * - Message count badge (left)
	 * - Session title (center, full width)
	 * - Relative timestamp (right)
	 * - Streaming status indicator
	 */

	import { MessageSquare, Zap } from 'lucide-svelte';
	import type { Session } from '$lib/stores/tabs';

	interface Props {
		sessions: Session[];
		onOpenThread: (sessionId: string) => void;
		isSessionStreaming: (sessionId: string) => boolean;
		formatRelativeTime: (dateStr: string) => string;
	}

	let {
		sessions,
		onOpenThread,
		isSessionStreaming,
		formatRelativeTime
	}: Props = $props();

	function handleKeyDown(e: KeyboardEvent, sessionId: string) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			onOpenThread(sessionId);
		}
	}
</script>

<div class="recent-list">
	{#each sessions as session, index (session.id)}
		<button
			class="list-item"
			class:is-streaming={isSessionStreaming(session.id)}
			onclick={() => onOpenThread(session.id)}
			onkeydown={(e) => handleKeyDown(e, session.id)}
			style="animation-delay: {index * 25}ms"
		>
			<!-- Left accent bar -->
			<div class="accent-bar"></div>

			<!-- Message count (left) -->
			<div class="msg-count" class:streaming={isSessionStreaming(session.id)}>
				{#if isSessionStreaming(session.id)}
					<Zap size={12} />
				{:else}
					<MessageSquare size={12} />
				{/if}
				<span>{session.turn_count || session.message_count || 0}</span>
			</div>

			<!-- Title (center) -->
			<span class="item-title">
				{session.title || 'Untitled conversation'}
			</span>

			<!-- Streaming indicator -->
			{#if isSessionStreaming(session.id)}
				<span class="streaming-badge">
					<span class="streaming-dot"></span>
				</span>
			{/if}

			<!-- Timestamp (right) -->
			<span class="item-time">
				{formatRelativeTime(session.updated_at)}
			</span>
		</button>
	{/each}

	{#if sessions.length === 0}
		<div class="empty-state">
			<p>No recent conversations</p>
		</div>
	{/if}
</div>

<style>
	.recent-list {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 16px 32px;
		width: 100%;
		max-width: 900px;
		margin: 0 auto;
		overflow-y: auto;
		max-height: 100%;
	}

	.list-item {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 10px 14px;
		background: var(--bg-card, oklch(0.15 0.01 260));
		border: 1px solid var(--border-default, rgba(255, 255, 255, 0.08));
		border-radius: 8px;
		cursor: pointer;
		transition: all 0.15s ease;
		position: relative;
		overflow: hidden;
		text-align: left;
		width: 100%;
		color: inherit;
		font-family: inherit;

		/* Entry animation */
		opacity: 0;
		transform: translateX(16px);
		animation: listItemEnter 0.25s ease forwards;
	}

	@keyframes listItemEnter {
		to {
			opacity: 1;
			transform: translateX(0);
		}
	}

	.list-item:hover {
		background: var(--bg-card-hover, oklch(0.17 0.01 260));
		border-color: var(--border-strong, rgba(255, 255, 255, 0.12));
		transform: translateX(3px);
	}

	.list-item:focus-visible {
		outline: 2px solid var(--ai-primary, #22d3ee);
		outline-offset: 2px;
	}

	/* Left accent bar - subtle monochrome */
	.accent-bar {
		position: absolute;
		left: 0;
		top: 0;
		bottom: 0;
		width: 3px;
		background: var(--text-dim, #52525b);
		opacity: 0.3;
		transition: all 0.15s ease;
	}

	.list-item:hover .accent-bar {
		opacity: 0.7;
		background: var(--ai-primary, #22d3ee);
	}

	.list-item.is-streaming .accent-bar {
		background: var(--ai-primary, #22d3ee);
		opacity: 1;
		animation: pulse 2s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.4; }
	}

	/* Message count badge (left side) */
	.msg-count {
		display: flex;
		align-items: center;
		gap: 4px;
		min-width: 44px;
		padding: 4px 8px;
		background: rgba(255, 255, 255, 0.04);
		border: 1px solid rgba(255, 255, 255, 0.06);
		border-radius: 6px;
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--text-muted, #71717a);
		flex-shrink: 0;
		transition: all 0.15s ease;
	}

	.list-item:hover .msg-count {
		background: rgba(34, 211, 238, 0.08);
		border-color: rgba(34, 211, 238, 0.15);
		color: var(--text-secondary, #a1a1aa);
	}

	.msg-count.streaming {
		background: rgba(34, 211, 238, 0.12);
		border-color: rgba(34, 211, 238, 0.25);
		color: var(--ai-primary, #22d3ee);
	}

	/* Title */
	.item-title {
		flex: 1;
		min-width: 0;
		font-size: 0.875rem;
		font-weight: 450;
		color: var(--text-primary, #f4f4f5);
		line-height: 1.35;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* Streaming indicator dot */
	.streaming-badge {
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
	}

	.streaming-dot {
		width: 6px;
		height: 6px;
		background: var(--ai-primary, #22d3ee);
		border-radius: 50%;
		animation: blink 1.5s ease-in-out infinite;
		box-shadow: 0 0 6px var(--ai-primary, #22d3ee);
	}

	@keyframes blink {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.3; }
	}

	/* Timestamp */
	.item-time {
		font-size: 0.6875rem;
		color: var(--text-dim, #52525b);
		flex-shrink: 0;
		white-space: nowrap;
		min-width: 55px;
		text-align: right;
	}

	/* Empty state */
	.empty-state {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 60px 20px;
		color: var(--text-muted, #71717a);
		font-size: 0.9375rem;
	}

	/* Mobile responsive */
	@media (max-width: 640px) {
		.recent-list {
			padding: 12px 16px;
			gap: 3px;
		}

		.list-item {
			padding: 8px 10px;
			gap: 8px;
		}

		.msg-count {
			min-width: 38px;
			padding: 3px 6px;
			font-size: 0.6875rem;
		}

		.item-title {
			font-size: 0.8125rem;
		}

		.item-time {
			font-size: 0.625rem;
			min-width: 48px;
		}
	}
</style>

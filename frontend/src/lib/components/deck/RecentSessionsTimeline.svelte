<script lang="ts">
	/**
	 * RecentSessionsTimeline - Chronological timeline view for recent sessions
	 *
	 * Displays sessions grouped by time period with:
	 * - Visual timeline connector
	 * - Date group headers (Today, Yesterday, This Week, etc.)
	 * - Session cards with contextual info
	 * - Streaming status indicators
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

	// Group sessions by time period
	interface TimeGroup {
		label: string;
		sessions: Session[];
	}

	function groupSessionsByTime(sessions: Session[]): TimeGroup[] {
		const now = new Date();
		const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
		const yesterday = new Date(today.getTime() - 86400000);
		const weekAgo = new Date(today.getTime() - 7 * 86400000);

		const groups: { [key: string]: Session[] } = {
			'Today': [],
			'Yesterday': [],
			'This Week': [],
			'Earlier': []
		};

		for (const session of sessions) {
			const sessionDate = new Date(session.updated_at);
			const sessionDay = new Date(sessionDate.getFullYear(), sessionDate.getMonth(), sessionDate.getDate());

			if (sessionDay.getTime() >= today.getTime()) {
				groups['Today'].push(session);
			} else if (sessionDay.getTime() >= yesterday.getTime()) {
				groups['Yesterday'].push(session);
			} else if (sessionDay.getTime() >= weekAgo.getTime()) {
				groups['This Week'].push(session);
			} else {
				groups['Earlier'].push(session);
			}
		}

		// Return only non-empty groups
		return Object.entries(groups)
			.filter(([_, sessions]) => sessions.length > 0)
			.map(([label, sessions]) => ({ label, sessions }));
	}

	const groupedSessions = $derived(groupSessionsByTime(sessions));

	function handleKeyDown(e: KeyboardEvent, sessionId: string) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			onOpenThread(sessionId);
		}
	}

	// Format time of day
	function formatTimeOfDay(dateStr: string): string {
		const date = new Date(dateStr);
		return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
	}
</script>

<div class="timeline-container">
	<div class="timeline">
		{#each groupedSessions as group, groupIndex}
			<div class="timeline-group" style="--group-index: {groupIndex}">
				<div class="timeline-date">{group.label}</div>

				{#each group.sessions as session, index (session.id)}
					<div
						class="timeline-item"
						class:is-streaming={isSessionStreaming(session.id)}
						style="animation-delay: {(groupIndex * 100) + (index * 40)}ms"
					>
						<!-- Timeline node -->
						<div class="timeline-node">
							<div class="node-dot"></div>
						</div>

						<!-- Card -->
						<button
							class="timeline-card"
							onclick={() => onOpenThread(session.id)}
							onkeydown={(e) => handleKeyDown(e, session.id)}
						>
							<div class="card-header">
								<span class="card-title">
									{session.title || 'Untitled conversation'}
								</span>
								{#if isSessionStreaming(session.id)}
									<span class="streaming-indicator">
										<Zap size={12} />
										Live
									</span>
								{/if}
							</div>

							<div class="card-preview">
								Session with {session.turn_count || session.message_count || 0} {(session.turn_count || session.message_count || 0) === 1 ? 'turn' : 'turns'}
							</div>

							<div class="card-footer">
								<span class="card-time">{formatTimeOfDay(session.updated_at)}</span>
								<span class="card-meta">
									<MessageSquare size={12} />
									{session.turn_count || session.message_count || 0}
								</span>
							</div>
						</button>
					</div>
				{/each}
			</div>
		{/each}

		{#if sessions.length === 0}
			<div class="empty-state">
				<p>No recent conversations</p>
			</div>
		{/if}
	</div>
</div>

<style>
	.timeline-container {
		width: 100%;
		max-width: 700px;
		margin: 0 auto;
		padding: 20px 40px;
		overflow-y: auto;
		max-height: 100%;
	}

	.timeline {
		position: relative;
		padding-left: 28px;
	}

	/* Vertical timeline line */
	.timeline::before {
		content: '';
		position: absolute;
		left: 8px;
		top: 0;
		bottom: 0;
		width: 2px;
		background: linear-gradient(
			to bottom,
			var(--ai-primary, #22d3ee) 0%,
			var(--text-dim, #52525b) 30%,
			var(--border-subtle, rgba(255, 255, 255, 0.06)) 100%
		);
		opacity: 0.5;
	}

	.timeline-group {
		margin-bottom: 28px;
	}

	.timeline-group:last-child {
		margin-bottom: 0;
	}

	/* Date header */
	.timeline-date {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-dim, #52525b);
		margin-bottom: 12px;
		margin-left: 4px;
	}

	.timeline-item {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		position: relative;
		margin-bottom: 10px;

		/* Entry animation */
		opacity: 0;
		transform: translateY(10px);
		animation: timelineItemEnter 0.35s ease forwards;
	}

	@keyframes timelineItemEnter {
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.timeline-item:last-child {
		margin-bottom: 0;
	}

	/* Timeline node (dot) */
	.timeline-node {
		position: absolute;
		left: -24px;
		top: 14px;
		width: 18px;
		height: 18px;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.node-dot {
		width: 10px;
		height: 10px;
		border-radius: 50%;
		background: var(--text-dim, #52525b);
		border: 2px solid var(--bg-base, oklch(0.10 0.01 260));
		transition: all 0.2s ease;
	}

	.timeline-item:hover .node-dot {
		background: var(--ai-primary, #22d3ee);
		box-shadow: 0 0 8px rgba(34, 211, 238, 0.4);
	}

	.timeline-item.is-streaming .node-dot {
		background: var(--ai-primary, #22d3ee);
		animation: nodePulse 2s ease-in-out infinite;
	}

	@keyframes nodePulse {
		0%, 100% {
			box-shadow: 0 0 0 0 rgba(34, 211, 238, 0.4);
		}
		50% {
			box-shadow: 0 0 0 6px rgba(34, 211, 238, 0);
		}
	}

	/* Timeline card */
	.timeline-card {
		flex: 1;
		background: var(--bg-card, oklch(0.15 0.01 260));
		border: 1px solid var(--border-default, rgba(255, 255, 255, 0.1));
		border-radius: 10px;
		padding: 14px 16px;
		cursor: pointer;
		transition: all 0.2s ease;
		text-align: left;
		width: 100%;
		color: inherit;
		font-family: inherit;
	}

	.timeline-card:hover {
		background: var(--bg-card-hover, oklch(0.17 0.01 260));
		border-color: var(--ai-primary, #22d3ee);
		transform: translateX(4px);
	}

	.timeline-card:focus-visible {
		outline: 2px solid var(--ai-primary, #22d3ee);
		outline-offset: 2px;
	}

	.timeline-item.is-streaming .timeline-card {
		border-color: rgba(34, 211, 238, 0.3);
	}

	.card-header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: 6px;
	}

	.card-title {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--text-primary, #f4f4f5);
		line-height: 1.4;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.streaming-indicator {
		display: flex;
		align-items: center;
		gap: 4px;
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--ai-primary, #22d3ee);
		background: rgba(34, 211, 238, 0.1);
		border: 1px solid rgba(34, 211, 238, 0.2);
		padding: 2px 8px;
		border-radius: 4px;
		flex-shrink: 0;
	}

	.card-preview {
		font-size: 0.8125rem;
		color: var(--text-muted, #71717a);
		line-height: 1.4;
		margin-bottom: 10px;
	}

	.card-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.card-time {
		font-size: 0.6875rem;
		color: var(--text-dim, #52525b);
	}

	.card-meta {
		display: flex;
		align-items: center;
		gap: 4px;
		font-size: 0.75rem;
		color: var(--text-dim, #52525b);
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
		.timeline-container {
			padding: 16px 20px;
		}

		.timeline {
			padding-left: 24px;
		}

		.timeline::before {
			left: 6px;
		}

		.timeline-node {
			left: -20px;
		}

		.node-dot {
			width: 8px;
			height: 8px;
		}

		.timeline-card {
			padding: 12px 14px;
		}

		.card-title {
			font-size: 0.8125rem;
		}

		.card-preview {
			font-size: 0.75rem;
		}
	}
</style>

<script lang="ts">
	/**
	 * ThreadsTabContent - Recent sessions and history
	 */

	import { History } from 'lucide-svelte';
	import type { HistorySession } from '../ActivityPanel.svelte';

	interface Props {
		recentSessions?: HistorySession[];
		onHistorySessionClick?: (session: HistorySession) => void;
	}

	let {
		recentSessions = [],
		onHistorySessionClick
	}: Props = $props();

	function formatTime(date: Date): string {
		const now = new Date();
		const diff = now.getTime() - date.getTime();
		const minutes = Math.floor(diff / 60000);
		const hours = Math.floor(diff / 3600000);
		const days = Math.floor(diff / 86400000);

		if (minutes < 1) return 'just now';
		if (minutes < 60) return `${minutes}m ago`;
		if (hours < 24) return `${hours}h ago`;
		return `${days}d ago`;
	}
</script>

<div class="threads-content">
	<div class="section">
		<div class="section-header">
			<History size={14} strokeWidth={1.5} />
			<span>Recent Sessions</span>
			{#if recentSessions.length > 0}
				<span class="section-count">{recentSessions.length}</span>
			{/if}
		</div>

		{#if recentSessions.length === 0}
			<div class="empty-state">No session history</div>
		{:else}
			<div class="sessions-list">
				{#each recentSessions as session (session.id)}
					<button
						class="session-item"
						class:open={session.isOpen}
						onclick={() => onHistorySessionClick?.(session)}
					>
						<div class="item-content">
							<div class="item-title">{session.title}</div>
						</div>
						<div class="item-meta">
							{#if session.isOpen}
								<span class="open-badge">open</span>
							{:else}
								{formatTime(session.timestamp)}
							{/if}
						</div>
					</button>
				{/each}
			</div>
		{/if}
	</div>
</div>

<style>
	.threads-content {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.section {
		background: var(--secondary);
		border: 1px solid var(--border);
		border-radius: 10px;
		overflow: hidden;
	}

	.section-header {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 12px;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--foreground);
		border-bottom: 1px solid var(--border);
	}

	.section-count {
		padding: 2px 6px;
		background: var(--muted);
		border-radius: 10px;
		font-size: 0.625rem;
		font-weight: 700;
	}

	.empty-state {
		padding: 24px 16px;
		text-align: center;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.sessions-list {
		padding: 8px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.session-item {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 8px 10px;
		background: transparent;
		border: 1px solid transparent;
		border-radius: 8px;
		cursor: pointer;
		transition: all 0.15s ease;
		text-align: left;
		width: 100%;
	}

	.session-item:hover {
		background: var(--hover-overlay);
		border-color: var(--border);
	}

	.session-item.open {
		opacity: 0.6;
	}

	.item-content {
		flex: 1;
		min-width: 0;
	}

	.item-title {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.item-meta {
		font-size: 0.625rem;
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.open-badge {
		font-size: 0.5625rem;
		font-weight: 600;
		text-transform: uppercase;
		color: var(--success);
		background: color-mix(in oklch, var(--success) 15%, transparent);
		padding: 2px 6px;
		border-radius: 4px;
	}
</style>

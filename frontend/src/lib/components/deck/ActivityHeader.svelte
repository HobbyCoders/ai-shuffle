<script lang="ts">
	/**
	 * ActivityHeader - Fixed header showing active chats + running background agents
	 *
	 * Always visible at the top of the Activity Panel
	 */

	import { MessageSquare, Bot, Loader2, AlertCircle } from 'lucide-svelte';
	import type { ActiveSession } from './ActivityPanel.svelte';

	interface Props {
		sessions?: ActiveSession[];
		onSessionClick?: (session: ActiveSession) => void;
	}

	let {
		sessions = [],
		onSessionClick
	}: Props = $props();

	// Separate chats and agents for display
	const chats = $derived(sessions.filter(s => s.type === 'chat'));
	const agents = $derived(sessions.filter(s => s.type === 'agent'));

	function getStatusColor(status: ActiveSession['status']): string {
		switch (status) {
			case 'streaming':
			case 'running':
				return 'var(--success)';
			case 'active':
				return 'var(--primary)';
			case 'error':
				return 'var(--destructive)';
			case 'idle':
			default:
				return 'var(--muted-foreground)';
		}
	}

	function getStatusIcon(session: ActiveSession) {
		if (session.status === 'error') return AlertCircle;
		if (session.status === 'streaming' || session.status === 'running') return Loader2;
		return session.type === 'agent' ? Bot : MessageSquare;
	}

	function isAnimating(status: ActiveSession['status']): boolean {
		return status === 'streaming' || status === 'running';
	}
</script>

<div class="activity-header">
	<div class="header-title">Active Sessions</div>

	{#if sessions.length === 0}
		<div class="empty-state">No active sessions</div>
	{:else}
		<div class="sessions-list">
			{#each sessions as session (session.id)}
				{@const Icon = getStatusIcon(session)}
				<button
					class="session-item"
					class:selected={session.isSelected}
					class:unread={session.unread}
					onclick={() => onSessionClick?.(session)}
				>
					<div class="session-icon" class:animating={isAnimating(session.status)}>
						<Icon
							size={14}
							strokeWidth={1.5}
							style="color: {getStatusColor(session.status)}"
						/>
					</div>
					<div class="session-info">
						<span class="session-title">{session.title}</span>
						{#if session.type === 'agent' && session.progress !== undefined}
							<span class="session-progress">{session.progress}%</span>
						{/if}
					</div>
					{#if session.unread}
						<div class="unread-dot"></div>
					{/if}
				</button>
			{/each}
		</div>
	{/if}
</div>

<style>
	.activity-header {
		padding: 12px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.header-title {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
		margin-bottom: 8px;
	}

	.empty-state {
		padding: 12px 0;
		text-align: center;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.sessions-list {
		display: flex;
		flex-direction: column;
		gap: 4px;
		max-height: 200px;
		overflow-y: auto;
	}

	.session-item {
		display: flex;
		align-items: center;
		gap: 8px;
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

	.session-item.selected {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		border-color: color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.session-icon {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.session-icon.animating {
		animation: pulse 1.5s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.5; }
	}

	.session-info {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.session-title {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.session-progress {
		font-size: 0.625rem;
		font-weight: 600;
		color: var(--success);
		flex-shrink: 0;
	}

	.unread-dot {
		width: 6px;
		height: 6px;
		background: var(--primary);
		border-radius: 50%;
		flex-shrink: 0;
	}
</style>

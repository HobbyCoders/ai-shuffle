<script lang="ts">
	/**
	 * AgentsTabContent - Background agents (running + completed)
	 */

	import { Bot, CheckCircle2, XCircle, Pause, Loader2 } from 'lucide-svelte';
	import type { DeckAgent } from '../types';

	interface Props {
		running?: DeckAgent[];
		completed?: DeckAgent[];
		onAgentClick?: (agent: DeckAgent) => void;
	}

	let {
		running = [],
		completed = [],
		onAgentClick
	}: Props = $props();

	function getAgentStatusColor(status: DeckAgent['status']): string {
		switch (status) {
			case 'running':
				return 'var(--success)';
			case 'idle':
				return 'var(--muted-foreground)';
			case 'error':
				return 'var(--destructive)';
			case 'paused':
				return 'var(--warning)';
			default:
				return 'var(--muted-foreground)';
		}
	}

	function getStatusIcon(status: DeckAgent['status']) {
		switch (status) {
			case 'running':
				return Loader2;
			case 'error':
				return XCircle;
			case 'paused':
				return Pause;
			default:
				return CheckCircle2;
		}
	}
</script>

<div class="agents-content">
	<!-- Running Agents -->
	<div class="section">
		<div class="section-header">
			<Bot size={14} strokeWidth={1.5} />
			<span>Running</span>
			{#if running.length > 0}
				<span class="section-count active">{running.length}</span>
			{/if}
		</div>

		{#if running.length === 0}
			<div class="empty-state">No agents running</div>
		{:else}
			<div class="agents-list">
				{#each running as agent (agent.id)}
					<button class="agent-item" onclick={() => onAgentClick?.(agent)}>
						<div class="status-indicator running">
							<Loader2 size={12} strokeWidth={2} />
						</div>
						<div class="agent-info">
							<div class="agent-name">{agent.name}</div>
							{#if agent.task}
								<div class="agent-task">{agent.task}</div>
							{/if}
						</div>
						{#if agent.progress !== undefined}
							<div class="progress-badge">{agent.progress}%</div>
						{/if}
					</button>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Completed Agents -->
	{#if completed.length > 0}
		<div class="section">
			<div class="section-header">
				<CheckCircle2 size={14} strokeWidth={1.5} />
				<span>Completed</span>
				<span class="section-count">{completed.length}</span>
			</div>

			<div class="agents-list">
				{#each completed as agent (agent.id)}
					<button class="agent-item completed" onclick={() => onAgentClick?.(agent)}>
						<div class="status-indicator" style:color={getAgentStatusColor(agent.status)}>
							<svelte:component this={getStatusIcon(agent.status)} size={12} strokeWidth={2} />
						</div>
						<div class="agent-info">
							<div class="agent-name">{agent.name}</div>
							{#if agent.task}
								<div class="agent-task">{agent.task}</div>
							{/if}
						</div>
					</button>
				{/each}
			</div>
		</div>
	{/if}
</div>

<style>
	.agents-content {
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

	.section-count.active {
		background: color-mix(in oklch, var(--success) 20%, transparent);
		color: var(--success);
	}

	.empty-state {
		padding: 24px 16px;
		text-align: center;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.agents-list {
		padding: 8px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.agent-item {
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

	.agent-item:hover {
		background: var(--hover-overlay);
		border-color: var(--border);
	}

	.agent-item.completed {
		opacity: 0.7;
	}

	.status-indicator {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.status-indicator.running {
		color: var(--success);
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	.agent-info {
		flex: 1;
		min-width: 0;
	}

	.agent-name {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.agent-task {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		margin-top: 2px;
	}

	.progress-badge {
		font-size: 0.625rem;
		font-weight: 600;
		color: var(--success);
		background: color-mix(in oklch, var(--success) 15%, transparent);
		padding: 2px 6px;
		border-radius: 4px;
		flex-shrink: 0;
	}
</style>

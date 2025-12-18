<script lang="ts">
	/**
	 * Dock - Bottom bar for minimized cards and running processes
	 *
	 * Shows minimized cards on the left and running processes (generations, agents)
	 * in the center. Desktop only - hidden on mobile.
	 */

	import {
		MessageSquare,
		Bot,
		Image,
		Video,
		AudioLines,
		File,
		Settings,
		Loader2
	} from 'lucide-svelte';
	import type { MinimizedCard, RunningProcess } from './types';

	interface Props {
		minimizedCards?: MinimizedCard[];
		runningProcesses?: RunningProcess[];
		onCardClick?: (card: MinimizedCard) => void;
		onProcessClick?: (process: RunningProcess) => void;
	}

	let {
		minimizedCards = [],
		runningProcesses = [],
		onCardClick,
		onProcessClick
	}: Props = $props();

	function getCardIconComponent(type: MinimizedCard['type']) {
		switch (type) {
			case 'chat':
				return MessageSquare;
			case 'agent':
				return Bot;
			case 'generation':
				return Image;
			case 'file':
				return File;
			case 'settings':
				return Settings;
			default:
				return File;
		}
	}

	function getProcessStatusColor(status: RunningProcess['status']): string {
		switch (status) {
			case 'running':
			case 'generating':
				return '#a855f7';
			case 'idle':
			case 'pending':
				return '#6b7280';
			case 'complete':
				return '#10b981';
			case 'error':
				return '#ef4444';
			case 'paused':
				return '#f59e0b';
			default:
				return '#6b7280';
		}
	}

	function isProcessActive(status: RunningProcess['status']): boolean {
		return status === 'running' || status === 'generating';
	}
</script>

<div class="dock">
	<!-- Left: Minimized Cards -->
	<div class="dock-section minimized-cards">
		{#each minimizedCards as card}
			{@const IconComponent = getCardIconComponent(card.type)}
			<button
				class="dock-item card-item"
				onclick={() => onCardClick?.(card)}
				title={card.title}
			>
				<div class="item-icon">
					<IconComponent size={16} strokeWidth={1.5} />
				</div>
				<span class="item-label">{card.title}</span>
				<span class="tooltip">{card.title}</span>
			</button>
		{/each}
	</div>

	<!-- Center: Running Processes -->
	<div class="dock-section running-processes">
		{#each runningProcesses as process}
			{@const isActive = isProcessActive(process.status)}
			{@const statusColor = getProcessStatusColor(process.status)}
			<button
				class="dock-item process-item"
				class:active={isActive}
				onclick={() => onProcessClick?.(process)}
				title={`${process.title} - ${process.status}${process.progress !== undefined ? ` (${process.progress}%)` : ''}`}
			>
				<div class="process-icon" style:--status-color={statusColor}>
					{#if process.type === 'generation'}
						<Image size={14} strokeWidth={1.5} />
					{:else}
						<Bot size={14} strokeWidth={1.5} />
					{/if}
					{#if isActive}
						<div class="spinner">
							<Loader2 size={18} strokeWidth={2} class="spin" />
						</div>
					{/if}
				</div>
				<div class="process-content">
					<span class="process-title">{process.title}</span>
					{#if process.progress !== undefined && isActive}
						<div class="progress-bar-container">
							<div class="progress-bar" style:width="{process.progress}%"></div>
						</div>
					{/if}
				</div>
				<span class="tooltip">{process.title} - {process.status}</span>
			</button>
		{/each}
	</div>

	<!-- Right: Spacer for balance -->
	<div class="dock-section dock-spacer"></div>
</div>

<style>
	.dock {
		display: flex;
		align-items: center;
		justify-content: space-between;
		height: 56px;
		background: rgba(17, 17, 17, 0.95);
		border-top: 1px solid rgba(255, 255, 255, 0.08);
		padding: 0 16px;
		backdrop-filter: blur(12px);
	}

	.dock-section {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.minimized-cards {
		flex: 1;
		justify-content: flex-start;
	}

	.running-processes {
		flex: 0 0 auto;
		justify-content: center;
	}

	.dock-spacer {
		flex: 1;
	}

	.dock-item {
		position: relative;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 12px;
		background: rgba(255, 255, 255, 0.05);
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-radius: 10px;
		color: rgba(255, 255, 255, 0.8);
		cursor: pointer;
		transition: all 0.15s ease;
		max-width: 160px;
	}

	.dock-item:hover {
		background: rgba(255, 255, 255, 0.1);
		border-color: rgba(255, 255, 255, 0.15);
		color: white;
	}

	.card-item .item-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		color: rgba(255, 255, 255, 0.6);
	}

	.card-item:hover .item-icon {
		color: rgba(255, 255, 255, 0.9);
	}

	.item-label {
		font-size: 0.75rem;
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.process-item {
		padding: 6px 12px;
	}

	.process-item.active {
		border-color: rgba(168, 85, 247, 0.3);
		background: rgba(168, 85, 247, 0.1);
	}

	.process-icon {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 24px;
		height: 24px;
		color: var(--status-color);
	}

	.spinner {
		position: absolute;
		inset: -2px;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.spinner :global(.spin) {
		animation: spin 1s linear infinite;
		opacity: 0.6;
	}

	@keyframes spin {
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}

	.process-content {
		display: flex;
		flex-direction: column;
		gap: 4px;
		min-width: 0;
	}

	.process-title {
		font-size: 0.6875rem;
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 100px;
	}

	.progress-bar-container {
		width: 80px;
		height: 3px;
		background: rgba(255, 255, 255, 0.1);
		border-radius: 2px;
		overflow: hidden;
	}

	.progress-bar {
		height: 100%;
		background: linear-gradient(90deg, #a855f7, #ec4899);
		border-radius: 2px;
		transition: width 0.3s ease;
	}

	.tooltip {
		position: absolute;
		bottom: calc(100% + 8px);
		left: 50%;
		transform: translateX(-50%);
		padding: 6px 10px;
		background: rgba(30, 30, 30, 0.95);
		border: 1px solid rgba(255, 255, 255, 0.1);
		border-radius: 6px;
		font-size: 0.6875rem;
		color: white;
		white-space: nowrap;
		opacity: 0;
		visibility: hidden;
		transition: all 0.15s ease;
		pointer-events: none;
		z-index: 100;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
	}

	.dock-item:hover .tooltip {
		opacity: 1;
		visibility: visible;
	}
</style>

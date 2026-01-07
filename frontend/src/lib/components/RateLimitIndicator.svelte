<script lang="ts">
	/**
	 * RateLimitIndicator - Shows current rate limit status and queue position
	 *
	 * Displays:
	 * - Current usage vs limits
	 * - Warning when approaching limits
	 * - Queue position when request is queued
	 */
	import { onMount, onDestroy } from 'svelte';
	import { api } from '$lib/api/client';

	interface RateLimitStatus {
		minute_count: number;
		minute_limit: number;
		minute_remaining: number;
		minute_reset: string;
		hour_count: number;
		hour_limit: number;
		hour_remaining: number;
		hour_reset: string;
		day_count: number;
		day_limit: number;
		day_remaining: number;
		day_reset: string;
		concurrent_count: number;
		concurrent_limit: number;
		concurrent_remaining: number;
		is_limited: boolean;
		is_unlimited: boolean;
		priority: number;
	}

	interface QueueStatus {
		position: number;
		estimated_wait_seconds: number;
		total_queued: number;
		is_queued: boolean;
	}

	interface Props {
		showDetailed?: boolean;
		compact?: boolean;
	}

	let { showDetailed = false, compact = false }: Props = $props();

	let status: RateLimitStatus | null = $state(null);
	let queueStatus: QueueStatus | null = $state(null);
	let loading = $state(true);
	let error = $state('');
	let refreshInterval: ReturnType<typeof setInterval> | null = null;

	// Calculate warning thresholds
	const getWarningLevel = (remaining: number, limit: number): 'ok' | 'warning' | 'critical' => {
		if (limit === 0) return 'ok';
		const percentage = (remaining / limit) * 100;
		if (percentage <= 10) return 'critical';
		if (percentage <= 25) return 'warning';
		return 'ok';
	};

	// Get the most critical warning level across all limits
	const overallWarningLevel = $derived(() => {
		if (!status) return 'ok';
		if (status.is_unlimited) return 'ok';

		const levels = [
			getWarningLevel(status.minute_remaining, status.minute_limit),
			getWarningLevel(status.hour_remaining, status.hour_limit),
			getWarningLevel(status.day_remaining, status.day_limit)
		];

		if (levels.includes('critical')) return 'critical';
		if (levels.includes('warning')) return 'warning';
		return 'ok';
	});

	// Format time until reset
	const formatTimeUntil = (resetTime: string): string => {
		const reset = new Date(resetTime);
		const now = new Date();
		const diffMs = reset.getTime() - now.getTime();

		if (diffMs <= 0) return 'now';

		const seconds = Math.floor(diffMs / 1000);
		const minutes = Math.floor(seconds / 60);
		const hours = Math.floor(minutes / 60);

		if (hours > 0) return `${hours}h ${minutes % 60}m`;
		if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
		return `${seconds}s`;
	};

	async function loadStatus() {
		try {
			const [rateLimitRes, queueRes] = await Promise.all([
				api.get<RateLimitStatus>('/rate-limits'),
				api.get<QueueStatus>('/rate-limits/queue')
			]);
			status = rateLimitRes;
			queueStatus = queueRes;
			error = '';
		} catch (e: any) {
			// Rate limits endpoint may not be available if not authenticated
			if (e.status !== 401) {
				error = e.detail || 'Failed to load rate limits';
			}
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		loadStatus();
		// Refresh every 30 seconds
		refreshInterval = setInterval(loadStatus, 30000);
	});

	onDestroy(() => {
		if (refreshInterval) {
			clearInterval(refreshInterval);
		}
	});
</script>

{#if !loading && status && !status.is_unlimited}
	<div
		class="rate-limit-indicator"
		class:compact
		class:warning={overallWarningLevel() === 'warning'}
		class:critical={overallWarningLevel() === 'critical'}
	>
		{#if queueStatus?.is_queued}
			<!-- Queue position indicator -->
			<div class="queue-indicator">
				<svg class="queue-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<circle cx="12" cy="12" r="10"/>
					<polyline points="12,6 12,12 16,14"/>
				</svg>
				<span class="queue-text">
					Queue position: {queueStatus.position}
					{#if queueStatus.estimated_wait_seconds > 0}
						<span class="queue-wait">(~{Math.ceil(queueStatus.estimated_wait_seconds / 60)} min wait)</span>
					{/if}
				</span>
			</div>
		{:else if compact}
			<!-- Compact view: just show most constrained limit -->
			<div class="compact-view">
				{#if status.minute_remaining < status.hour_remaining && status.minute_remaining < status.day_remaining}
					<span class="limit-badge" class:warning={getWarningLevel(status.minute_remaining, status.minute_limit) === 'warning'} class:critical={getWarningLevel(status.minute_remaining, status.minute_limit) === 'critical'}>
						{status.minute_remaining}/{status.minute_limit} /min
					</span>
				{:else if status.hour_remaining < status.day_remaining}
					<span class="limit-badge" class:warning={getWarningLevel(status.hour_remaining, status.hour_limit) === 'warning'} class:critical={getWarningLevel(status.hour_remaining, status.hour_limit) === 'critical'}>
						{status.hour_remaining}/{status.hour_limit} /hr
					</span>
				{:else}
					<span class="limit-badge" class:warning={getWarningLevel(status.day_remaining, status.day_limit) === 'warning'} class:critical={getWarningLevel(status.day_remaining, status.day_limit) === 'critical'}>
						{status.day_remaining}/{status.day_limit} /day
					</span>
				{/if}
			</div>
		{:else}
			<!-- Standard view -->
			<div class="status-row">
				<div class="limit-item" class:warning={getWarningLevel(status.minute_remaining, status.minute_limit) === 'warning'} class:critical={getWarningLevel(status.minute_remaining, status.minute_limit) === 'critical'}>
					<span class="limit-label">Per minute</span>
					<span class="limit-value">{status.minute_remaining}/{status.minute_limit}</span>
					{#if status.minute_remaining < status.minute_limit}
						<span class="reset-time">resets in {formatTimeUntil(status.minute_reset)}</span>
					{/if}
				</div>
				<div class="limit-item" class:warning={getWarningLevel(status.hour_remaining, status.hour_limit) === 'warning'} class:critical={getWarningLevel(status.hour_remaining, status.hour_limit) === 'critical'}>
					<span class="limit-label">Per hour</span>
					<span class="limit-value">{status.hour_remaining}/{status.hour_limit}</span>
				</div>
				<div class="limit-item" class:warning={getWarningLevel(status.day_remaining, status.day_limit) === 'warning'} class:critical={getWarningLevel(status.day_remaining, status.day_limit) === 'critical'}>
					<span class="limit-label">Per day</span>
					<span class="limit-value">{status.day_remaining}/{status.day_limit}</span>
				</div>
			</div>

			{#if showDetailed}
				<div class="detailed-info">
					<div class="info-row">
						<span>Concurrent requests:</span>
						<span>{status.concurrent_remaining}/{status.concurrent_limit}</span>
					</div>
					<div class="info-row">
						<span>Priority level:</span>
						<span>{status.priority}</span>
					</div>
				</div>
			{/if}
		{/if}

		{#if overallWarningLevel() === 'critical'}
			<div class="warning-message">
				<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
					<line x1="12" y1="9" x2="12" y2="13"/>
					<line x1="12" y1="17" x2="12.01" y2="17"/>
				</svg>
				<span>Rate limit nearly exhausted</span>
			</div>
		{/if}
	</div>
{/if}

<style>
	.rate-limit-indicator {
		padding: 0.5rem 0.75rem;
		border-radius: 0.375rem;
		background: var(--bg-secondary, #f3f4f6);
		font-size: 0.75rem;
		color: var(--text-secondary, #6b7280);
	}

	:global(.dark) .rate-limit-indicator {
		background: var(--bg-secondary, #374151);
		color: var(--text-secondary, #9ca3af);
	}

	.rate-limit-indicator.warning {
		background: #fef3c7;
		border: 1px solid #f59e0b;
	}

	:global(.dark) .rate-limit-indicator.warning {
		background: rgba(245, 158, 11, 0.1);
		border: 1px solid #f59e0b;
	}

	.rate-limit-indicator.critical {
		background: #fee2e2;
		border: 1px solid #ef4444;
	}

	:global(.dark) .rate-limit-indicator.critical {
		background: rgba(239, 68, 68, 0.1);
		border: 1px solid #ef4444;
	}

	.rate-limit-indicator.compact {
		padding: 0.25rem 0.5rem;
		display: inline-flex;
		align-items: center;
	}

	.queue-indicator {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		color: #f59e0b;
	}

	.queue-icon {
		width: 1rem;
		height: 1rem;
	}

	.queue-text {
		font-weight: 500;
	}

	.queue-wait {
		font-weight: 400;
		opacity: 0.8;
	}

	.status-row {
		display: flex;
		gap: 1rem;
		flex-wrap: wrap;
	}

	.limit-item {
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
	}

	.limit-item.warning .limit-value {
		color: #f59e0b;
		font-weight: 600;
	}

	.limit-item.critical .limit-value {
		color: #ef4444;
		font-weight: 600;
	}

	.limit-label {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		opacity: 0.7;
	}

	.limit-value {
		font-weight: 500;
		color: var(--text-primary, #111827);
	}

	:global(.dark) .limit-value {
		color: var(--text-primary, #f9fafb);
	}

	.reset-time {
		font-size: 0.625rem;
		opacity: 0.6;
	}

	.compact-view {
		display: flex;
		align-items: center;
	}

	.limit-badge {
		padding: 0.125rem 0.375rem;
		border-radius: 0.25rem;
		background: var(--bg-tertiary, #e5e7eb);
		font-weight: 500;
	}

	:global(.dark) .limit-badge {
		background: var(--bg-tertiary, #4b5563);
	}

	.limit-badge.warning {
		background: #fef3c7;
		color: #92400e;
	}

	:global(.dark) .limit-badge.warning {
		background: rgba(245, 158, 11, 0.2);
		color: #fcd34d;
	}

	.limit-badge.critical {
		background: #fee2e2;
		color: #991b1b;
	}

	:global(.dark) .limit-badge.critical {
		background: rgba(239, 68, 68, 0.2);
		color: #fca5a5;
	}

	.detailed-info {
		margin-top: 0.5rem;
		padding-top: 0.5rem;
		border-top: 1px solid var(--border-color, #e5e7eb);
	}

	:global(.dark) .detailed-info {
		border-color: var(--border-color, #374151);
	}

	.info-row {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
	}

	.warning-message {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-top: 0.5rem;
		padding: 0.375rem 0.5rem;
		background: #fee2e2;
		border-radius: 0.25rem;
		color: #991b1b;
		font-weight: 500;
	}

	:global(.dark) .warning-message {
		background: rgba(239, 68, 68, 0.15);
		color: #fca5a5;
	}

	.warning-message svg {
		width: 1rem;
		height: 1rem;
		flex-shrink: 0;
	}
</style>

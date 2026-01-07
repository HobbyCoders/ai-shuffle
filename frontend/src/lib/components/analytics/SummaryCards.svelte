<script lang="ts">
	import type { AnalyticsSummary } from '$lib/api/client';

	export let summary: AnalyticsSummary | null = null;
	export let loading = false;

	// Format number
	function formatNumber(n: number): string {
		if (n >= 1000000) return (n / 1000000).toFixed(2) + 'M';
		if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
		return n.toLocaleString();
	}

	// Format currency
	function formatCost(cost: number): string {
		if (cost >= 1) return '$' + cost.toFixed(2);
		return '$' + cost.toFixed(4);
	}

	// Card definitions with semantic colors
	$: cards = [
		{
			title: 'Total Tokens Used',
			value: summary ? formatNumber(summary.total_input_tokens + summary.total_output_tokens) : '-',
			subtitle: summary ? `${formatNumber(summary.total_input_tokens)} in / ${formatNumber(summary.total_output_tokens)} out` : '',
			icon: 'M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z',
			colorClass: 'summary-icon-info'
		},
		{
			title: 'Total Cost',
			value: summary ? formatCost(summary.total_cost) : '-',
			subtitle: 'USD',
			icon: 'M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
			colorClass: 'summary-icon-success'
		},
		{
			title: 'Sessions',
			value: summary ? summary.session_count.toString() : '-',
			subtitle: 'conversations',
			icon: 'M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z',
			colorClass: 'summary-icon-primary'
		},
		{
			title: 'Avg Cost/Session',
			value: summary ? formatCost(summary.average_cost_per_session) : '-',
			subtitle: 'per conversation',
			icon: 'M9 7h6m0 10v-3m-3 3h.01M9 17h.01M9 14h.01M12 14h.01M15 11h.01M12 11h.01M9 11h.01M7 21h10a2 2 0 002-2V5a2 2 0 00-2-2H7a2 2 0 00-2 2v14a2 2 0 002 2z',
			colorClass: 'summary-icon-warning'
		}
	];
</script>

<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
	{#each cards as card}
		<div class="summary-card">
			{#if loading}
				<div class="animate-pulse">
					<div class="summary-skeleton summary-skeleton-title"></div>
					<div class="summary-skeleton summary-skeleton-value"></div>
					<div class="summary-skeleton summary-skeleton-subtitle"></div>
				</div>
			{:else}
				<div class="flex items-start justify-between">
					<div class="summary-content">
						<p class="summary-title">{card.title}</p>
						<p class="summary-value">{card.value}</p>
						<p class="summary-subtitle">{card.subtitle}</p>
					</div>
					<div class="summary-icon {card.colorClass}">
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={card.icon} />
						</svg>
					</div>
				</div>
			{/if}
		</div>
	{/each}
</div>

<style>
	/* Summary card base styles */
	.summary-card {
		background-color: var(--card);
		border: 1px solid var(--border);
		border-radius: 0.75rem;
		padding: 1rem;
		box-shadow: var(--shadow-s);
		transition: all 200ms ease;
	}

	.summary-card:hover {
		border-color: color-mix(in oklch, var(--primary) 30%, var(--border));
		box-shadow: var(--shadow-m);
		transform: translateY(-2px);
	}

	/* Loading skeleton styles */
	.summary-skeleton {
		border-radius: 0.25rem;
		background-color: var(--muted);
	}

	.summary-skeleton-title {
		height: 1rem;
		width: 6rem;
		margin-bottom: 0.75rem;
	}

	.summary-skeleton-value {
		height: 2rem;
		width: 8rem;
		margin-bottom: 0.5rem;
	}

	.summary-skeleton-subtitle {
		height: 0.75rem;
		width: 5rem;
	}

	/* Content styles */
	.summary-content {
		flex: 1;
		min-width: 0;
	}

	.summary-title {
		font-size: 0.875rem;
		color: var(--muted-foreground);
		margin-bottom: 0.25rem;
		font-weight: 500;
	}

	.summary-value {
		font-size: 1.5rem;
		font-weight: 700;
		color: var(--foreground);
		line-height: 1.2;
	}

	.summary-subtitle {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin-top: 0.25rem;
	}

	/* Icon container base */
	.summary-icon {
		padding: 0.5rem;
		border-radius: 0.5rem;
		flex-shrink: 0;
	}

	/* Icon color variants using semantic colors */
	.summary-icon-info {
		background-color: color-mix(in oklch, var(--info) 15%, transparent);
		color: var(--info);
	}

	.summary-icon-success {
		background-color: color-mix(in oklch, var(--success) 15%, transparent);
		color: var(--success);
	}

	.summary-icon-primary {
		background-color: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
	}

	.summary-icon-warning {
		background-color: color-mix(in oklch, var(--warning) 15%, transparent);
		color: var(--warning);
	}
</style>

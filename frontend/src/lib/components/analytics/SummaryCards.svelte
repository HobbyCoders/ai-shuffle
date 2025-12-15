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

	// Card definitions
	$: cards = [
		{
			title: 'Total Tokens Used',
			value: summary ? formatNumber(summary.total_input_tokens + summary.total_output_tokens) : '-',
			subtitle: summary ? `${formatNumber(summary.total_input_tokens)} in / ${formatNumber(summary.total_output_tokens)} out` : '',
			icon: 'M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z',
			color: 'text-blue-400'
		},
		{
			title: 'Total Cost',
			value: summary ? formatCost(summary.total_cost) : '-',
			subtitle: 'USD',
			icon: 'M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
			color: 'text-green-400'
		},
		{
			title: 'Sessions',
			value: summary ? summary.session_count.toString() : '-',
			subtitle: 'conversations',
			icon: 'M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z',
			color: 'text-purple-400'
		},
		{
			title: 'Avg Cost/Session',
			value: summary ? formatCost(summary.average_cost_per_session) : '-',
			subtitle: 'per conversation',
			icon: 'M9 7h6m0 10v-3m-3 3h.01M9 17h.01M9 14h.01M12 14h.01M15 11h.01M12 11h.01M9 11h.01M7 21h10a2 2 0 002-2V5a2 2 0 00-2-2H7a2 2 0 00-2 2v14a2 2 0 002 2z',
			color: 'text-orange-400'
		}
	];
</script>

<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
	{#each cards as card}
		<div class="bg-card border border-border rounded-xl p-4 transition-all hover:border-primary/30">
			{#if loading}
				<div class="animate-pulse">
					<div class="h-4 bg-muted rounded w-24 mb-3"></div>
					<div class="h-8 bg-muted rounded w-32 mb-2"></div>
					<div class="h-3 bg-muted rounded w-20"></div>
				</div>
			{:else}
				<div class="flex items-start justify-between">
					<div>
						<p class="text-sm text-muted-foreground mb-1">{card.title}</p>
						<p class="text-2xl font-bold text-foreground">{card.value}</p>
						<p class="text-xs text-muted-foreground mt-1">{card.subtitle}</p>
					</div>
					<div class="p-2 rounded-lg bg-muted/50 {card.color}">
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={card.icon} />
						</svg>
					</div>
				</div>
			{/if}
		</div>
	{/each}
</div>

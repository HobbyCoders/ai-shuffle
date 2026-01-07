<script lang="ts">
	import type { CostBreakdownItem } from '$lib/api/client';

	export let data: CostBreakdownItem[] = [];
	export let loading = false;
	export let groupBy: 'profile' | 'user' = 'profile';
	export let onGroupByChange: (value: 'profile' | 'user') => void = () => {};

	// Colors for bars
	const colors = [
		'bg-primary',
		'bg-blue-500',
		'bg-purple-500',
		'bg-pink-500',
		'bg-orange-500',
		'bg-green-500',
		'bg-yellow-500',
		'bg-red-500'
	];

	// Format currency
	function formatCost(cost: number): string {
		return '$' + cost.toFixed(4);
	}

	// Format number
	function formatNumber(n: number): string {
		if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
		if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
		return n.toString();
	}

	// Get max cost for bar scaling
	$: maxCost = Math.max(...data.map(d => d.cost), 0.0001);
</script>

<div class="w-full">
	<div class="flex items-center justify-between mb-4">
		<h3 class="text-lg font-semibold text-foreground">Cost Breakdown</h3>
		<div class="flex items-center gap-2">
			<button
				class="px-3 py-1.5 text-sm rounded-lg transition-all {groupBy === 'profile' ? 'bg-primary text-primary-foreground' : 'bg-card border border-border text-muted-foreground hover:text-foreground'}"
				on:click={() => onGroupByChange('profile')}
			>
				By Profile
			</button>
			<button
				class="px-3 py-1.5 text-sm rounded-lg transition-all {groupBy === 'user' ? 'bg-primary text-primary-foreground' : 'bg-card border border-border text-muted-foreground hover:text-foreground'}"
				on:click={() => onGroupByChange('user')}
			>
				By User
			</button>
		</div>
	</div>

	{#if loading}
		<div class="h-[200px] flex items-center justify-center">
			<div class="animate-spin w-6 h-6 border-2 border-primary border-t-transparent rounded-full"></div>
		</div>
	{:else if data.length === 0}
		<div class="h-[200px] flex items-center justify-center bg-card rounded-xl border border-border">
			<div class="text-center text-muted-foreground">
				<svg class="w-12 h-12 mx-auto mb-2 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 3.055A9.001 9.001 0 1020.945 13H11V3.055z" />
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.488 9H15V3.512A9.025 9.025 0 0120.488 9z" />
				</svg>
				<p>No cost data for this period</p>
			</div>
		</div>
	{:else}
		<div class="bg-card rounded-xl border border-border p-4 space-y-3">
			{#each data as item, i}
				{@const barWidth = (item.cost / maxCost) * 100}
				<div class="flex items-center gap-4">
					<div class="w-32 text-sm text-foreground truncate" title={item.name}>
						{item.name || 'Unknown'}
					</div>
					<div class="flex-1 relative h-8">
						<div
							class="absolute inset-y-0 left-0 rounded {colors[i % colors.length]}"
							style="width: {barWidth}%"
						></div>
						<div class="absolute inset-y-0 left-0 right-0 flex items-center justify-between px-2">
							<span class="text-xs font-medium text-primary-foreground mix-blend-difference">
								{formatCost(item.cost)}
							</span>
							<span class="text-xs text-muted-foreground">
								{formatNumber(item.tokens)} tokens ({item.percentage.toFixed(1)}%)
							</span>
						</div>
					</div>
				</div>
			{/each}
		</div>
	{/if}
</div>

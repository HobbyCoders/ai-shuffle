<script lang="ts">
	import type { TrendDataPoint } from '$lib/api/client';

	export let data: TrendDataPoint[] = [];
	export let loading = false;

	// Chart dimensions
	const chartHeight = 200;
	const chartPadding = { top: 20, right: 20, bottom: 40, left: 60 };

	// Calculate chart values
	$: chartWidth = 600; // Will be responsive via viewBox
	$: innerWidth = chartWidth - chartPadding.left - chartPadding.right;
	$: innerHeight = chartHeight - chartPadding.top - chartPadding.bottom;

	// Calculate max values for scaling
	$: maxTokens = Math.max(
		...data.map(d => Math.max(d.input_tokens, d.output_tokens)),
		1 // Prevent division by zero
	);

	// Generate bar positions
	$: barWidth = data.length > 0 ? Math.max(innerWidth / data.length - 4, 8) : 20;
	$: barGroupWidth = data.length > 0 ? innerWidth / data.length : 20;

	// Format number for display
	function formatNumber(n: number): string {
		if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
		if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
		return n.toString();
	}

	// Format date for display
	function formatDate(dateStr: string): string {
		const date = new Date(dateStr);
		return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
	}

	// Get Y position for a value
	function getY(value: number): number {
		return chartPadding.top + innerHeight - (value / maxTokens) * innerHeight;
	}

	// Calculate Y-axis ticks
	$: yTicks = [0, maxTokens * 0.25, maxTokens * 0.5, maxTokens * 0.75, maxTokens];
</script>

<div class="w-full">
	<div class="flex items-center justify-between mb-4">
		<h3 class="text-lg font-semibold text-foreground">Token Usage Over Time</h3>
		<div class="flex items-center gap-4 text-sm">
			<div class="flex items-center gap-2">
				<div class="w-3 h-3 rounded bg-primary"></div>
				<span class="text-muted-foreground">Input Tokens</span>
			</div>
			<div class="flex items-center gap-2">
				<div class="w-3 h-3 rounded bg-primary/50"></div>
				<span class="text-muted-foreground">Output Tokens</span>
			</div>
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
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
				</svg>
				<p>No usage data for this period</p>
			</div>
		</div>
	{:else}
		<div class="bg-card rounded-xl border border-border p-4 overflow-x-auto">
			<svg viewBox="0 0 {chartWidth} {chartHeight}" class="w-full" style="min-width: 400px;">
				<!-- Y-axis grid lines and labels -->
				{#each yTicks as tick}
					<line
						x1={chartPadding.left}
						y1={getY(tick)}
						x2={chartWidth - chartPadding.right}
						y2={getY(tick)}
						stroke="currentColor"
						stroke-opacity="0.1"
						stroke-dasharray="4 4"
					/>
					<text
						x={chartPadding.left - 8}
						y={getY(tick)}
						text-anchor="end"
						dominant-baseline="middle"
						class="text-[10px] fill-muted-foreground"
					>
						{formatNumber(tick)}
					</text>
				{/each}

				<!-- Bars -->
				{#each data as point, i}
					{@const x = chartPadding.left + i * barGroupWidth + barGroupWidth / 2}
					{@const inputHeight = (point.input_tokens / maxTokens) * innerHeight}
					{@const outputHeight = (point.output_tokens / maxTokens) * innerHeight}

					<!-- Input tokens bar -->
					<rect
						x={x - barWidth / 2 - 2}
						y={chartPadding.top + innerHeight - inputHeight}
						width={barWidth / 2 - 1}
						height={inputHeight}
						rx="2"
						class="fill-primary"
					/>

					<!-- Output tokens bar -->
					<rect
						x={x + 2}
						y={chartPadding.top + innerHeight - outputHeight}
						width={barWidth / 2 - 1}
						height={outputHeight}
						rx="2"
						class="fill-primary/50"
					/>

					<!-- X-axis label -->
					{#if data.length <= 14 || i % Math.ceil(data.length / 7) === 0}
						<text
							x={x}
							y={chartHeight - 10}
							text-anchor="middle"
							class="text-[10px] fill-muted-foreground"
						>
							{formatDate(point.date)}
						</text>
					{/if}
				{/each}

				<!-- Axis lines -->
				<line
					x1={chartPadding.left}
					y1={chartPadding.top + innerHeight}
					x2={chartWidth - chartPadding.right}
					y2={chartPadding.top + innerHeight}
					stroke="currentColor"
					stroke-opacity="0.2"
				/>
				<line
					x1={chartPadding.left}
					y1={chartPadding.top}
					x2={chartPadding.left}
					y2={chartPadding.top + innerHeight}
					stroke="currentColor"
					stroke-opacity="0.2"
				/>
			</svg>
		</div>
	{/if}
</div>

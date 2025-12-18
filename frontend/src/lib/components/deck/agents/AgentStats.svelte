<script lang="ts">
	/**
	 * AgentStats - Statistics panel
	 *
	 * Features:
	 * - Time filter: Today, Week, Month, All Time
	 * - Stats cards: Total agents, Success rate, Avg time, Running
	 * - 7-day activity bar chart
	 */
	import { Rocket, TrendingUp, Clock, Play } from 'lucide-svelte';

	// State
	let timeFilter = $state<'today' | 'week' | 'month' | 'all'>('week');

	// Mock stats data
	const stats = {
		today: {
			total: 5,
			success: 4,
			failed: 1,
			avgTime: 1800000, // 30 minutes
			running: 2
		},
		week: {
			total: 28,
			success: 24,
			failed: 4,
			avgTime: 2400000, // 40 minutes
			running: 2
		},
		month: {
			total: 95,
			success: 82,
			failed: 13,
			avgTime: 2700000, // 45 minutes
			running: 2
		},
		all: {
			total: 312,
			success: 275,
			failed: 37,
			avgTime: 2520000, // 42 minutes
			running: 2
		}
	};

	// 7-day activity data
	const activityData = [
		{ day: 'Mon', count: 8, success: 7, failed: 1 },
		{ day: 'Tue', count: 5, success: 5, failed: 0 },
		{ day: 'Wed', count: 3, success: 2, failed: 1 },
		{ day: 'Thu', count: 6, success: 5, failed: 1 },
		{ day: 'Fri', count: 4, success: 4, failed: 0 },
		{ day: 'Sat', count: 1, success: 1, failed: 0 },
		{ day: 'Sun', count: 1, success: 0, failed: 1 }
	];

	const maxCount = Math.max(...activityData.map(d => d.count));

	const currentStats = $derived(stats[timeFilter]);
	const successRate = $derived(currentStats.total > 0 ? Math.round((currentStats.success / currentStats.total) * 100) : 0);

	// Format duration
	function formatDuration(ms: number): string {
		const minutes = Math.floor(ms / 60000);
		if (minutes >= 60) {
			const hours = Math.floor(minutes / 60);
			const mins = minutes % 60;
			return `${hours}h ${mins}m`;
		}
		return `${minutes}m`;
	}

	// Time filter options
	const filterOptions: { value: typeof timeFilter; label: string }[] = [
		{ value: 'today', label: 'Today' },
		{ value: 'week', label: 'Week' },
		{ value: 'month', label: 'Month' },
		{ value: 'all', label: 'All Time' }
	];
</script>

<div class="h-full overflow-y-auto p-4 space-y-6">
	<!-- Time filter -->
	<div class="flex items-center gap-1 p-1 bg-muted rounded-lg w-fit">
		{#each filterOptions as option}
			<button
				onclick={() => timeFilter = option.value}
				class="px-3 py-1.5 text-sm font-medium rounded-md transition-colors {timeFilter === option.value ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
			>
				{option.label}
			</button>
		{/each}
	</div>

	<!-- Stats cards -->
	<div class="grid grid-cols-2 gap-3">
		<!-- Total agents -->
		<div class="p-4 bg-card border border-border rounded-xl">
			<div class="flex items-center gap-2 text-muted-foreground mb-2">
				<Rocket class="w-4 h-4" />
				<span class="text-xs font-medium">Total Agents</span>
			</div>
			<p class="text-2xl font-bold text-foreground">{currentStats.total}</p>
			<p class="text-xs text-muted-foreground mt-1">
				{currentStats.success} completed, {currentStats.failed} failed
			</p>
		</div>

		<!-- Success rate -->
		<div class="p-4 bg-card border border-border rounded-xl">
			<div class="flex items-center gap-2 text-muted-foreground mb-2">
				<TrendingUp class="w-4 h-4" />
				<span class="text-xs font-medium">Success Rate</span>
			</div>
			<p class="text-2xl font-bold {successRate >= 80 ? 'text-green-500' : successRate >= 60 ? 'text-yellow-500' : 'text-red-500'}">
				{successRate}%
			</p>
			<div class="h-1.5 bg-muted rounded-full overflow-hidden mt-2">
				<div
					class="h-full rounded-full transition-all {successRate >= 80 ? 'bg-green-500' : successRate >= 60 ? 'bg-yellow-500' : 'bg-red-500'}"
					style:width="{successRate}%"
				></div>
			</div>
		</div>

		<!-- Average time -->
		<div class="p-4 bg-card border border-border rounded-xl">
			<div class="flex items-center gap-2 text-muted-foreground mb-2">
				<Clock class="w-4 h-4" />
				<span class="text-xs font-medium">Avg. Completion</span>
			</div>
			<p class="text-2xl font-bold text-foreground">{formatDuration(currentStats.avgTime)}</p>
			<p class="text-xs text-muted-foreground mt-1">
				Per agent task
			</p>
		</div>

		<!-- Currently running -->
		<div class="p-4 bg-card border border-border rounded-xl">
			<div class="flex items-center gap-2 text-muted-foreground mb-2">
				<Play class="w-4 h-4" />
				<span class="text-xs font-medium">Running Now</span>
			</div>
			<p class="text-2xl font-bold text-blue-500">{currentStats.running}</p>
			<p class="text-xs text-muted-foreground mt-1">
				Active agents
			</p>
		</div>
	</div>

	<!-- 7-day activity chart -->
	<div class="p-4 bg-card border border-border rounded-xl">
		<h3 class="text-sm font-medium text-foreground mb-4">7-Day Activity</h3>

		<div class="flex items-end justify-between gap-2 h-32">
			{#each activityData as day}
				{@const height = maxCount > 0 ? (day.count / maxCount) * 100 : 0}
				{@const successHeight = day.count > 0 ? (day.success / day.count) * 100 : 0}

				<div class="flex-1 flex flex-col items-center gap-2">
					<!-- Bar -->
					<div class="w-full relative flex flex-col-reverse" style:height="100%">
						<div
							class="w-full bg-muted rounded-t relative overflow-hidden transition-all"
							style:height="{height}%"
						>
							<!-- Success portion -->
							<div
								class="absolute bottom-0 left-0 right-0 bg-green-500/80"
								style:height="{successHeight}%"
							></div>
							<!-- Failed portion -->
							<div
								class="absolute top-0 left-0 right-0 bg-red-500/80"
								style:height="{100 - successHeight}%"
							></div>
						</div>
					</div>

					<!-- Label -->
					<span class="text-xs text-muted-foreground">{day.day}</span>
				</div>
			{/each}
		</div>

		<!-- Legend -->
		<div class="flex items-center justify-center gap-4 mt-4 text-xs">
			<div class="flex items-center gap-1.5">
				<div class="w-3 h-3 rounded bg-green-500/80"></div>
				<span class="text-muted-foreground">Success</span>
			</div>
			<div class="flex items-center gap-1.5">
				<div class="w-3 h-3 rounded bg-red-500/80"></div>
				<span class="text-muted-foreground">Failed</span>
			</div>
		</div>
	</div>

	<!-- Recent summary -->
	<div class="p-4 bg-card border border-border rounded-xl">
		<h3 class="text-sm font-medium text-foreground mb-3">Quick Summary</h3>
		<div class="space-y-2 text-sm">
			<div class="flex items-center justify-between">
				<span class="text-muted-foreground">Most productive day</span>
				<span class="text-foreground font-medium">Monday (8 agents)</span>
			</div>
			<div class="flex items-center justify-between">
				<span class="text-muted-foreground">Fastest completion</span>
				<span class="text-foreground font-medium">12 minutes</span>
			</div>
			<div class="flex items-center justify-between">
				<span class="text-muted-foreground">Longest running</span>
				<span class="text-foreground font-medium">2h 15m</span>
			</div>
			<div class="flex items-center justify-between">
				<span class="text-muted-foreground">Most common error</span>
				<span class="text-foreground font-medium">Timeout (3)</span>
			</div>
		</div>
	</div>
</div>

<script lang="ts">
	/**
	 * AgentStats - Statistics panel
	 *
	 * Features:
	 * - Time filter: Today, Week, Month, All Time
	 * - Stats cards: Total agents, Success rate, Avg time, Running
	 * - 7-day activity bar chart
	 */
	import { onMount } from 'svelte';
	import { Rocket, TrendingUp, Clock, Play, Loader2, AlertCircle } from 'lucide-svelte';
	import { agents, agentStats, runningCount } from '$lib/stores/agents';
	import type { AgentStats } from '$lib/stores/agents';

	// State
	let timeFilter = $state<'today' | 'week' | 'month' | 'all'>('week');
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Derived from store
	const stats = $derived($agentStats);
	const running = $derived($runningCount);

	// Time filter to days mapping
	const filterToDays: Record<typeof timeFilter, number> = {
		today: 1,
		week: 7,
		month: 30,
		all: 365
	};

	// Fetch stats on mount and when filter changes
	onMount(() => {
		fetchStats();
	});

	$effect(() => {
		// Re-fetch when timeFilter changes
		timeFilter;
		fetchStats();
	});

	async function fetchStats() {
		loading = true;
		error = null;
		try {
			await agents.fetchStats(filterToDays[timeFilter]);
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load stats';
		} finally {
			loading = false;
		}
	}

	// Calculate success rate
	const successRate = $derived(() => {
		if (!stats) return 0;
		return stats.total > 0 ? Math.round((stats.completed / stats.total) * 100) : 0;
	});

	// Calculate activity data from byDay
	const activityData = $derived(() => {
		if (!stats?.byDay) return [];

		// Get the last 7 days
		const days: { day: string; count: number; label: string }[] = [];
		const dayLabels = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

		for (let i = 6; i >= 0; i--) {
			const date = new Date();
			date.setDate(date.getDate() - i);
			const dateKey = date.toISOString().split('T')[0];
			const dayOfWeek = date.getDay();

			days.push({
				day: dayLabels[dayOfWeek],
				count: stats.byDay[dateKey] ?? 0,
				label: dateKey
			});
		}

		return days;
	});

	const maxCount = $derived(() => {
		const data = activityData();
		return data.length > 0 ? Math.max(...data.map(d => d.count), 1) : 1;
	});

	// Format duration
	function formatDuration(minutes: number): string {
		if (minutes >= 60) {
			const hours = Math.floor(minutes / 60);
			const mins = Math.round(minutes % 60);
			return `${hours}h ${mins}m`;
		}
		return `${Math.round(minutes)}m`;
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

	{#if loading}
		<div class="flex items-center justify-center py-12">
			<Loader2 class="w-8 h-8 text-primary animate-spin" />
		</div>
	{:else if error}
		<div class="flex flex-col items-center justify-center py-12 text-center">
			<AlertCircle class="w-8 h-8 text-red-500 mb-2" />
			<p class="text-sm text-red-500">{error}</p>
			<button
				onclick={fetchStats}
				class="mt-2 text-sm text-primary hover:underline"
			>
				Retry
			</button>
		</div>
	{:else if stats}
		<!-- Stats cards -->
		<div class="grid grid-cols-2 gap-3">
			<!-- Total agents -->
			<div class="p-4 bg-card border border-border rounded-xl">
				<div class="flex items-center gap-2 text-muted-foreground mb-2">
					<Rocket class="w-4 h-4" />
					<span class="text-xs font-medium">Total Agents</span>
				</div>
				<p class="text-2xl font-bold text-foreground">{stats.total}</p>
				<p class="text-xs text-muted-foreground mt-1">
					{stats.completed} completed, {stats.failed} failed
				</p>
			</div>

			<!-- Success rate -->
			<div class="p-4 bg-card border border-border rounded-xl">
				<div class="flex items-center gap-2 text-muted-foreground mb-2">
					<TrendingUp class="w-4 h-4" />
					<span class="text-xs font-medium">Success Rate</span>
				</div>
				<p class="text-2xl font-bold {successRate() >= 80 ? 'text-green-500' : successRate() >= 60 ? 'text-yellow-500' : 'text-red-500'}">
					{successRate()}%
				</p>
				<div class="h-1.5 bg-muted rounded-full overflow-hidden mt-2">
					<div
						class="h-full rounded-full transition-all {successRate() >= 80 ? 'bg-green-500' : successRate() >= 60 ? 'bg-yellow-500' : 'bg-red-500'}"
						style:width="{successRate()}%"
					></div>
				</div>
			</div>

			<!-- Average time -->
			<div class="p-4 bg-card border border-border rounded-xl">
				<div class="flex items-center gap-2 text-muted-foreground mb-2">
					<Clock class="w-4 h-4" />
					<span class="text-xs font-medium">Avg. Completion</span>
				</div>
				<p class="text-2xl font-bold text-foreground">{formatDuration(stats.avgDurationMinutes)}</p>
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
				<p class="text-2xl font-bold text-blue-500">{running}</p>
				<p class="text-xs text-muted-foreground mt-1">
					{stats.queued} queued
				</p>
			</div>
		</div>

		<!-- 7-day activity chart -->
		<div class="p-4 bg-card border border-border rounded-xl">
			<h3 class="text-sm font-medium text-foreground mb-4">7-Day Activity</h3>

			{#if activityData().length > 0}
				<div class="flex items-end justify-between gap-2 h-32">
					{#each activityData() as day}
						{@const height = maxCount() > 0 ? (day.count / maxCount()) * 100 : 0}

						<div class="flex-1 flex flex-col items-center gap-2">
							<!-- Bar -->
							<div class="w-full relative flex flex-col-reverse" style:height="100%">
								<div
									class="w-full bg-primary/80 rounded-t transition-all"
									style:height="{height}%"
									title="{day.label}: {day.count} agents"
								></div>
							</div>

							<!-- Count label -->
							<span class="text-xs text-muted-foreground font-medium">{day.count}</span>

							<!-- Day label -->
							<span class="text-xs text-muted-foreground">{day.day}</span>
						</div>
					{/each}
				</div>
			{:else}
				<div class="flex items-center justify-center h-32 text-muted-foreground text-sm">
					No activity data available
				</div>
			{/if}
		</div>

		<!-- Quick summary -->
		<div class="p-4 bg-card border border-border rounded-xl">
			<h3 class="text-sm font-medium text-foreground mb-3">Quick Summary</h3>
			<div class="space-y-2 text-sm">
				<div class="flex items-center justify-between">
					<span class="text-muted-foreground">Total completed</span>
					<span class="text-foreground font-medium">{stats.completed} agents</span>
				</div>
				<div class="flex items-center justify-between">
					<span class="text-muted-foreground">Total failed</span>
					<span class="text-foreground font-medium">{stats.failed} agents</span>
				</div>
				<div class="flex items-center justify-between">
					<span class="text-muted-foreground">Average duration</span>
					<span class="text-foreground font-medium">{formatDuration(stats.avgDurationMinutes)}</span>
				</div>
				<div class="flex items-center justify-between">
					<span class="text-muted-foreground">Success rate</span>
					<span class="text-foreground font-medium {successRate() >= 80 ? 'text-green-500' : successRate() >= 60 ? 'text-yellow-500' : 'text-red-500'}">
						{successRate()}%
					</span>
				</div>
			</div>
		</div>
	{:else}
		<div class="flex flex-col items-center justify-center py-12 text-center">
			<Rocket class="w-12 h-12 text-muted-foreground/50 mb-3" />
			<p class="text-sm text-muted-foreground">No stats available</p>
			<p class="text-xs text-muted-foreground/70 mt-1">Launch some agents to see statistics</p>
		</div>
	{/if}
</div>

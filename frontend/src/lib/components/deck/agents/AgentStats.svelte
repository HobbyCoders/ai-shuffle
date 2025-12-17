<script lang="ts">
	/**
	 * AgentStats - Statistics panel for agent operations
	 *
	 * Features:
	 * - Total agents run (today/week/all time)
	 * - Success rate percentage
	 * - Average completion time
	 * - Most common task types
	 * - Cost breakdown
	 * - Simple charts/graphs
	 */
	import type { BackgroundAgent } from '$lib/stores/agents';

	interface Props {
		agents: BackgroundAgent[];
	}

	let { agents }: Props = $props();

	// Time filters
	let timeFilter = $state<'today' | 'week' | 'month' | 'all'>('week');

	// Filter agents by time
	const filteredAgents = $derived.by(() => {
		const now = new Date();
		const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
		const weekAgo = new Date(today.getTime() - 7 * 24 * 60 * 60 * 1000);
		const monthAgo = new Date(today.getTime() - 30 * 24 * 60 * 60 * 1000);

		switch (timeFilter) {
			case 'today':
				return agents.filter(a => a.startedAt >= today);
			case 'week':
				return agents.filter(a => a.startedAt >= weekAgo);
			case 'month':
				return agents.filter(a => a.startedAt >= monthAgo);
			default:
				return agents;
		}
	});

	// Calculate statistics
	const stats = $derived.by(() => {
		const completed = filteredAgents.filter(a => a.status === 'completed');
		const failed = filteredAgents.filter(a => a.status === 'failed');
		const running = filteredAgents.filter(a => a.status === 'running');
		const queued = filteredAgents.filter(a => a.status === 'queued');

		// Success rate
		const totalFinished = completed.length + failed.length;
		const successRate = totalFinished > 0 ? Math.round((completed.length / totalFinished) * 100) : 0;

		// Average completion time (only completed agents)
		let avgDuration = 0;
		if (completed.length > 0) {
			const totalDuration = completed.reduce((sum, a) => {
				if (a.completedAt) {
					return sum + (a.completedAt.getTime() - a.startedAt.getTime());
				}
				return sum;
			}, 0);
			avgDuration = Math.round(totalDuration / completed.length / 1000); // in seconds
		}

		// Task type distribution (mock - based on agent names)
		const taskTypes = new Map<string, number>();
		filteredAgents.forEach(a => {
			const type = categorizeAgent(a.name);
			taskTypes.set(type, (taskTypes.get(type) || 0) + 1);
		});

		return {
			total: filteredAgents.length,
			completed: completed.length,
			failed: failed.length,
			running: running.length,
			queued: queued.length,
			successRate,
			avgDuration,
			taskTypes: Array.from(taskTypes.entries()).sort((a, b) => b[1] - a[1]).slice(0, 5)
		};
	});

	// Categorize agent by name
	function categorizeAgent(name: string): string {
		const lower = name.toLowerCase();
		if (lower.includes('feature') || lower.includes('implement')) return 'Feature';
		if (lower.includes('bug') || lower.includes('fix')) return 'Bug Fix';
		if (lower.includes('refactor')) return 'Refactor';
		if (lower.includes('test')) return 'Testing';
		if (lower.includes('doc')) return 'Documentation';
		if (lower.includes('review') || lower.includes('pr')) return 'Code Review';
		return 'Other';
	}

	// Format duration
	function formatDuration(seconds: number): string {
		if (seconds < 60) return `${seconds}s`;

		const minutes = Math.floor(seconds / 60);
		if (minutes < 60) return `${minutes}m`;

		const hours = Math.floor(minutes / 60);
		const remainingMinutes = minutes % 60;
		return `${hours}h ${remainingMinutes}m`;
	}

	// Get color for task type
	function getTypeColor(type: string): string {
		const colors: Record<string, string> = {
			'Feature': 'bg-cyan-500',
			'Bug Fix': 'bg-red-500',
			'Refactor': 'bg-violet-500',
			'Testing': 'bg-emerald-500',
			'Documentation': 'bg-amber-500',
			'Code Review': 'bg-blue-500',
			'Other': 'bg-gray-500'
		};
		return colors[type] || 'bg-gray-500';
	}
</script>

<div class="agent-stats p-4 space-y-6">
	<!-- Time filter -->
	<div class="flex items-center justify-between">
		<h3 class="text-sm font-medium text-foreground">Agent Statistics</h3>
		<div class="flex gap-1 bg-muted rounded-lg p-0.5">
			{#each [
				{ id: 'today', label: 'Today' },
				{ id: 'week', label: 'Week' },
				{ id: 'month', label: 'Month' },
				{ id: 'all', label: 'All' }
			] as filter}
				<button
					type="button"
					onclick={() => timeFilter = filter.id as typeof timeFilter}
					class="px-3 py-1 text-xs rounded-md transition-colors {timeFilter === filter.id ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
				>
					{filter.label}
				</button>
			{/each}
		</div>
	</div>

	<!-- Main stats grid -->
	<div class="grid grid-cols-2 md:grid-cols-4 gap-4">
		<!-- Total -->
		<div class="stat-card bg-muted/30 rounded-xl p-4 border border-border/50">
			<div class="text-2xl font-bold text-foreground">{stats.total}</div>
			<div class="text-xs text-muted-foreground mt-1">Total Agents</div>
			<div class="flex items-center gap-2 mt-2 text-[10px]">
				{#if stats.running > 0}
					<span class="flex items-center gap-1 text-cyan-500">
						<span class="w-1.5 h-1.5 rounded-full bg-cyan-500 animate-pulse"></span>
						{stats.running} running
					</span>
				{/if}
				{#if stats.queued > 0}
					<span class="text-amber-500">{stats.queued} queued</span>
				{/if}
			</div>
		</div>

		<!-- Success Rate -->
		<div class="stat-card bg-muted/30 rounded-xl p-4 border border-border/50">
			<div class="text-2xl font-bold {stats.successRate >= 80 ? 'text-emerald-500' : stats.successRate >= 50 ? 'text-amber-500' : 'text-red-500'}">
				{stats.successRate}%
			</div>
			<div class="text-xs text-muted-foreground mt-1">Success Rate</div>
			<div class="mt-2 h-1.5 bg-muted rounded-full overflow-hidden">
				<div
					class="h-full rounded-full transition-all {stats.successRate >= 80 ? 'bg-emerald-500' : stats.successRate >= 50 ? 'bg-amber-500' : 'bg-red-500'}"
					style="width: {stats.successRate}%"
				></div>
			</div>
		</div>

		<!-- Avg Duration -->
		<div class="stat-card bg-muted/30 rounded-xl p-4 border border-border/50">
			<div class="text-2xl font-bold text-foreground">{formatDuration(stats.avgDuration)}</div>
			<div class="text-xs text-muted-foreground mt-1">Avg Completion</div>
			<div class="text-[10px] text-muted-foreground mt-2">
				From {stats.completed} completed
			</div>
		</div>

		<!-- Completed / Failed -->
		<div class="stat-card bg-muted/30 rounded-xl p-4 border border-border/50">
			<div class="flex items-baseline gap-2">
				<span class="text-2xl font-bold text-emerald-500">{stats.completed}</span>
				<span class="text-muted-foreground">/</span>
				<span class="text-lg font-bold text-red-500">{stats.failed}</span>
			</div>
			<div class="text-xs text-muted-foreground mt-1">Completed / Failed</div>
		</div>
	</div>

	<!-- Task type distribution -->
	{#if stats.taskTypes.length > 0}
		<div class="border-t border-border pt-4">
			<h4 class="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-3">
				Task Distribution
			</h4>
			<div class="space-y-2">
				{#each stats.taskTypes as [type, count]}
					{@const percentage = Math.round((count / stats.total) * 100)}
					<div class="flex items-center gap-3">
						<div class="w-20 text-xs text-foreground truncate">{type}</div>
						<div class="flex-1 h-2 bg-muted rounded-full overflow-hidden">
							<div
								class="h-full rounded-full {getTypeColor(type)} transition-all"
								style="width: {percentage}%"
							></div>
						</div>
						<div class="w-12 text-xs text-muted-foreground text-right">
							{count} ({percentage}%)
						</div>
					</div>
				{/each}
			</div>
		</div>
	{/if}

	<!-- Activity chart (simple bar chart) -->
	{#if filteredAgents.length > 0}
		<div class="border-t border-border pt-4">
			<h4 class="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-3">
				Recent Activity
			</h4>
			<div class="flex items-end gap-1 h-24">
				{#each Array(7) as _, i}
					{@const date = new Date(Date.now() - (6 - i) * 24 * 60 * 60 * 1000)}
					{@const dayStart = new Date(date.getFullYear(), date.getMonth(), date.getDate())}
					{@const dayEnd = new Date(dayStart.getTime() + 24 * 60 * 60 * 1000)}
					{@const dayAgents = filteredAgents.filter(a => a.startedAt >= dayStart && a.startedAt < dayEnd)}
					{@const completed = dayAgents.filter(a => a.status === 'completed').length}
					{@const failed = dayAgents.filter(a => a.status === 'failed').length}
					{@const maxCount = Math.max(...Array(7).fill(0).map((_, j) => {
						const d = new Date(Date.now() - (6 - j) * 24 * 60 * 60 * 1000);
						const dStart = new Date(d.getFullYear(), d.getMonth(), d.getDate());
						const dEnd = new Date(dStart.getTime() + 24 * 60 * 60 * 1000);
						return filteredAgents.filter(a => a.startedAt >= dStart && a.startedAt < dEnd).length;
					}), 1)}

					<div class="flex-1 flex flex-col items-center gap-1">
						<div class="w-full flex flex-col-reverse gap-px" style="height: 72px;">
							{#if completed > 0}
								<div
									class="w-full bg-emerald-500 rounded-t transition-all"
									style="height: {(completed / maxCount) * 100}%"
								></div>
							{/if}
							{#if failed > 0}
								<div
									class="w-full bg-red-500 transition-all"
									style="height: {(failed / maxCount) * 100}%"
								></div>
							{/if}
						</div>
						<span class="text-[10px] text-muted-foreground">
							{['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][date.getDay()]}
						</span>
					</div>
				{/each}
			</div>
			<div class="flex items-center justify-center gap-4 mt-2 text-[10px] text-muted-foreground">
				<span class="flex items-center gap-1">
					<span class="w-2 h-2 bg-emerald-500 rounded"></span>
					Completed
				</span>
				<span class="flex items-center gap-1">
					<span class="w-2 h-2 bg-red-500 rounded"></span>
					Failed
				</span>
			</div>
		</div>
	{/if}

	<!-- Empty state -->
	{#if filteredAgents.length === 0}
		<div class="text-center py-8">
			<svg class="w-12 h-12 mx-auto text-muted-foreground/30 mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
			</svg>
			<p class="text-sm text-muted-foreground">No agents in this time period</p>
		</div>
	{/if}
</div>

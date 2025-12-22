<script lang="ts">
	/**
	 * AgentListItem - Individual agent in the list
	 *
	 * Features:
	 * - Status indicator with animation
	 * - Agent name and current task
	 * - Progress bar for running agents
	 * - Duration badge
	 * - Quick actions: Pause/Resume, View, Cancel
	 */
	import { Pause, Play, Eye, X, Clock, GitBranch } from 'lucide-svelte';
	import type { BackgroundAgent, AgentStatus } from '$lib/stores/agents';

	interface Props {
		agent: BackgroundAgent;
		onSelect: () => void;
		onPause?: () => void;
		onResume?: () => void;
		onCancel?: () => void;
	}

	let { agent, onSelect, onPause, onResume, onCancel }: Props = $props();

	// Status colors and styles
	const statusConfig: Record<AgentStatus, { color: string; bgColor: string; label: string; pulse: boolean }> = {
		running: { color: 'bg-blue-500', bgColor: 'bg-blue-500/10', label: 'Running', pulse: true },
		paused: { color: 'bg-yellow-500', bgColor: 'bg-yellow-500/10', label: 'Paused', pulse: false },
		completed: { color: 'bg-green-500', bgColor: 'bg-green-500/10', label: 'Completed', pulse: false },
		failed: { color: 'bg-red-500', bgColor: 'bg-red-500/10', label: 'Failed', pulse: false },
		queued: { color: 'bg-gray-400', bgColor: 'bg-gray-400/10', label: 'Queued', pulse: false }
	};

	const config = $derived(statusConfig[agent.status]);

	// Get current task description from tasks array
	const currentTask = $derived(() => {
		// Find the first in_progress task, or pending task
		function findCurrentTask(tasks: typeof agent.tasks): string | undefined {
			for (const task of tasks) {
				if (task.status === 'in_progress') {
					return task.name;
				}
				if (task.children) {
					const childTask = findCurrentTask(task.children);
					if (childTask) return childTask;
				}
			}
			return undefined;
		}
		return findCurrentTask(agent.tasks) ?? (agent.error ? agent.error : undefined);
	});

	// Format duration
	function formatDuration(ms: number): string {
		const seconds = Math.floor(ms / 1000);
		const minutes = Math.floor(seconds / 60);
		const hours = Math.floor(minutes / 60);

		if (hours > 0) {
			return `${hours}h ${minutes % 60}m`;
		} else if (minutes > 0) {
			return `${minutes}m ${seconds % 60}s`;
		} else {
			return `${seconds}s`;
		}
	}

	// Calculate elapsed time for running agents
	function getElapsedTime(): string {
		if (!agent.startedAt) return '--';
		const endTime = agent.completedAt ?? new Date();
		const elapsed = endTime.getTime() - agent.startedAt.getTime();
		return formatDuration(elapsed);
	}

	function handlePauseResume(e: Event) {
		e.stopPropagation();
		if (agent.status === 'running') {
			onPause?.();
		} else if (agent.status === 'paused') {
			onResume?.();
		}
	}

	function handleCancel(e: Event) {
		e.stopPropagation();
		onCancel?.();
	}
</script>

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div
	class="p-4 bg-card hover:bg-accent border border-border rounded-xl transition-all group cursor-pointer"
	onclick={onSelect}
>
	<div class="flex items-start gap-3">
		<!-- Status indicator -->
		<div class="relative flex-shrink-0 mt-1">
			<div class="w-3 h-3 rounded-full {config.color}" class:animate-pulse={config.pulse}></div>
			{#if config.pulse}
				<div class="absolute inset-0 w-3 h-3 rounded-full {config.color} animate-ping opacity-75"></div>
			{/if}
		</div>

		<!-- Main content -->
		<div class="flex-1 min-w-0">
			<!-- Name and status -->
			<div class="flex items-center gap-2 flex-wrap">
				<h3 class="font-medium text-foreground truncate">{agent.name}</h3>
				<span class="px-2 py-0.5 text-xs rounded-full {config.bgColor} text-foreground">
					{config.label}
				</span>
			</div>

			<!-- Task or message -->
			{#if currentTask()}
				<p class="text-sm text-muted-foreground mt-1 truncate">{currentTask()}</p>
			{:else if agent.resultSummary}
				<p class="text-sm text-muted-foreground mt-1 truncate">{agent.resultSummary}</p>
			{/if}

			<!-- Progress bar for running agents -->
			{#if agent.status === 'running' && agent.progress !== undefined}
				<div class="mt-2">
					<div class="flex items-center justify-between text-xs text-muted-foreground mb-1">
						<span>Progress</span>
						<span>{agent.progress}%</span>
					</div>
					<div class="h-1.5 bg-muted rounded-full overflow-hidden">
						<div
							class="h-full bg-blue-500 rounded-full transition-all duration-300"
							style:width="{agent.progress}%"
						></div>
					</div>
				</div>
			{/if}

			<!-- Metadata row -->
			<div class="flex items-center gap-3 mt-2">
				<!-- Duration -->
				<div class="flex items-center gap-1 text-xs text-muted-foreground">
					<Clock class="w-3 h-3" />
					<span>{getElapsedTime()}</span>
				</div>

				<!-- Branch -->
				{#if agent.branch}
					<div class="flex items-center gap-1 text-xs text-muted-foreground">
						<GitBranch class="w-3 h-3" />
						<span class="truncate max-w-[100px]">{agent.branch}</span>
					</div>
				{/if}

				<!-- PR Link -->
				{#if agent.prUrl}
					<a
						href={agent.prUrl}
						target="_blank"
						rel="noopener noreferrer"
						onclick={(e) => e.stopPropagation()}
						class="text-xs text-primary hover:underline"
					>
						View PR
					</a>
				{/if}
			</div>
		</div>

		<!-- Actions -->
		<div class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
			{#if agent.status === 'running' || agent.status === 'paused'}
				<button
					onclick={handlePauseResume}
					class="p-1.5 text-muted-foreground hover:text-foreground rounded-lg hover:bg-muted transition-colors"
					title={agent.status === 'running' ? 'Pause' : 'Resume'}
				>
					{#if agent.status === 'running'}
						<Pause class="w-4 h-4" />
					{:else}
						<Play class="w-4 h-4" />
					{/if}
				</button>
			{/if}

			<button
				onclick={(e) => { e.stopPropagation(); onSelect(); }}
				class="p-1.5 text-muted-foreground hover:text-foreground rounded-lg hover:bg-muted transition-colors"
				title="View details"
			>
				<Eye class="w-4 h-4" />
			</button>

			{#if agent.status === 'running' || agent.status === 'paused' || agent.status === 'queued'}
				<button
					onclick={handleCancel}
					class="p-1.5 text-muted-foreground hover:text-red-500 rounded-lg hover:bg-red-500/10 transition-colors"
					title="Cancel"
				>
					<X class="w-4 h-4" />
				</button>
			{/if}
		</div>
	</div>
</div>

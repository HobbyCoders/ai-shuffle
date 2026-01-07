<script lang="ts">
	/**
	 * AgentDetails - Full agent details panel
	 *
	 * Features:
	 * - Header with name and status badge
	 * - Metadata: started time, duration, branch
	 * - GitHub link for PR
	 * - Tabs: Tasks, Logs
	 * - Control buttons: Pause, Resume, Cancel, Delete
	 */
	import {
		X,
		Pause,
		Play,
		StopCircle,
		Trash2,
		Clock,
		GitBranch,
		ExternalLink,
		ListTree,
		ScrollText
	} from 'lucide-svelte';
	import TaskTree from './TaskTree.svelte';
	import AgentLogs from './AgentLogs.svelte';
	import type { BackgroundAgent, AgentStatus } from '$lib/stores/agents';

	interface Props {
		agent: BackgroundAgent;
		onClose: () => void;
		onPause?: () => void;
		onResume?: () => void;
		onCancel?: () => void;
		onDelete?: () => void;
	}

	let { agent, onClose, onPause, onResume, onCancel, onDelete }: Props = $props();

	// State
	let activeTab = $state<'tasks' | 'logs'>('tasks');

	// Status colors
	const statusConfig: Record<AgentStatus, { color: string; bgColor: string; label: string }> = {
		running: { color: 'text-blue-500', bgColor: 'bg-blue-500/10', label: 'Running' },
		paused: { color: 'text-yellow-500', bgColor: 'bg-yellow-500/10', label: 'Paused' },
		completed: { color: 'text-green-500', bgColor: 'bg-green-500/10', label: 'Completed' },
		failed: { color: 'text-red-500', bgColor: 'bg-red-500/10', label: 'Failed' },
		queued: { color: 'text-gray-400', bgColor: 'bg-gray-400/10', label: 'Queued' }
	};

	const config = $derived(statusConfig[agent.status]);

	// Get current task description from tasks array
	const currentTask = $derived(() => {
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
		return findCurrentTask(agent.tasks) ?? (agent.error ? agent.error : agent.resultSummary);
	});

	// Format time
	function formatTime(date?: Date): string {
		if (!date) return '--';
		return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
	}

	function formatDate(date?: Date): string {
		if (!date) return '--';
		return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
	}

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

	function getElapsedTime(): string {
		if (!agent.startedAt) return '--';
		const endTime = agent.completedAt ?? new Date();
		const elapsed = endTime.getTime() - agent.startedAt.getTime();
		return formatDuration(elapsed);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			onClose();
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- Panel backdrop -->
<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div
	class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] bg-black/60 backdrop-blur-sm z-50 flex items-center justify-end"
	onclick={onClose}
>
	<!-- Panel -->
	<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
	<div
		class="h-full w-full max-w-xl bg-card border-l border-border shadow-2xl flex flex-col"
		onclick={(e) => e.stopPropagation()}
	>
		<!-- Header -->
		<div class="flex items-start justify-between px-6 py-4 border-b border-border">
			<div class="flex-1 min-w-0 pr-4">
				<div class="flex items-center gap-2 flex-wrap">
					<h2 class="text-lg font-semibold text-foreground truncate">{agent.name}</h2>
					<span class="px-2 py-0.5 text-xs rounded-full {config.bgColor} {config.color}">
						{config.label}
					</span>
				</div>
				{#if currentTask()}
					<p class="text-sm text-muted-foreground mt-1 truncate">{currentTask()}</p>
				{/if}
			</div>
			<button
				onclick={onClose}
				class="p-2 hover:bg-muted rounded-lg transition-colors flex-shrink-0"
			>
				<X class="w-5 h-5 text-muted-foreground" />
			</button>
		</div>

		<!-- Metadata -->
		<div class="px-6 py-4 border-b border-border bg-muted/30">
			<div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
				<!-- Started -->
				<div>
					<p class="text-xs text-muted-foreground mb-1">Started</p>
					<p class="text-sm text-foreground">
						{#if agent.startedAt}
							{formatTime(agent.startedAt)}
							<span class="text-muted-foreground">{formatDate(agent.startedAt)}</span>
						{:else}
							--
						{/if}
					</p>
				</div>

				<!-- Duration -->
				<div>
					<p class="text-xs text-muted-foreground mb-1">Duration</p>
					<div class="flex items-center gap-1 text-sm text-foreground">
						<Clock class="w-3.5 h-3.5 text-muted-foreground" />
						<span>{getElapsedTime()}</span>
					</div>
				</div>

				<!-- Branch -->
				<div>
					<p class="text-xs text-muted-foreground mb-1">Branch</p>
					<div class="flex items-center gap-1 text-sm text-foreground">
						{#if agent.branch}
							<GitBranch class="w-3.5 h-3.5 text-muted-foreground" />
							<span class="truncate">{agent.branch}</span>
						{:else}
							<span class="text-muted-foreground">--</span>
						{/if}
					</div>
				</div>

				<!-- GitHub link -->
				<div>
					<p class="text-xs text-muted-foreground mb-1">GitHub</p>
					{#if agent.prUrl}
						<a
							href={agent.prUrl}
							target="_blank"
							rel="noopener noreferrer"
							class="flex items-center gap-1 text-sm text-primary hover:underline"
						>
							<ExternalLink class="w-3.5 h-3.5" />
							View PR
						</a>
					{:else if agent.branch}
						<span class="text-sm text-muted-foreground">No PR yet</span>
					{:else}
						<span class="text-sm text-muted-foreground">--</span>
					{/if}
				</div>
			</div>

			<!-- Progress bar -->
			{#if agent.status === 'running' && agent.progress !== undefined}
				<div class="mt-4">
					<div class="flex items-center justify-between text-xs text-muted-foreground mb-1">
						<span>Progress</span>
						<span>{agent.progress}%</span>
					</div>
					<div class="h-2 bg-muted rounded-full overflow-hidden">
						<div
							class="h-full bg-blue-500 rounded-full transition-all duration-300"
							style:width="{agent.progress}%"
						></div>
					</div>
				</div>
			{/if}

			<!-- Error message -->
			{#if agent.status === 'failed' && agent.error}
				<div class="mt-4 p-3 bg-red-500/10 border border-red-500/20 rounded-lg">
					<p class="text-sm text-red-500 font-medium">Error</p>
					<p class="text-sm text-red-400 mt-1">{agent.error}</p>
				</div>
			{/if}

			<!-- Result summary -->
			{#if agent.status === 'completed' && agent.resultSummary}
				<div class="mt-4 p-3 bg-green-500/10 border border-green-500/20 rounded-lg">
					<p class="text-sm text-green-500 font-medium">Result</p>
					<p class="text-sm text-green-400 mt-1">{agent.resultSummary}</p>
				</div>
			{/if}
		</div>

		<!-- Control buttons -->
		{#if agent.status === 'running' || agent.status === 'paused' || agent.status === 'queued'}
			<div class="px-6 py-3 border-b border-border flex items-center gap-2">
				{#if agent.status === 'running'}
					<button
						onclick={onPause}
						class="flex items-center gap-2 px-3 py-1.5 bg-yellow-500/10 text-yellow-500 rounded-lg hover:bg-yellow-500/20 transition-colors text-sm font-medium"
					>
						<Pause class="w-4 h-4" />
						Pause
					</button>
				{:else if agent.status === 'paused'}
					<button
						onclick={onResume}
						class="flex items-center gap-2 px-3 py-1.5 bg-green-500/10 text-green-500 rounded-lg hover:bg-green-500/20 transition-colors text-sm font-medium"
					>
						<Play class="w-4 h-4" />
						Resume
					</button>
				{/if}

				<button
					onclick={onCancel}
					class="flex items-center gap-2 px-3 py-1.5 bg-red-500/10 text-red-500 rounded-lg hover:bg-red-500/20 transition-colors text-sm font-medium ml-auto"
				>
					<StopCircle class="w-4 h-4" />
					Cancel
				</button>
			</div>
		{:else}
			<!-- Delete button for completed/failed agents -->
			<div class="px-6 py-3 border-b border-border flex items-center">
				<button
					onclick={onDelete}
					class="flex items-center gap-2 px-3 py-1.5 bg-red-500/10 text-red-500 rounded-lg hover:bg-red-500/20 transition-colors text-sm font-medium ml-auto"
				>
					<Trash2 class="w-4 h-4" />
					Delete
				</button>
			</div>
		{/if}

		<!-- Tabs -->
		<div class="flex items-center gap-1 px-6 py-2 border-b border-border">
			<button
				onclick={() => activeTab = 'tasks'}
				class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors {activeTab === 'tasks' ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
			>
				<ListTree class="w-4 h-4" />
				Tasks
			</button>
			<button
				onclick={() => activeTab = 'logs'}
				class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors {activeTab === 'logs' ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
			>
				<ScrollText class="w-4 h-4" />
				Logs
			</button>
		</div>

		<!-- Content -->
		<div class="flex-1 overflow-hidden">
			{#if activeTab === 'tasks'}
				<TaskTree tasks={agent.tasks} />
			{:else}
				<AgentLogs agentId={agent.id} initialLogs={agent.logs} />
			{/if}
		</div>
	</div>
</div>

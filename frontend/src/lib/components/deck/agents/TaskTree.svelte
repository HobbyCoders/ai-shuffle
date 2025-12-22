<script lang="ts">
	/**
	 * TaskTree - Hierarchical task display
	 *
	 * Features:
	 * - Recursive tree structure
	 * - Status icons: completed, running, pending, failed
	 * - Expandable/collapsible branches
	 * - Progress percentage for parent tasks
	 * - CSS connector lines
	 */
	import { ChevronRight, Check, X, Circle, Loader2 } from 'lucide-svelte';
	import type { AgentTask, TaskStatus } from '$lib/stores/agents';

	interface Props {
		tasks: AgentTask[];
	}

	let { tasks }: Props = $props();

	// Track expanded state - auto-expand tasks with in_progress children
	let expandedIds = $state<Set<string>>(new Set());

	// Auto-expand parent tasks that have in_progress children
	$effect(() => {
		const newExpanded = new Set(expandedIds);

		function checkAndExpand(taskList: AgentTask[], parentId?: string) {
			for (const task of taskList) {
				if (task.status === 'in_progress' && parentId) {
					newExpanded.add(parentId);
				}
				if (task.children && task.children.length > 0) {
					checkAndExpand(task.children, task.id);
				}
			}
		}

		checkAndExpand(tasks);

		// Only update if there are changes
		if (newExpanded.size !== expandedIds.size) {
			expandedIds = newExpanded;
		}
	});

	function toggleExpand(id: string) {
		const newSet = new Set(expandedIds);
		if (newSet.has(id)) {
			newSet.delete(id);
		} else {
			newSet.add(id);
		}
		expandedIds = newSet;
	}

	// Calculate progress for a task with children
	function calculateProgress(task: AgentTask): number | undefined {
		if (!task.children || task.children.length === 0) {
			return undefined;
		}

		const total = task.children.length;
		const completed = task.children.filter(c => c.status === 'completed').length;
		const inProgress = task.children.filter(c => c.status === 'in_progress').length;

		// Count in_progress as 0.5
		return Math.round(((completed + inProgress * 0.5) / total) * 100);
	}

	// Status icon component
	const statusConfig: Record<TaskStatus, { icon: typeof Check; color: string; bgColor: string; animate: boolean }> = {
		completed: { icon: Check, color: 'text-green-500', bgColor: 'bg-green-500/10', animate: false },
		in_progress: { icon: Loader2, color: 'text-blue-500', bgColor: 'bg-blue-500/10', animate: true },
		pending: { icon: Circle, color: 'text-gray-400', bgColor: 'bg-gray-400/10', animate: false },
		failed: { icon: X, color: 'text-red-500', bgColor: 'bg-red-500/10', animate: false }
	};
</script>

{#snippet taskItem(task: AgentTask, depth: number)}
	{@const config = statusConfig[task.status]}
	{@const Icon = config.icon}
	{@const hasChildren = task.children && task.children.length > 0}
	{@const isExpanded = expandedIds.has(task.id)}
	{@const progress = calculateProgress(task)}

	<div class="task-item" style:--depth={depth}>
		<!-- Task row -->
		<div class="flex items-center gap-2 py-1.5 px-2 hover:bg-muted/50 rounded-lg group">
			<!-- Expand/collapse button -->
			{#if hasChildren}
				<button
					onclick={() => toggleExpand(task.id)}
					class="p-0.5 text-muted-foreground hover:text-foreground transition-colors"
				>
					<ChevronRight
						class="w-4 h-4 transition-transform {isExpanded ? 'rotate-90' : ''}"
					/>
				</button>
			{:else}
				<span class="w-5"></span>
			{/if}

			<!-- Status icon -->
			<div class="flex-shrink-0 w-5 h-5 rounded-full {config.bgColor} flex items-center justify-center">
				<Icon class="w-3 h-3 {config.color} {config.animate ? 'animate-spin' : ''}" />
			</div>

			<!-- Task name -->
			<span class="flex-1 text-sm text-foreground truncate {task.status === 'completed' ? 'line-through text-muted-foreground' : ''}">
				{task.name}
			</span>

			<!-- Progress (for parent tasks) -->
			{#if progress !== undefined && task.status === 'in_progress'}
				<span class="text-xs text-muted-foreground">
					{progress}%
				</span>
			{/if}
		</div>

		<!-- Children -->
		{#if hasChildren && isExpanded}
			<div class="task-children ml-4 pl-4 border-l border-border">
				{#each task.children as child (child.id)}
					{@render taskItem(child, depth + 1)}
				{/each}
			</div>
		{/if}
	</div>
{/snippet}

<div class="h-full overflow-y-auto p-4">
	{#if tasks.length === 0}
		<div class="flex flex-col items-center justify-center h-full text-center">
			<Circle class="w-12 h-12 text-muted-foreground/50 mb-3" />
			<p class="text-sm text-muted-foreground">No tasks yet</p>
			<p class="text-xs text-muted-foreground/70 mt-1">Tasks will appear as the agent works</p>
		</div>
	{:else}
		<div class="space-y-1">
			{#each tasks as task (task.id)}
				{@render taskItem(task, 0)}
			{/each}
		</div>
	{/if}
</div>

<style>
	.task-children {
		position: relative;
	}

	/* Vertical connector line */
	.task-children::before {
		content: '';
		position: absolute;
		left: 0;
		top: 0;
		bottom: 0.75rem;
		width: 1px;
		background: hsl(var(--border));
	}

	/* Horizontal connector to each item */
	.task-children > .task-item::before {
		content: '';
		position: absolute;
		left: -1rem;
		top: 0.875rem;
		width: 0.75rem;
		height: 1px;
		background: hsl(var(--border));
	}

	.task-children > .task-item {
		position: relative;
	}
</style>

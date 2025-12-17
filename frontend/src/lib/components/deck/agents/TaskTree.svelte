<script lang="ts">
	/**
	 * TaskTree - Reusable task tree visualization for agent progress
	 *
	 * Features:
	 * - Recursive tree structure with expandable branches
	 * - Status icons: completed, running, pending, failed
	 * - Progress percentage at each level
	 * - Click-to-expand details
	 * - CSS-based tree lines
	 */
	import type { AgentTask, TaskStatus } from '$lib/stores/agents';
	import TaskTree from './TaskTree.svelte';

	interface Props {
		tasks: AgentTask[];
		depth?: number;
		expandedIds?: Set<string>;
		ontaskclick?: (task: AgentTask) => void;
	}

	let {
		tasks,
		depth = 0,
		expandedIds = new Set<string>(),
		ontaskclick
	}: Props = $props();

	// Track which branches are expanded
	let localExpanded = $state(new Set<string>(expandedIds));

	function toggleExpand(taskId: string) {
		if (localExpanded.has(taskId)) {
			localExpanded.delete(taskId);
		} else {
			localExpanded.add(taskId);
		}
		localExpanded = new Set(localExpanded);
	}

	// Get status icon and color
	function getStatusIcon(status: TaskStatus): { symbol: string; class: string; animate: boolean } {
		switch (status) {
			case 'completed':
				return { symbol: 'check', class: 'text-emerald-500', animate: false };
			case 'in_progress':
				return { symbol: 'spinner', class: 'text-cyan-500', animate: true };
			case 'failed':
				return { symbol: 'x', class: 'text-red-500', animate: false };
			case 'pending':
			default:
				return { symbol: 'circle', class: 'text-muted-foreground/50', animate: false };
		}
	}

	// Calculate progress for a task (including children)
	function calculateProgress(task: AgentTask): number {
		if (!task.children || task.children.length === 0) {
			if (task.status === 'completed') return 100;
			if (task.status === 'in_progress') return 50;
			return 0;
		}

		const total = task.children.length;
		const completed = task.children.filter(c => c.status === 'completed').length;
		const inProgress = task.children.filter(c => c.status === 'in_progress').length;

		return Math.round(((completed + inProgress * 0.5) / total) * 100);
	}

	// Format task description
	function formatDescription(task: AgentTask): string {
		if (task.description) return task.description;
		if (task.children && task.children.length > 0) {
			const completed = task.children.filter(c => c.status === 'completed').length;
			return `${completed}/${task.children.length} subtasks`;
		}
		return '';
	}
</script>

<div class="task-tree" class:root={depth === 0}>
	{#each tasks as task, index (task.id)}
		{@const statusIcon = getStatusIcon(task.status)}
		{@const isLast = index === tasks.length - 1}
		{@const hasChildren = task.children && task.children.length > 0}
		{@const isExpanded = localExpanded.has(task.id)}
		{@const progress = calculateProgress(task)}

		<div class="task-item" class:is-last={isLast}>
			<!-- Tree connector line -->
			{#if depth > 0}
				<div class="tree-line" class:is-last={isLast}></div>
			{/if}

			<!-- Main task row -->
			<div class="task-row">
				<!-- Expand/collapse toggle or status icon -->
				{#if hasChildren}
					<button
						type="button"
						class="expand-toggle"
						onclick={() => toggleExpand(task.id)}
						aria-expanded={isExpanded}
						aria-label={isExpanded ? 'Collapse' : 'Expand'}
					>
						<svg
							class="w-4 h-4 transition-transform duration-200 {isExpanded ? 'rotate-90' : ''}"
							fill="none"
							stroke="currentColor"
							viewBox="0 0 24 24"
						>
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
						</svg>
					</button>
				{:else}
					<div class="status-icon {statusIcon.class}" class:animate-spin={statusIcon.animate}>
						{#if statusIcon.symbol === 'check'}
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
							</svg>
						{:else if statusIcon.symbol === 'spinner'}
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
							</svg>
						{:else if statusIcon.symbol === 'x'}
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
							</svg>
						{:else}
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<circle cx="12" cy="12" r="3" stroke-width="2" />
							</svg>
						{/if}
					</div>
				{/if}

				<!-- Task content -->
				<button
					type="button"
					class="task-content"
					onclick={() => ontaskclick?.(task)}
				>
					<span class="task-name" class:completed={task.status === 'completed'}>
						{task.name}
					</span>

					{#if hasChildren && !isExpanded}
						<span class="task-progress">
							{progress}%
						</span>
					{/if}

					{#if formatDescription(task)}
						<span class="task-description">
							{formatDescription(task)}
						</span>
					{/if}
				</button>

				<!-- Parent status indicator -->
				{#if hasChildren}
					<div class="parent-status {statusIcon.class}">
						{#if task.status === 'completed'}
							<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
							</svg>
						{:else if task.status === 'in_progress'}
							<svg class="w-3.5 h-3.5 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
							</svg>
						{:else if task.status === 'failed'}
							<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
							</svg>
						{/if}
					</div>
				{/if}
			</div>

			<!-- Children (if expanded) -->
			{#if hasChildren && isExpanded}
				<div class="task-children">
					<TaskTree
						tasks={task.children || []}
						depth={depth + 1}
						{expandedIds}
						{ontaskclick}
					/>
				</div>
			{/if}
		</div>
	{/each}
</div>

<style>
	.task-tree {
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
		font-size: 0.875rem;
	}

	.task-tree.root {
		padding: 0.5rem 0;
	}

	.task-item {
		position: relative;
	}

	.task-row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.375rem 0;
		min-height: 2rem;
	}

	/* Tree connector lines */
	.tree-line {
		position: absolute;
		left: -1rem;
		top: 0;
		bottom: 0;
		width: 1rem;
		border-left: 1px solid hsl(var(--border) / 0.5);
	}

	.tree-line::before {
		content: '';
		position: absolute;
		left: 0;
		top: 1rem;
		width: 0.75rem;
		border-top: 1px solid hsl(var(--border) / 0.5);
	}

	.tree-line.is-last {
		height: 1rem;
	}

	.task-children {
		margin-left: 1.5rem;
		position: relative;
	}

	/* Expand toggle */
	.expand-toggle {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.25rem;
		height: 1.25rem;
		border-radius: 0.25rem;
		color: hsl(var(--muted-foreground));
		transition: all 0.15s ease;
		flex-shrink: 0;
	}

	.expand-toggle:hover {
		color: hsl(var(--foreground));
		background: hsl(var(--muted));
	}

	/* Status icon */
	.status-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.25rem;
		height: 1.25rem;
		flex-shrink: 0;
	}

	/* Parent status */
	.parent-status {
		display: flex;
		align-items: center;
		justify-content: center;
		margin-left: auto;
		flex-shrink: 0;
	}

	/* Task content */
	.task-content {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex: 1;
		min-width: 0;
		text-align: left;
		padding: 0.25rem 0.5rem;
		border-radius: 0.375rem;
		transition: background-color 0.15s ease;
	}

	.task-content:hover {
		background: hsl(var(--muted) / 0.5);
	}

	.task-name {
		color: hsl(var(--foreground));
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.task-name.completed {
		color: hsl(var(--muted-foreground));
	}

	.task-progress {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		background: hsl(var(--muted));
		padding: 0.125rem 0.375rem;
		border-radius: 9999px;
		flex-shrink: 0;
	}

	.task-description {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
</style>

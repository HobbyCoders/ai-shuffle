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

	type TaskStatus = 'completed' | 'running' | 'pending' | 'failed';

	interface Task {
		id: string;
		name: string;
		status: TaskStatus;
		progress?: number;
		children?: Task[];
	}

	interface Props {
		tasks?: Task[];
	}

	let { tasks }: Props = $props();

	// Default mock data
	const defaultTasks: Task[] = [
		{
			id: '1',
			name: 'Set up authentication system',
			status: 'running',
			progress: 65,
			children: [
				{
					id: '1.1',
					name: 'Create auth middleware',
					status: 'completed',
					children: [
						{ id: '1.1.1', name: 'JWT validation', status: 'completed' },
						{ id: '1.1.2', name: 'Token refresh logic', status: 'completed' },
						{ id: '1.1.3', name: 'Error handling', status: 'completed' }
					]
				},
				{
					id: '1.2',
					name: 'Implement user model',
					status: 'completed',
					children: [
						{ id: '1.2.1', name: 'Password hashing', status: 'completed' },
						{ id: '1.2.2', name: 'Session management', status: 'completed' }
					]
				},
				{
					id: '1.3',
					name: 'Create auth routes',
					status: 'running',
					progress: 50,
					children: [
						{ id: '1.3.1', name: 'Login endpoint', status: 'completed' },
						{ id: '1.3.2', name: 'Register endpoint', status: 'running' },
						{ id: '1.3.3', name: 'Logout endpoint', status: 'pending' },
						{ id: '1.3.4', name: 'Password reset', status: 'pending' }
					]
				},
				{
					id: '1.4',
					name: 'Add tests',
					status: 'pending',
					children: [
						{ id: '1.4.1', name: 'Unit tests', status: 'pending' },
						{ id: '1.4.2', name: 'Integration tests', status: 'pending' }
					]
				}
			]
		},
		{
			id: '2',
			name: 'Update documentation',
			status: 'pending'
		}
	];

	const displayTasks = $derived(tasks ?? defaultTasks);

	// Track expanded state
	let expandedIds = $state<Set<string>>(new Set(['1', '1.3']));

	function toggleExpand(id: string) {
		const newSet = new Set(expandedIds);
		if (newSet.has(id)) {
			newSet.delete(id);
		} else {
			newSet.add(id);
		}
		expandedIds = newSet;
	}

	// Status icon component
	const statusConfig: Record<TaskStatus, { icon: typeof Check; color: string; bgColor: string; animate: boolean }> = {
		completed: { icon: Check, color: 'text-green-500', bgColor: 'bg-green-500/10', animate: false },
		running: { icon: Loader2, color: 'text-blue-500', bgColor: 'bg-blue-500/10', animate: true },
		pending: { icon: Circle, color: 'text-gray-400', bgColor: 'bg-gray-400/10', animate: false },
		failed: { icon: X, color: 'text-red-500', bgColor: 'bg-red-500/10', animate: false }
	};
</script>

{#snippet taskItem(task: Task, depth: number)}
	{@const config = statusConfig[task.status]}
	{@const Icon = config.icon}
	{@const hasChildren = task.children && task.children.length > 0}
	{@const isExpanded = expandedIds.has(task.id)}

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
			{#if task.progress !== undefined && task.status === 'running'}
				<span class="text-xs text-muted-foreground">
					{task.progress}%
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
	{#if displayTasks.length === 0}
		<div class="flex flex-col items-center justify-center h-full text-center">
			<Circle class="w-12 h-12 text-muted-foreground/50 mb-3" />
			<p class="text-sm text-muted-foreground">No tasks yet</p>
			<p class="text-xs text-muted-foreground/70 mt-1">Tasks will appear as the agent works</p>
		</div>
	{:else}
		<div class="space-y-1">
			{#each displayTasks as task (task.id)}
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

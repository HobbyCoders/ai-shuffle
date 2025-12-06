<script lang="ts">
	/**
	 * TodoList - Cursor-style minimal task progress display
	 */
	import type { TodoItem } from '$lib/stores/tabs';

	interface Props {
		todos: TodoItem[];
	}

	let { todos }: Props = $props();

	// Track expanded state - start expanded by default
	let isExpanded = $state(true);

	// Compute statistics
	const stats = $derived(() => {
		const total = todos.length;
		const completed = todos.filter(t => t.status === 'completed').length;
		return { total, completed };
	});

	// Get the currently active task (first in_progress, or first pending)
	const activeTask = $derived(() => {
		const inProgressTask = todos.find(t => t.status === 'in_progress');
		if (inProgressTask) return inProgressTask;
		return todos.find(t => t.status === 'pending');
	});

	// Check if all tasks are completed
	const allCompleted = $derived(() => {
		return todos.length > 0 && todos.every(t => t.status === 'completed');
	});
</script>

{#if todos.length > 0}
	<div class="w-full">
		<!-- Collapsed Header -->
		<button
			onclick={() => isExpanded = !isExpanded}
			class="w-full flex items-center gap-2 py-1.5 text-left hover:bg-muted/30 rounded px-2 -mx-2 transition-colors"
		>
			<!-- Check or Circle -->
			{#if allCompleted()}
				<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
				</svg>
			{:else}
				<!-- Show static circle for in-progress/pending -->
				<div class="w-4 h-4 rounded-full border-2 border-muted-foreground/40 flex-shrink-0"></div>
			{/if}

			<!-- Task text -->
			<span class="flex-1 text-sm text-muted-foreground truncate">
				{#if allCompleted()}
					All tasks completed
				{:else if activeTask()}
					{activeTask()?.status === 'in_progress' ? activeTask()?.activeForm : activeTask()?.content}
				{:else}
					{stats().total} pending tasks
				{/if}
			</span>

			<!-- Progress count -->
			<span class="text-sm text-muted-foreground tabular-nums flex-shrink-0">
				{stats().completed}/{stats().total}
			</span>

			<!-- Chevron -->
			<svg
				class="w-4 h-4 text-muted-foreground transition-transform duration-200 flex-shrink-0 {isExpanded ? 'rotate-180' : ''}"
				fill="none"
				stroke="currentColor"
				viewBox="0 0 24 24"
			>
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
			</svg>
		</button>

		<!-- Expanded Task List -->
		{#if isExpanded}
			<div class="mt-1 space-y-0.5">
				{#each todos as todo, index (index)}
					<div
						class="flex items-center gap-2 py-1 px-2 -mx-2 rounded {todo.status === 'in_progress' ? 'bg-muted/40' : ''}"
					>
						<!-- Checkbox style icon -->
						{#if todo.status === 'completed'}
							<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
							</svg>
						{:else}
							<div class="w-4 h-4 rounded border border-muted-foreground/40 flex-shrink-0"></div>
						{/if}

						<!-- Task text -->
						<span
							class="flex-1 text-sm truncate {todo.status === 'completed' ? 'text-muted-foreground line-through' : 'text-foreground'}"
						>
							{todo.status === 'in_progress' ? todo.activeForm : todo.content}
						</span>
					</div>
				{/each}
			</div>
		{/if}
	</div>
{/if}

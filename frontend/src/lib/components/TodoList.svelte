<script lang="ts">
	/**
	 * TodoList — the plan, as one segmented progress row.
	 *
	 * Collapsed it is a tick bar (one pill per task: done / current / pending),
	 * the current step's label, and an n/total counter — enough to read progress
	 * without opening anything. Expanding gives the full checklist.
	 */
	import type { TodoItem } from '$lib/stores/types';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';

	interface Props {
		todos: TodoItem[];
		/** Override the default expand state */
		initialExpanded?: boolean;
	}

	let { todos, initialExpanded }: Props = $props();

	const prefs = $derived(chatPreferences.current);

	// Once the user has toggled this list, their choice outranks the preference.
	let userToggled = $state(false);
	let userExpandedChoice = $state(false);

	const isExpanded = $derived(
		initialExpanded !== undefined
			? initialExpanded
			: userToggled
				? userExpandedChoice
				: prefs.todosStartExpanded
	);

	function toggleExpanded() {
		userToggled = true;
		userExpandedChoice = !isExpanded;
	}

	const stats = $derived.by(() => ({
		total: todos.length,
		completed: todos.filter((t) => t.status === 'completed').length
	}));

	// The step worth naming: what's running, or what's up next.
	const activeTask = $derived.by(
		() => todos.find((t) => t.status === 'in_progress') ?? todos.find((t) => t.status === 'pending')
	);

	const allCompleted = $derived(todos.length > 0 && todos.every((t) => t.status === 'completed'));
</script>

{#if todos.length > 0}
	<div class="todo">
		<button type="button" class="todo-row" onclick={toggleExpanded} aria-expanded={isExpanded}>
			<span class="wlbar" aria-hidden="true">
				{#each todos as todo, i (i)}
					<i
						class:f={todo.status === 'completed'}
						class:c={todo.status === 'in_progress'}
					></i>
				{/each}
			</span>

			<span class="todo-label">
				{#if allCompleted}
					All tasks completed
				{:else if activeTask}
					{activeTask.status === 'in_progress' ? activeTask.activeForm : activeTask.content}
				{:else}
					{stats.total} pending tasks
				{/if}
			</span>

			<span class="todo-count mono">{stats.completed}/{stats.total}</span>

			<svg
				class="todo-chev"
				class:is-open={isExpanded}
				width="13"
				height="13"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				stroke-linejoin="round"
				aria-hidden="true"
			>
				<path d="m6 9 6 6 6-6" />
			</svg>
		</button>

		{#if isExpanded}
			<ul class="todo-items">
				{#each todos as todo, index (index)}
					<li class="todo-item" class:is-current={todo.status === 'in_progress'}>
						{#if todo.status === 'completed'}
							<svg class="glyph is-done" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
								<path d="M20 6 9 17l-5-5" />
							</svg>
						{:else if todo.status === 'in_progress'}
							<svg class="glyph is-current" width="13" height="13" viewBox="0 0 24 24" aria-hidden="true">
								<circle cx="12" cy="12" r="4.5" fill="currentColor" />
							</svg>
						{:else}
							<svg class="glyph is-pending" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
								<circle cx="12" cy="12" r="5" />
							</svg>
						{/if}
						<span class="todo-text" class:is-done={todo.status === 'completed'}>
							{todo.status === 'in_progress' ? todo.activeForm : todo.content}
						</span>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
{/if}

<style>
	.todo {
		width: 100%;
		min-width: 0;
		margin: 0 -8px;
	}

	.todo-row {
		display: flex;
		align-items: center;
		gap: 9px;
		width: 100%;
		padding: 6px 8px;
		border: none;
		background: transparent;
		border-radius: 8px;
		text-align: left;
		cursor: pointer;
		transition: background-color 0.13s ease;
	}

	.todo-row:hover {
		background: var(--scrim);
	}

	.todo-row:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	@media (max-width: 639px) {
		.todo-row {
			min-height: 44px;
		}
	}

	.wlbar {
		display: inline-flex;
		align-items: center;
		gap: 2.5px;
		flex: none;
		flex-wrap: wrap;
		max-width: 45%;
	}

	.wlbar i {
		width: 7px;
		height: 3px;
		border-radius: 2px;
		background: var(--border-strong);
		transition: background-color 0.2s ease;
	}

	.wlbar i.f {
		background: var(--emerald);
	}

	.wlbar i.c {
		background: var(--blue);
	}

	.todo-label {
		flex: 1;
		min-width: 0;
		font-size: 12.5px;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.todo-count {
		flex: none;
		font-family: var(--font-mono);
		font-size: 11px;
		color: var(--subtle-foreground);
		font-variant-numeric: tabular-nums;
	}

	.todo-chev {
		flex: none;
		color: var(--subtle-foreground);
		transition: transform 0.16s ease;
	}

	.todo-chev.is-open {
		transform: rotate(180deg);
	}

	.todo-items {
		list-style: none;
		margin: 2px 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}

	.todo-item {
		display: flex;
		align-items: center;
		gap: 9px;
		padding: 4px 8px;
		border-radius: 8px;
		min-width: 0;
	}

	.todo-item.is-current {
		background: var(--scrim);
	}

	.glyph {
		flex: none;
	}

	.glyph.is-done {
		color: var(--emerald);
	}

	.glyph.is-current {
		color: var(--blue);
	}

	.glyph.is-pending {
		color: var(--border-strong);
	}

	.todo-text {
		flex: 1;
		min-width: 0;
		font-size: 12.5px;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.todo-text.is-done {
		color: var(--subtle-foreground);
		text-decoration: line-through;
	}
</style>

<script lang="ts">
	/**
	 * ActiveTasks — a collapsible line pinned to the TAIL of the chat history,
	 * summarizing in-flight background work (background sub-agents + shell
	 * subprocesses).
	 *
	 * Collapsed: "N running tasks" with a spinner. Click expands to an indented
	 * "|__" tree of each task — kind badge (agent/shell), subject, elapsed time,
	 * status, and an "x" to stop it. Click again collapses.
	 *
	 * Presentational: `tasks` comes in as a prop (MessageArea feeds it from the
	 * session store's /live-polled `activeTasks`) so it stays unit-testable, and
	 * lives INSIDE the transcript column where it scrolls with history but always
	 * renders last.
	 */
	import { onDestroy } from 'svelte';
	import type { Task } from '$lib/stores/types';

	interface Props {
		tasks: Task[];
		/**
		 * Stop one task by id. Resolves `false` when the stop didn't take (e.g.
		 * the task was already gone) so the row's close button can be re-enabled
		 * instead of sticking disabled forever.
		 */
		onStop?: (taskId: string) => boolean | void | Promise<boolean | void>;
	}

	let { tasks, onStop }: Props = $props();

	// Local expand/collapse state — expanded by default when tasks appear; not
	// persisted (the tail line is transient per-turn chrome, unlike the old strip).
	let expanded = $state(true);

	const runningCount = $derived(tasks.filter((t) => t.status === 'running').length);
	const hasRunning = $derived(runningCount > 0);

	function toggle() {
		expanded = !expanded;
	}

	// Tasks the user has asked to stop — disable + dim their row until the next
	// poll removes them. Keyed by task id.
	let stopping = $state<Record<string, boolean>>({});

	async function stopTask(id: string) {
		if (stopping[id]) return;
		stopping = { ...stopping, [id]: true };
		let ok = false;
		try {
			// onStop resolves `false` when the stop didn't take (task already gone,
			// dead session); `true`/`undefined` means it's in flight.
			ok = (await onStop?.(id)) !== false;
		} catch {
			ok = false;
		}
		if (!ok) {
			// Re-enable so the user can retry — the row is still here because the
			// stop didn't take. On success we leave it disabled + dimmed; the next
			// /live poll drops the row once the registry marks the task terminal.
			const next = { ...stopping };
			delete next[id];
			stopping = next;
		}
	}

	// Live clock so elapsed times tick without a new poll — only while there is
	// at least one RUNNING task AND the tree is visible.
	let now = $state(Date.now());
	let timer: ReturnType<typeof setInterval> | null = null;

	$effect(() => {
		if (hasRunning && expanded) {
			if (timer === null) {
				now = Date.now();
				timer = setInterval(() => {
					now = Date.now();
				}, 1000);
			}
		} else if (timer !== null) {
			clearInterval(timer);
			timer = null;
		}
	});

	onDestroy(() => {
		if (timer !== null) clearInterval(timer);
	});

	/** Format elapsed time since an ISO start timestamp as a compact "1m 20s". */
	function elapsed(startedAt: string, nowMs: number): string {
		const start = Date.parse(startedAt);
		if (Number.isNaN(start)) return '';
		const secs = Math.max(0, Math.floor((nowMs - start) / 1000));
		if (secs < 60) return `${secs}s`;
		const mins = Math.floor(secs / 60);
		const rem = secs % 60;
		if (mins < 60) return rem > 0 ? `${mins}m ${rem}s` : `${mins}m`;
		const hours = Math.floor(mins / 60);
		const remMins = mins % 60;
		return remMins > 0 ? `${hours}h ${remMins}m` : `${hours}h`;
	}

	/** A task is terminal (no longer doing work) once it has failed or finished. */
	function isTerminal(status: Task['status']): boolean {
		return status === 'error' || status === 'killed' || status === 'completed';
	}

	/**
	 * Clock endpoint for the elapsed display. A running task counts up against the
	 * live `now`; a terminal (failed/finished) task FREEZES at its `updatedAt`.
	 */
	function clockEnd(task: Task, nowMs: number): number {
		if (!isTerminal(task.status)) return nowMs;
		const end = Date.parse(task.updatedAt);
		return Number.isNaN(end) ? nowMs : end;
	}

	/** Sort: running first, then terminal failures, then completed. */
	function sortKey(t: Task): number {
		if (t.status === 'running') return 0;
		if (t.status === 'completed') return 2;
		return 1;
	}

	let sortedTasks = $derived(tasks.filter((task) => task.status === 'running'));
</script>

{#if sortedTasks.length > 0}
	<div class="active-tasks" data-expanded={expanded}>
		<button
			type="button"
			class="tasks-toggle"
			onclick={toggle}
			aria-expanded={expanded}
			title={expanded ? 'Collapse task list' : 'Show running tasks'}
			data-testid="tasks-toggle"
		>
			{#if hasRunning}
				<span class="spinner" aria-hidden="true"></span>
			{:else}
				<span class="done-dot" aria-hidden="true"></span>
			{/if}
			<span class="tasks-label">
				{#if hasRunning}
					{runningCount} running {runningCount === 1 ? 'task' : 'tasks'}
				{:else}
					{tasks.length} background {tasks.length === 1 ? 'task' : 'tasks'}
				{/if}
			</span>
			<svg
				class="chevron"
				class:is-open={expanded}
				fill="none"
				stroke="currentColor"
				viewBox="0 0 24 24"
				aria-hidden="true"
			>
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7" />
			</svg>
		</button>
		{#if expanded}
			<ul class="task-tree" role="list">
				{#each sortedTasks as task (task.id)}
					<li
						class="task-row"
						class:is-error={task.status === 'error'}
						class:is-killed={task.status === 'killed'}
						class:is-stopping={stopping[task.id]}
					>
						<span class="tree-branch" aria-hidden="true">|__</span>
						<span
							class="task-kind"
							class:kind-agent={task.kind === 'agent'}
							class:kind-shell={task.kind === 'shell'}
							title={task.kind === 'agent' ? 'Background agent' : 'Shell subprocess'}
						>
							{task.kind === 'agent' ? task.agent || 'agent' : 'shell'}
						</span>
						<span class="task-subject" title={task.summary ?? task.subject}>
							{task.subject || task.summary || 'Task'}
						</span>
						{#if task.startedAt}
							{@const e = elapsed(task.startedAt, clockEnd(task, now))}
							{#if e}<span class="task-elapsed">{e}</span>{/if}
						{/if}
						<span class="task-status" data-status={task.status}>{task.status}</span>
						{#if onStop && !isTerminal(task.status)}
							<button
								type="button"
								class="stop-btn"
								disabled={stopping[task.id]}
								onclick={() => stopTask(task.id)}
								title="Stop task"
								aria-label="Stop task {task.subject || task.summary || ''}"
							>
								<svg
									class="stop-icon"
									fill="none"
									stroke="currentColor"
									viewBox="0 0 24 24"
									aria-hidden="true"
								>
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 6l12 12M18 6L6 18" />
								</svg>
							</button>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</div>
{/if}

<style>
	/* Pinned to the transcript tail: full column width, no card chrome — just a
	   text line that reads as part of the conversation flow. */
	.active-tasks {
		flex-shrink: 0;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.tasks-toggle {
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.125rem 0.25rem;
		margin-left: -0.25rem;
		border: none;
		background: transparent;
		font: inherit;
		color: inherit;
		text-align: left;
		cursor: pointer;
		border-radius: 0.375rem;
		transition: color 0.12s ease, background 0.12s ease;
	}

	.tasks-toggle:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	.tasks-toggle:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.tasks-label {
		font-weight: 500;
		white-space: nowrap;
	}

	/* Spinner: small circular arc rotating while any task is running. */
	.spinner {
		flex-shrink: 0;
		width: 0.625rem;
		height: 0.625rem;
		border-radius: 9999px;
		border: 1.5px solid color-mix(in srgb, var(--primary) 30%, transparent);
		border-top-color: var(--primary);
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.spinner {
			animation-duration: 2.4s;
		}
	}

	/* All-terminal state: static dot instead of spinner. */
	.done-dot {
		flex-shrink: 0;
		width: 0.625rem;
		height: 0.625rem;
		border-radius: 9999px;
		background: var(--muted-foreground);
		opacity: 0.5;
	}

	.chevron {
		flex-shrink: 0;
		width: 0.75rem;
		height: 0.75rem;
		transform: rotate(-90deg);
		transition: transform 0.15s ease;
		opacity: 0.7;
	}

	.chevron.is-open {
		transform: rotate(0deg);
	}

	.task-tree {
		list-style: none;
		margin: 0.25rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		padding-left: 0.25rem;
	}

	.task-row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		min-width: 0;
		color: var(--foreground);
		line-height: 1.4;
	}

	.task-row.is-stopping {
		opacity: 0.5;
	}

	.tree-branch {
		flex-shrink: 0;
		color: var(--muted-foreground);
		opacity: 0.6;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.6875rem;
		user-select: none;
	}

	.task-kind {
		flex-shrink: 0;
		max-width: 8rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		padding: 0 0.375rem;
		border-radius: 9999px;
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: lowercase;
		letter-spacing: 0.02em;
		line-height: 1.5;
	}

	.kind-agent {
		background: color-mix(in srgb, var(--primary) 15%, transparent);
		color: var(--primary);
	}

	.kind-shell {
		background: var(--muted);
		color: var(--muted-foreground);
	}

	.task-subject {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.task-elapsed {
		flex-shrink: 0;
		font-variant-numeric: tabular-nums;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.task-status {
		flex-shrink: 0;
		font-size: 0.625rem;
		text-transform: capitalize;
		color: var(--muted-foreground);
	}

	.task-status[data-status='running'] {
		color: var(--primary);
	}

	.task-row.is-error .task-status,
	.task-row.is-killed .task-status {
		color: var(--destructive);
	}

	.stop-btn {
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 0.125rem;
		border: none;
		background: transparent;
		color: var(--muted-foreground);
		border-radius: 0.25rem;
		cursor: pointer;
		transition: color 0.12s ease, background 0.12s ease;
	}

	.stop-btn:hover:not(:disabled) {
		color: var(--destructive);
		background: color-mix(in srgb, var(--destructive) 12%, transparent);
	}

	.stop-btn:disabled {
		cursor: default;
		opacity: 0.5;
	}

	.stop-icon {
		width: 0.8125rem;
		height: 0.8125rem;
	}
</style>

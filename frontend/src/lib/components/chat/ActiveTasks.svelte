<script lang="ts">
	/**
	 * ActiveTasks — compact strip listing the session's in-flight background
	 * tasks (background sub-agents + shell subprocesses).
	 *
	 * Rendered above the composer during a turn (and especially while the turn is
	 * PARKED waiting on tasks, when isStreaming is false) so the user can see
	 * what's pending. Reads `activeTasks` from the active session — the poller
	 * keeps it fresh on every /live poll. Renders nothing when there are no tasks.
	 *
	 * Per task: a kind badge (agent/shell), the subject, the status, and the
	 * elapsed time since `startedAt` (ticking live).
	 */
	import { onDestroy } from 'svelte';
	import type { Task } from '$lib/stores/types';

	interface Props {
		tasks: Task[];
	}

	let { tasks }: Props = $props();

	// Live clock so the elapsed time ticks without a new poll. Updated once per
	// second; only mounts a timer when there's at least one task to show.
	let now = $state(Date.now());
	let timer: ReturnType<typeof setInterval> | null = null;

	$effect(() => {
		if (tasks.length > 0) {
			if (timer === null) {
				now = Date.now();
				timer = setInterval(() => { now = Date.now(); }, 1000);
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

	function kindLabel(kind: Task['kind']): string {
		return kind === 'agent' ? 'agent' : 'shell';
	}
</script>

{#if tasks.length > 0}
	<div class="active-tasks">
		<div class="active-tasks-inner">
			<div class="active-tasks-header">
				<svg class="w-3 h-3 tasks-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
				</svg>
				<span class="tasks-label">Background ({tasks.length})</span>
			</div>
			<div class="task-rows">
				{#each tasks as task (task.id)}
					<div class="task-row" class:is-error={task.status === 'error'} class:is-killed={task.status === 'killed'}>
						<span class="task-badge" class:badge-agent={task.kind === 'agent'} class:badge-shell={task.kind === 'shell'}>
							{kindLabel(task.kind)}
						</span>
						<span class="task-subject" title={task.summary ?? task.subject}>
							{task.subject || task.summary || 'Task'}
						</span>
						<span class="task-status" data-status={task.status}>{task.status}</span>
						{#if task.startedAt}
							{@const e = elapsed(task.startedAt, now)}
							{#if e}<span class="task-elapsed">{e}</span>{/if}
						{/if}
					</div>
				{/each}
			</div>
		</div>
	</div>
{/if}

<style>
	.active-tasks {
		padding: 0 0.75rem;
		flex-shrink: 0;
	}

	@media (min-width: 640px) {
		.active-tasks {
			padding: 0 1rem;
		}
	}

	.active-tasks-inner {
		max-width: 56rem;
		margin: 0 auto;
		padding: 0.375rem 0.625rem;
		background: color-mix(in oklch, var(--card) 80%, transparent);
		border: 1px solid var(--border-subtle);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
	}

	.active-tasks-header {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.tasks-icon {
		flex-shrink: 0;
		color: var(--primary);
	}

	.tasks-label {
		font-weight: 500;
	}

	.task-rows {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.task-row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.6875rem;
		color: var(--foreground);
		min-width: 0;
	}

	.task-badge {
		flex-shrink: 0;
		padding: 0.0625rem 0.375rem;
		border-radius: 9999px;
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.02em;
		line-height: 1.4;
	}

	.badge-agent {
		background: color-mix(in srgb, var(--primary) 15%, transparent);
		color: var(--primary);
	}

	.badge-shell {
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

	.task-status {
		flex-shrink: 0;
		font-size: 0.625rem;
		text-transform: capitalize;
		color: var(--muted-foreground);
	}

	.task-status[data-status='running'] {
		color: var(--primary);
	}

	.task-row.is-error .task-status {
		color: var(--destructive);
	}

	.task-elapsed {
		flex-shrink: 0;
		font-variant-numeric: tabular-nums;
		font-size: 0.625rem;
		color: var(--muted-foreground);
	}
</style>

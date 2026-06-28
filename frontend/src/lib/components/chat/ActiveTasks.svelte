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
	 * Per task: a kind badge (agent/shell), the subject, the status, the elapsed
	 * time since `startedAt` (ticking live), and an "x" to stop it.
	 *
	 * Clicking anywhere on the header toggles minimized: the whole panel shrinks
	 * to a single "Running N tasks" line. The collapsed state is a chat preference
	 * (localStorage + cross-device server sync, same store the tool-result blocks
	 * use) so it persists across turns, reloads, and devices.
	 */
	import { onDestroy } from 'svelte';
	import type { Task } from '$lib/stores/types';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';

	interface Props {
		tasks: Task[];
		/**
		 * Stop one task by id. The task drops off the next /live poll once stopped.
		 * Resolves `false` when the stop didn't take (e.g. the task was already
		 * gone) so the row's close button can be re-enabled instead of sticking
		 * disabled forever; `true`/`void` means the stop is in flight.
		 */
		onStop?: (taskId: string) => boolean | void | Promise<boolean | void>;
	}

	let { tasks, onStop }: Props = $props();

	// Persisted via the chat-preferences store (instant localStorage + debounced
	// server sync), so the minimized state survives reloads and follows the user
	// across devices.
	const collapsed = $derived(chatPreferences.current.activeTasksCollapsed);

	function toggleCollapsed() {
		chatPreferences.setActiveTasksCollapsed(!collapsed);
	}

	// Tasks the user has asked to stop — disable + dim their row until the next
	// poll removes them. Keyed by task id.
	let stopping = $state<Record<string, boolean>>({});

	async function stopTask(id: string) {
		if (stopping[id]) return;
		stopping = { ...stopping, [id]: true };
		let ok = false;
		try {
			// onStop resolves `false` when the stop didn't take (task already
			// gone, dead session); `true`/`undefined` means it's in flight.
			ok = (await onStop?.(id)) !== false;
		} catch {
			ok = false;
		}
		if (!ok) {
			// Re-enable so the user can retry — the row is still here because the
			// stop didn't take (the next poll won't remove it). On success we
			// leave it disabled + dimmed; the next /live poll drops the row once
			// the registry marks the task terminal.
			const next = { ...stopping };
			delete next[id];
			stopping = next;
		}
	}

	// Live clock so the elapsed time ticks without a new poll. Updated once per
	// second; only mounts a timer when there's at least one task to show.
	let now = $state(Date.now());
	let timer: ReturnType<typeof setInterval> | null = null;

	$effect(() => {
		if (tasks.length > 0 && !collapsed) {
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
			<button
				type="button"
				class="active-tasks-header"
				onclick={toggleCollapsed}
				aria-expanded={!collapsed}
				title={collapsed ? 'Expand background tasks' : 'Minimize background tasks'}
				aria-label={collapsed ? 'Expand background tasks' : 'Minimize background tasks'}
			>
				<svg class="w-3 h-3 tasks-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
				</svg>
				<span class="tasks-label">
					{#if collapsed}
						Running {tasks.length} {tasks.length === 1 ? 'task' : 'tasks'}
					{:else}
						Background ({tasks.length})
					{/if}
				</span>
				<svg class="chevron" class:is-collapsed={collapsed} fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7" />
				</svg>
			</button>
			{#if !collapsed}
				<div class="task-rows">
					{#each tasks as task (task.id)}
						<div
							class="task-row"
							class:is-error={task.status === 'error'}
							class:is-killed={task.status === 'killed'}
							class:is-stopping={stopping[task.id]}
						>
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
							{#if onStop}
								<button
									type="button"
									class="stop-btn"
									disabled={stopping[task.id]}
									onclick={() => stopTask(task.id)}
									title="Stop task"
									aria-label="Stop task {task.subject || task.summary || ''}"
								>
									<svg class="stop-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 6l12 12M18 6L6 18" />
									</svg>
								</button>
							{/if}
						</div>
					{/each}
				</div>
			{/if}
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

	/* The whole header is the collapse toggle — click anywhere on it. */
	.active-tasks-header {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		width: 100%;
		padding: 0.1875rem 0.25rem;
		margin: -0.1875rem -0.25rem;
		border: none;
		background: transparent;
		font: inherit;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		text-align: left;
		border-radius: 0.375rem;
		cursor: pointer;
		transition: color 0.12s ease, background 0.12s ease;
	}

	.active-tasks-header:hover {
		color: var(--foreground);
		background: var(--muted);
	}

	.tasks-icon {
		flex-shrink: 0;
		color: var(--primary);
	}

	.tasks-label {
		font-weight: 500;
	}

	/* Expanded → chevron points up (collapse). Collapsed → points down (expand). */
	.chevron {
		margin-left: auto;
		flex-shrink: 0;
		width: 0.875rem;
		height: 0.875rem;
		transform: rotate(180deg);
		transition: transform 0.15s ease;
	}

	.chevron.is-collapsed {
		transform: rotate(0deg);
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

	.task-row.is-stopping {
		opacity: 0.5;
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

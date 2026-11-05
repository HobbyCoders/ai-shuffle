<script lang="ts">
	/** Read-only rendering of ONE durable plan.updated snapshot, never a planner. */
	interface PlanTask {
		id: string;
		title: string;
		status: 'pending' | 'in_progress' | 'blocked' | 'completed' | 'cancelled';
		depends_on?: string[];
		blocked_reason?: string;
		evidence?: string[];
		references?: string[];
	}
	interface PlanSnapshot {
		version: number;
		generation: number;
		revision: number;
		goal: string;
		done_when: string;
		tasks: PlanTask[];
	}
	let { plan, onBeforeToggle }: {
		plan: unknown;
		/** Release transcript following synchronously, before disclosure changes layout. */
		onBeforeToggle?: (event: MouseEvent) => boolean | void;
	} = $props();
	let expanded = $state(false);
	const instanceId = $props.id();
	const detailsId = `${instanceId}-steps`;
	const statusLabels = {
		pending: 'Pending', in_progress: 'In progress', blocked: 'Blocked',
		completed: 'Completed', cancelled: 'Cancelled'
	};

	// Check the structured wire shape, not model prose or tool-call input. Unknown
	// versions fail visibly rather than silently implying successful completion.
	function snapshot(value: unknown): PlanSnapshot | null {
		if (!value || typeof value !== 'object') return null;
		const p = value as PlanSnapshot;
		const strings = (v: unknown) => v === undefined || (Array.isArray(v) && v.every(s => typeof s === 'string'));
		if (p.version !== 1 || !Number.isSafeInteger(p.generation) || p.generation < 1 ||
			!Number.isSafeInteger(p.revision) || p.revision < 0 ||
			typeof p.goal !== 'string' || typeof p.done_when !== 'string' ||
			!Array.isArray(p.tasks) || p.tasks.length > 50) return null;
		const ids = new Set<string>();
		for (const task of p.tasks) {
			if (!task || typeof task.id !== 'string' || !task.id || ids.has(task.id) ||
				typeof task.title !== 'string' || typeof task.status !== 'string' || !Object.hasOwn(statusLabels, task.status) ||
				!strings(task.depends_on) || !strings(task.evidence) || !strings(task.references) ||
				(task.blocked_reason !== undefined && typeof task.blocked_reason !== 'string')) return null;
			ids.add(task.id);
		}
		return p;
	}

	const view = $derived(snapshot(plan));
	const counts = $derived.by(() => {
		const c = { pending: 0, in_progress: 0, blocked: 0, completed: 0, cancelled: 0 };
		for (const task of view?.tasks ?? []) c[task.status]++;
		return c;
	});
	const current = $derived(view?.tasks.find(t => t.status === 'in_progress') ??
		view?.tasks.find(t => t.status === 'blocked') ?? view?.tasks.find(t => t.status === 'pending'));
	const allCompleted = $derived(!!view?.tasks.length && counts.completed === view.tasks.length);

	function toggle(event: MouseEvent) {
		if (onBeforeToggle?.(event) === false) return;
		expanded = !expanded;
	}
</script>

{#if !view}
	<p class="plan-unavailable" role="status">Plan details unavailable. Reload the chat to try again.</p>
{:else if view.revision > 0}
	<section class="plan" aria-label="Session plan" data-plan-revision={view.revision}>
		<button
			type="button" class="plan-row" aria-expanded={expanded} aria-controls={detailsId}
			aria-label={`${expanded ? 'Collapse' : 'Expand'} plan: ${view.goal}`}
			onclick={() => expanded = !expanded}
		>
			<span class="plan-bars" aria-hidden="true">
				{#each view.tasks as task (task.id)}<i class={task.status}></i>{/each}
			</span>
			<span class="plan-title"><span class="plan-label">Plan</span> {view.goal}</span>
			<span class="plan-count" aria-label={`${counts.completed} of ${view.tasks.length} steps reported completed`}>{counts.completed}/{view.tasks.length}</span>
			<svg class="plan-chevron" class:open={expanded} width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
		</button>
		<div class="plan-summary">
			{#if current}<span class="current">{current.title}</span>
			{:else if allCompleted}<span>All steps completed</span>{/if}
			{#if counts.blocked}<span class="blocked-count">{counts.blocked} blocked</span>{/if}
			{#if counts.cancelled}<span>{counts.cancelled} cancelled</span>{/if}
		</div>
		<p class="plan-source">Model-reported · not independently verified</p>
		{#if expanded}
			<div id={detailsId} class="plan-details">
				{#if view.done_when}<p class="acceptance"><span>Done when</span> {view.done_when}</p>{/if}
				{#if !view.tasks.length}<p class="empty">No steps reported yet.</p>{/if}
				<ul class="plan-tasks">
					{#each view.tasks as task (task.id)}
						<li class="plan-task" class:is-current={task.status === 'in_progress'} data-plan-task={task.id}>
							<span class={`task-mark ${task.status}`} aria-hidden="true">
								{#if task.status === 'completed'}✓{:else if task.status === 'cancelled'}×{:else if task.status === 'blocked'}!{:else if task.status === 'in_progress'}●{:else}○{/if}
							</span>
							<div class="task-body">
								<span class="sr-only">{statusLabels[task.status]}</span>
								<span class="task-title" class:is-terminal={task.status === 'completed' || task.status === 'cancelled'}>{task.title}</span>
								{#if task.blocked_reason}<p class="blocker">{task.blocked_reason}</p>{/if}
								{#if task.depends_on?.length}<p class="dependencies">After: {task.depends_on.map(id => view.tasks.find(t => t.id === id)?.title ?? id).join(', ')}</p>{/if}
								{#if task.evidence?.length}
									<div class="evidence"><span class="detail-label">Reported evidence</span>{#each task.evidence as item}<p>{item}</p>{/each}</div>
								{/if}
								{#if task.references?.length}<div class="references"><span class="detail-label">References</span>{#each task.references as ref}<code>{ref}</code>{/each}</div>{/if}
							</div>
						</li>
					{/each}
				</ul>
				<p class="snapshot">Snapshot · plan {view.generation} · revision {view.revision}</p>
			</div>
		{/if}
	</section>
{/if}

<style>
	.plan { width: 100%; min-width: 0; font-size: 12.5px; color: var(--foreground); }
	.plan-row { display: flex; align-items: center; gap: 9px; width: 100%; min-height: 36px; padding: 6px 8px; border: 0; border-radius: 8px; background: transparent; text-align: left; cursor: pointer; transition: background-color 0.2s ease-out; }
	.plan-row:hover { background: var(--scrim); }
	.plan-row:active { background: var(--muted); }
	.plan-row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.plan-row:disabled { opacity: 0.5; pointer-events: none; }
	.plan-bars { display: flex; flex-wrap: wrap; gap: 2.5px; max-width: 20%; flex: none; }
	.plan-bars i { width: 7px; height: 3px; border-radius: 2px; background: var(--border-strong); }
	.plan-bars .completed { background: var(--emerald); }
	.plan-bars .in_progress { background: var(--blue); }
	.plan-bars .blocked { background: var(--warning); }
	.plan-bars .cancelled { background: var(--subtle-foreground); }
	.plan-title { flex: 1; min-width: 0; overflow-wrap: anywhere; line-height: 1.5; }
	.plan-label { color: var(--muted-foreground); margin-right: 5px; }
	.plan-count { flex: none; color: var(--muted-foreground); font-variant-numeric: tabular-nums; font-size: 11px; }
	.plan-chevron { flex: none; color: var(--muted-foreground); transition: transform 0.2s ease-out; }
	.plan-chevron.open { transform: rotate(180deg); }
	.plan-summary { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; margin: 0 8px; color: var(--muted-foreground); line-height: 1.5; }
	.current { overflow-wrap: anywhere; }
	.blocked-count, .blocker { color: var(--warning); }
	.plan-source, .snapshot { margin: 4px 8px 0; font-size: 11px; line-height: 1.5; color: var(--muted-foreground); }
	.plan-details { margin-top: 10px; }
	.acceptance { margin: 0 8px 8px; line-height: 1.6; overflow-wrap: anywhere; }
	.acceptance > span, .detail-label { color: var(--muted-foreground); }
	.acceptance > span { margin-right: 6px; }
	.plan-tasks { list-style: none; margin: 0; padding: 0; }
	.plan-task { display: flex; align-items: flex-start; gap: 9px; padding: 6px 8px; border-radius: 8px; min-width: 0; }
	.plan-task.is-current { background: var(--scrim); }
	.task-mark { flex: 0 0 13px; text-align: center; line-height: 1.6; color: var(--muted-foreground); }
	.task-mark.completed { color: var(--emerald); }
	.task-mark.in_progress { color: var(--blue); }
	.task-mark.blocked { color: var(--warning); }
	.task-body { min-width: 0; line-height: 1.6; overflow-wrap: anywhere; }
	.task-title.is-terminal { color: var(--muted-foreground); }
	.blocker, .dependencies, .evidence p { margin: 3px 0 0; }
	.dependencies, .evidence, .references { font-size: 11.5px; color: var(--muted-foreground); }
	.evidence, .references { margin-top: 6px; }
	.references { display: flex; flex-wrap: wrap; gap: 3px 8px; }
	.references code { font-family: var(--font-mono); overflow-wrap: anywhere; }
	.empty, .plan-unavailable { padding: 0 8px; color: var(--muted-foreground); font-size: 12px; }
	@media (max-width: 639px), (pointer: coarse) { .plan-row { min-height: 44px; } }
	@media (prefers-reduced-motion: reduce) { .plan-row, .plan-chevron { transition: none; } }
</style>

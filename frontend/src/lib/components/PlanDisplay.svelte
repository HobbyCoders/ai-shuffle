<script lang="ts">
	/** Read-only rendering of ONE durable plan.updated snapshot, never a planner. */
	import ToolIcon from './chat/ToolIcon.svelte';

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
		<!-- Same row as a tool call (ToolUseMessage): icon, name, one-line preview,
		     state on the right. Everything else lives behind the disclosure. -->
		<button
			type="button" class="plan-row" class:is-open={expanded} aria-expanded={expanded} aria-controls={detailsId}
			aria-label={`${expanded ? 'Collapse' : 'Expand'} plan: ${view.goal}. ${counts.completed} of ${view.tasks.length} steps reported completed${counts.blocked ? `, ${counts.blocked} blocked` : ''}`}
			onclick={toggle}
		>
			<ToolIcon name="TodoWrite" class="plan-icon" />
			<span class="plan-name">Plan</span>
			<span class="plan-goal" title={view.goal}>{view.goal}</span>
			<span class="plan-right">
				{#if counts.blocked}<span class="plan-blocked">{counts.blocked} blocked</span>{/if}
				<span class="plan-count">{counts.completed}/{view.tasks.length}</span>
				<svg class="plan-chevron" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
				{#if allCompleted}
					<svg class="plan-glyph is-done" data-testid="plan-status-done" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
				{:else}
					<!-- Progress ring: completed share of all steps (cancelled never counts). -->
					<svg class="plan-glyph" class:is-blocked={counts.blocked > 0} data-testid="plan-status-progress" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke-width="2.6" aria-hidden="true">
						<circle class="ring-track" cx="12" cy="12" r="9" />
						<circle class="ring-fill" cx="12" cy="12" r="9" pathLength="100" stroke-dasharray={`${view.tasks.length ? (counts.completed / view.tasks.length) * 100 : 0} 100`} transform="rotate(-90 12 12)" />
					</svg>
				{/if}
			</span>
		</button>
		{#if expanded}
			<div id={detailsId} class="plan-details">
				<!-- The row preview ellipsizes; the full goal wraps here (no hover on touch). -->
				{#if view.goal}<div class="plan-section"><div class="plan-section-label">Goal</div><p class="acceptance" data-testid="plan-goal-full">{view.goal}</p></div>{/if}
				{#if view.done_when}<div class="plan-section"><div class="plan-section-label">Done when</div><p class="acceptance">{view.done_when}</p></div>{/if}
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
				<p class="plan-foot">
					{#if allCompleted}<span>All steps completed</span>{/if}
					{#if counts.cancelled}<span>{counts.cancelled} cancelled</span>{/if}
					<span>Model-reported · not independently verified</span>
					<span>Snapshot · plan {view.generation} · revision {view.revision}</span>
				</p>
			</div>
		{/if}
	</section>
{/if}

<style>
	/* Row and body mirror ToolUseMessage's .wlrow / .wl-body so a plan reads as one more tool row. */
	.plan { width: 100%; min-width: 0; font-size: 12.5px; color: var(--foreground); }
	.plan-row { display: flex; align-items: center; gap: 9px; width: 100%; min-width: 0; padding: 6px 8px; border: 0; border-radius: 8px; background: transparent; text-align: left; cursor: pointer; transition: background-color 180ms ease-out; }
	.plan-row:hover { background: var(--scrim); }
	.plan-row:active { background: var(--active-overlay, var(--scrim)); }
	.plan-row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.plan-row:disabled { opacity: 0.5; pointer-events: none; }
	.plan-row :global(.plan-icon) { color: var(--subtle-foreground); flex: none; width: 14px; height: 14px; }
	.plan-name { flex: none; font-size: 12.5px; font-weight: 600; color: var(--foreground); white-space: nowrap; }
	.plan-goal { min-width: 0; font-family: var(--font-mono); font-size: 11.5px; color: var(--muted-foreground); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.plan-right { margin-left: auto; display: inline-flex; align-items: center; gap: 8px; flex: none; color: var(--subtle-foreground); }
	.plan-count, .plan-blocked { font-family: var(--font-mono); font-size: 9.5px; font-variant-numeric: tabular-nums; }
	.plan-count { color: var(--subtle-foreground); }
	.plan-blocked, .blocker { color: var(--warning); }
	.plan-chevron { transition: transform 180ms ease-out; }
	.plan-row.is-open .plan-chevron { transform: rotate(180deg); }
	.plan-glyph.is-done { color: var(--emerald); }
	.ring-track { stroke: var(--border-strong); }
	.ring-fill { stroke: var(--emerald); stroke-linecap: round; }
	.plan-glyph.is-blocked .ring-track { stroke: var(--warning); opacity: 0.45; }
	.plan-details { margin: 2px 0 6px 14px; padding-left: 12px; border-left: 1px solid var(--border); display: flex; flex-direction: column; gap: 8px; }
	.plan-section-label { font-size: 10px; font-weight: 600; letter-spacing: 0.075em; text-transform: uppercase; color: var(--subtle-foreground); margin-bottom: 4px; }
	.acceptance { margin: 0; line-height: 1.6; overflow-wrap: anywhere; color: var(--muted-foreground); }
	.detail-label { color: var(--muted-foreground); }
	.plan-foot { display: flex; flex-wrap: wrap; gap: 2px 12px; margin: 0; font-size: 11px; line-height: 1.5; color: var(--subtle-foreground); }
	.plan-tasks { list-style: none; margin: 0 0 0 -6px; padding: 0; }
	.plan-task { display: flex; align-items: flex-start; gap: 9px; padding: 4px 6px; border-radius: 7px; min-width: 0; }
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
	.empty { margin: 0; color: var(--muted-foreground); font-size: 12px; }
	.plan-unavailable { padding: 0 8px; color: var(--muted-foreground); font-size: 12px; }
	@media (max-width: 639px), (pointer: coarse) { .plan-row { min-height: 44px; } }
	@media (prefers-reduced-motion: reduce) { .plan-row, .plan-chevron { transition: none; } }
</style>

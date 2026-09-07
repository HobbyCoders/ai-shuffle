<script lang="ts">
 import { swarms } from '$lib/stores/swarms';
 import { isSwarmActive, parseSwarm, resolveSwarm, swarmStages } from '$lib/utils/swarm';
 let { sessionId, input, result, status }: {
  sessionId?: string | null; input?: Record<string, unknown>; result?: string;
  status: 'running' | 'complete' | 'error';
 } = $props();
 let expanded = $state(true);
 let confirming = $state(false);
 let cancelling = $state(false);
 let cancelError = $state('');
 const initial = $derived(parseSwarm(result));
 const owner = $derived(sessionId ?? initial?.session_id);
 const snapshot = $derived(resolveSwarm(Object.values($swarms.bySession[owner ?? ''] ?? {}), initial, input));
 const stages = $derived(swarmStages(snapshot?.tasks ?? []));
 const completed = $derived(snapshot?.tasks.filter(t => t.status === 'completed').length ?? 0);
 const running = $derived(snapshot?.tasks.filter(t => t.status === 'running').length ?? 0);
 const refreshError = $derived($swarms.errors[owner ?? '']);
 const title = $derived(snapshot?.title || (typeof input?.title === 'string' ? input.title : 'Agent workflow'));
 const workflowStatus = $derived(snapshot?.status ?? (status === 'error' ? 'failed' : status === 'running' ? 'starting' : 'unavailable'));
 $effect(() => { if (initial) swarms.receive(initial, owner ?? undefined); });
 $effect(() => { if (owner) void swarms.hydrate(owner); });
 async function cancel() {
  if (!snapshot || cancelling) return;
  cancelling = true; cancelError = '';
  try { await swarms.cancel(snapshot.session_id, snapshot.id); confirming = false; }
  catch (error) { cancelError = error instanceof Error ? error.message : 'Unable to cancel workflow. Try again.'; }
  finally { cancelling = false; }
 }
</script>

<section class="swarm" aria-label={`Swarm: ${title}`} data-testid="swarm-card">
 <button class="heading" onclick={() => expanded = !expanded} aria-label={`Swarm ${title}`} aria-expanded={expanded}>
  <svg class="mark" width="24" height="28" viewBox="0 0 24 28" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M12 6v5M5 17v-6h14v6M5 21v4h14v-4"/><circle cx="12" cy="4" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/></svg>
  <span class="heading-copy"><span class="eyebrow">Swarm</span><strong>{title}</strong></span>
  <span class="state" data-state={workflowStatus}>{workflowStatus}</span>
  <span class="chevron" aria-hidden="true">{expanded ? '−' : '+'}</span>
 </button>
 {#if snapshot}
  <div class="progress-line" role="status" aria-live="polite">
   <span>{completed} of {snapshot.tasks.length} tasks completed{running ? ` · ${running} running` : ''}</span>
   <progress value={completed} max={Math.max(snapshot.tasks.length, 1)} aria-label="Completed tasks"></progress>
  </div>
 {/if}
 {#if expanded}
  {#if snapshot}
   <div class="stages">
    {#each stages as stage, index}
     <div class="stage">
      <div class="stage-label">{stage.unresolved ? 'Unresolved dependencies' : `Stage ${index + 1}`}<span>{stage.tasks.length > 1 ? 'Can run in parallel' : ''}</span></div>
      <div class="tasks">
       {#each stage.tasks as task (task.id)}
        <details class="task" data-state={task.status}>
         <summary>
          <span class="task-dot" aria-hidden="true"></span>
          <span class="task-copy"><strong>{task.title || task.id}</strong><span class="agent">{task.agent} <span class="task-id">/ {task.id}</span></span></span>
          <span class="state" data-state={task.status}>{task.status}</span>
         </summary>
         <div class="task-body">
          {#if task.depends_on.length}
           <p class="dependencies">After: {task.depends_on.map(id => snapshot?.tasks.find(t => t.id === id)?.title || id).join(' · ')}</p>
          {:else}<p class="dependencies">No dependencies — ready when the workflow starts.</p>{/if}
          {#if task.status === 'running'}<p>Working on {task.title || task.id}.</p>
          {:else if task.status === 'blocked'}<p>Dependencies must complete before this task can start. Check the earlier tasks for their status.</p>
          {:else if task.status === 'queued'}<p>Waiting for dependencies or an available agent.</p>
          {:else if task.status === 'cancelled'}<p>This task was cancelled.</p>
          {:else if task.status === 'interrupted'}<p>This task stopped before it finished.</p>{/if}
          {#if task.error}<div class="error" role="status"><span class="detail-label">Error</span><pre>{task.error}</pre></div>{/if}
          {#if task.result}<div class="result"><span class="detail-label">Result</span><pre>{task.result}</pre></div>
          {:else if task.status === 'completed'}<p>Completed without a text result.</p>{/if}
          {#if task.worker_session_id}
           <a class="text-button" href={`/?session=${encodeURIComponent(task.worker_session_id)}`} data-sveltekit-reload>Open {task.agent} session <span aria-hidden="true">↗</span></a>
          {/if}
         </div>
        </details>
       {/each}
      </div>
     </div>
    {/each}
   </div>
   {#if snapshot.tasks.length === 0}<p class="notice">No tasks were recorded for this workflow.</p>{/if}
   <footer>
    <span class="workflow-note" aria-live="polite">{snapshot.status === 'completed' ? 'All work finished.' : snapshot.status === 'failed' ? 'Workflow failed. Expand a task to inspect its error.' : snapshot.status === 'cancelled' ? 'Workflow cancelled. Finished results are kept.' : snapshot.status === 'interrupted' ? 'Workflow interrupted. Finished results are kept.' : 'Updates arrive as agents work.'}</span>
    {#if isSwarmActive(snapshot)}
     {#if confirming}
      <div class="cancel-confirm"><span>Cancel unfinished tasks?</span><button class="text-button danger" disabled={cancelling} onclick={cancel}>{cancelling ? 'Cancelling…' : 'Cancel workflow'}</button><button class="text-button" disabled={cancelling} onclick={() => confirming = false}>Keep running</button></div>
     {:else}<button class="text-button" onclick={() => confirming = true}>Cancel workflow</button>{/if}
    {/if}
   </footer>
  {:else}
   <p class="notice">{status === 'error' ? 'The workflow could not start. Review the tool error below.' : status === 'running' ? 'Starting workflow. Agent tasks will appear here as they are scheduled.' : 'Workflow details are not available yet. Refresh to retrieve its current state.'}</p>
   {#if result}<pre class="raw-result">{result}</pre>{/if}
  {/if}
  {#if snapshot && status === 'error' && result}<div class="notice error" role="alert"><span class="detail-label">Tool request failed</span><pre>{result}</pre></div>{/if}
  {#if cancelError}<p class="notice error" role="alert">{cancelError}</p>{/if}
  {#if refreshError || (!snapshot && status !== 'running')}
   <div class="refresh notice"><span role="status">{refreshError ? `Live status could not be refreshed. ${refreshError}` : 'No workflow snapshot received.'}</span>{#if owner}<button class="text-button" onclick={() => swarms.hydrate(owner)}>Refresh workflow</button>{/if}</div>
  {/if}
 {/if}
</section>

<style>
 .swarm { min-width: 0; width: 100%; margin: 8px 0; border: 1px solid var(--border); border-radius: 12px; background: var(--card); color: var(--foreground); overflow: hidden; font-family: var(--font-sans); }
 button { font: inherit; cursor: pointer; }
 button:focus-visible, a:focus-visible, summary:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; border-radius: 5px; }
 button:disabled { opacity: .6; cursor: wait; }
 .heading { display: flex; align-items: center; gap: 12px; width: 100%; padding: 16px; border: 0; background: transparent; color: inherit; text-align: left; }
 .heading:hover, summary:hover { background: var(--scrim); }
 .mark { color: var(--primary); flex: none; }
 .heading-copy { display: grid; gap: 4px; min-width: 0; flex: 1; }
 .heading-copy strong { font-size: 14px; font-weight: 600; line-height: 1.4; overflow-wrap: anywhere; }
 .eyebrow, .stage-label, .detail-label { font: 10px var(--font-mono); color: var(--muted-foreground); }
 .eyebrow { letter-spacing: .08em; text-transform: uppercase; }
 .state { color: var(--muted-foreground); font: 10px var(--font-mono); flex: none; }
 .state[data-state='running'], .state[data-state='starting'] { color: var(--primary); }
 .state[data-state='completed'] { color: var(--emerald); }
 .state[data-state='failed'], .state[data-state='blocked'], .error, .danger { color: var(--rose); }
 .chevron { color: var(--muted-foreground); width: 12px; font-size: 18px; }
 .progress-line { padding: 0 16px 14px 52px; display: grid; gap: 8px; color: var(--muted-foreground); font-size: 11px; }
 progress { display: block; appearance: none; border: none; height: 3px; width: 100%; border-radius: 4px; background: var(--muted); overflow: hidden; }
 progress::-webkit-progress-bar { background: var(--muted); } progress::-webkit-progress-value { background: var(--primary); } progress::-moz-progress-bar { background: var(--primary); }
 .stages { padding: 0 16px 8px; }
 .stage { position: relative; padding: 0 0 16px 16px; border-left: 1px solid var(--border); margin-left: 4px; }
 .stage:last-child { padding-bottom: 0; }
 .stage-label { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding: 4px 0 8px; }
 .stage-label::before { content: ''; position: absolute; left: -3px; width: 5px; height: 5px; border-radius: 50%; background: var(--primary); }
 .stage-label span { font-family: var(--font-sans); color: var(--subtle-foreground); }
 .tasks { display: grid; gap: 6px; }
 .task { border: 1px solid var(--border); border-radius: 8px; min-width: 0; overflow: hidden; }
 summary { display: flex; align-items: center; gap: 9px; padding: 11px 12px; cursor: pointer; list-style: none; }
 summary::-webkit-details-marker { display: none; }
 summary::after { content: '+'; color: var(--subtle-foreground); font: 12px var(--font-mono); }
 details[open] > summary::after { content: '−'; }
 .task-dot { height: 6px; width: 6px; border: 1px solid var(--muted-foreground); border-radius: 50%; flex: none; }
 .task[data-state='running'] .task-dot { border-color: var(--primary); background: var(--primary); }
 .task[data-state='completed'] .task-dot { border-color: var(--emerald); background: var(--emerald); }
 .task[data-state='failed'] .task-dot, .task[data-state='blocked'] .task-dot { border-color: var(--rose); }
 .task-copy { display: grid; gap: 4px; min-width: 0; flex: 1; overflow-wrap: anywhere; }
 .task-copy strong { font-size: 12px; font-weight: 550; line-height: 1.45; }
 .agent { font-size: 11px; color: var(--muted-foreground); } .task-id { font: 10px var(--font-mono); color: var(--subtle-foreground); }
 .task-body { border-top: 1px solid var(--border); padding: 12px; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
 p { margin: 0 0 10px; } .dependencies { color: var(--muted-foreground); font-size: 11px; }
 pre { white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; font: 11px/1.7 var(--font-mono); max-height: 24rem; overflow: auto; margin: 6px 0 12px; }
 .result, .error { min-width: 0; } .detail-label { display: block; color: inherit; }
 footer { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; padding: 12px 16px; }
 .workflow-note { font-size: 11px; color: var(--muted-foreground); }
 .text-button { background: transparent; color: var(--primary); border: 0; padding: 5px 0; font-size: 11px; text-align: left; }
 .text-button:hover { text-decoration: underline; } .text-button.danger { color: var(--rose); }
 .cancel-confirm { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; font-size: 11px; }
 .notice { padding: 10px 16px; font-size: 12px; line-height: 1.6; margin: 0; }
 .refresh { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; border-top: 1px solid var(--border); color: var(--muted-foreground); }
 .raw-result { margin: 0 16px 16px; }
 @media (max-width: 480px) { .heading { padding: 12px; gap: 8px; flex-wrap: wrap; } .heading-copy { flex-basis: 55%; } .progress-line { padding-left: 12px; padding-right: 12px; } .stages { padding-left: 8px; padding-right: 8px; } .stage { padding-left: 10px; } summary { padding: 10px 8px; gap: 7px; } .text-button { min-height: 44px; } }
</style>

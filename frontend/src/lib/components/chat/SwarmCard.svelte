<script lang="ts">
 import { ChevronDown, LoaderCircle, Check, X, CirclePause } from 'lucide-svelte';
 import { swarms } from '$lib/stores/swarms';
 import { isSwarmActive, parseSwarm, resolveSwarm, swarmStages } from '$lib/utils/swarm';
 import { openSessionLink } from '$lib/services/session-navigation';
 let { sessionId, input, result, status, expanded = $bindable(false) }: {
  sessionId?: string | null; input?: Record<string, unknown>; result?: string;
  status: 'running' | 'complete' | 'error'; expanded?: boolean;
 } = $props();
 let confirming = $state(false);
 let cancelling = $state(false);
 let cancelError = $state('');
 const initial = $derived(parseSwarm(result));
 const owner = $derived(sessionId ?? initial?.session_id);
 const snapshot = $derived(resolveSwarm(Object.values($swarms.bySession[owner ?? ''] ?? {}), initial, input));
 const stages = $derived(swarmStages(snapshot?.tasks ?? []));
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
  <svg class="mark" width="14" height="14" viewBox="0 0 24 28" fill="none" stroke="currentColor" stroke-width="1.85" aria-hidden="true"><path d="M12 6v5M5 17v-6h14v6M5 21v4h14v-4"/><circle cx="12" cy="4" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/></svg>
  <span class="tool-name">Swarm</span>
  <span class="heading-title" title={title}>{title}</span>
  <span class="heading-right">
   <span class="state" data-state={workflowStatus} role="status">{workflowStatus}</span>
   <ChevronDown size={13} class={expanded ? 'chevron expanded' : 'chevron'} aria-hidden="true" />
   {#if workflowStatus === 'running' || workflowStatus === 'starting'}
    <LoaderCircle size={13} class="running-icon" aria-hidden="true" />
   {:else if workflowStatus === 'completed'}
    <Check size={13} class="complete-icon" aria-hidden="true" />
   {:else if workflowStatus === 'failed'}
    <X size={13} class="error-icon" aria-hidden="true" />
   {:else}<CirclePause size={13} aria-hidden="true" />{/if}
  </span>
 </button>
 {#if expanded}
  <div class="body">
  {#if snapshot}
   <div class="stages">
    {#each stages as stage, index}
     <div class="stage">
      {#if stages.length > 1 || stage.unresolved}
       <div class="stage-label">{stage.unresolved ? 'Unresolved dependencies' : `Stage ${index + 1}`}</div>
      {/if}
      <div class="tasks">
       {#each stage.tasks as task (task.id)}
        <details class="task" data-state={task.status}>
         <summary>
          <span class="task-dot" aria-hidden="true"></span>
          <strong class="task-title" title={task.title || task.id}>{task.title || task.id}</strong>
          <span class="agent" title={task.agent}>{task.agent}</span>
          <span class="state" data-state={task.status}>{task.status}</span>
          <ChevronDown size={13} class="task-chevron" aria-hidden="true" />
         </summary>
         <div class="task-body">
          {#if task.depends_on.length}<p class="dependencies">After: {task.depends_on.map(id => snapshot?.tasks.find(t => t.id === id)?.title || id).join(' · ')}</p>{/if}
          {#if task.status === 'blocked'}<p>Dependencies must complete before this task can start.</p>
          {:else if task.status === 'queued'}<p>Waiting for dependencies or an available agent.</p>
          {:else if task.status === 'cancelled'}<p>This task was cancelled.</p>
          {:else if task.status === 'interrupted'}<p>This task stopped before it finished.</p>{/if}
          {#if task.error}<p class="error" role="status">{task.error}</p>{/if}
          {#if task.worker_session_id}
           <a class="transcript-link" href={`/?session=${encodeURIComponent(task.worker_session_id)}`} onclick={(event) => openSessionLink(event, task.worker_session_id!)} data-sveltekit-reload>Open transcript <span aria-hidden="true">↗</span></a>
          {:else}<p class="muted">{task.status === 'queued' || task.status === 'running' ? 'The transcript is not available yet.' : 'No separate transcript is available for this agent.'}</p>{/if}
         </div>
        </details>
       {/each}
      </div>
     </div>
    {/each}
   </div>
   {#if snapshot.tasks.length === 0}<p class="notice">No tasks were recorded for this workflow.</p>{/if}
   {#if isSwarmActive(snapshot)}
    <footer>
     {#if confirming}
      <div class="cancel-confirm"><span>Cancel unfinished tasks?</span><button class="text-button danger" disabled={cancelling} onclick={cancel}>{cancelling ? 'Cancelling…' : 'Cancel workflow'}</button><button class="text-button" disabled={cancelling} onclick={() => confirming = false}>Keep running</button></div>
     {:else}<button class="text-button" onclick={() => confirming = true}>Cancel workflow</button>{/if}
    </footer>
   {/if}
  {:else}
   <p class="notice">{status === 'error' ? 'The workflow could not start. Review the tool error below.' : status === 'running' ? 'Starting workflow. Agent tasks will appear here as they are scheduled.' : 'Workflow details are not available yet. Refresh to retrieve its current state.'}</p>
   {#if result}<pre class="raw-result">{result}</pre>{/if}
  {/if}
  {#if snapshot && status === 'error' && result}<div class="notice error" role="alert">Tool request failed<pre>{result}</pre></div>{/if}
  {#if cancelError}<p class="notice error" role="alert">{cancelError}</p>{/if}
  {#if refreshError || (!snapshot && status !== 'running')}
   <div class="refresh notice"><span role="status">{refreshError ? `Live status could not be refreshed. ${refreshError}` : 'No workflow snapshot received.'}</span>{#if owner}<button class="text-button" onclick={() => swarms.hydrate(owner)}>Refresh workflow</button>{/if}</div>
  {/if}
  </div>
 {/if}
</section>

<style>
 .swarm { min-width: 0; width: 100%; color: var(--foreground); }
 button { font: inherit; cursor: pointer; }
 button:focus-visible, a:focus-visible, summary:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; border-radius: 6px; }
 button:disabled { opacity: .6; cursor: wait; }
 .heading { display: flex; align-items: center; gap: 9px; width: 100%; min-width: 0; padding: 6px 8px; border: 0; border-radius: 8px; background: transparent; color: inherit; text-align: left; }
 .heading:hover, .heading[aria-expanded='true'], summary:hover { background: var(--scrim); }
 .mark { color: var(--subtle-foreground); flex: none; }
 .tool-name { font-size: 12.5px; font-weight: 600; }
 .heading-title { min-width: 0; font-size: 12px; color: var(--muted-foreground); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
 .heading-right { display: flex; align-items: center; gap: 8px; margin-left: auto; flex: none; color: var(--subtle-foreground); }
 .state { color: var(--muted-foreground); font-size: 11px; flex: none; text-transform: capitalize; }
 .state[data-state='failed'], .state[data-state='blocked'], .error, .danger { color: var(--rose); }
 .swarm :global(.chevron) { transition: transform .16s ease; }
 .swarm :global(.chevron.expanded), .task[open] > summary :global(.task-chevron) { transform: rotate(180deg); }
 .swarm :global(.running-icon) { color: var(--primary); animation: spin .9s linear infinite; }
 .swarm :global(.complete-icon) { color: var(--emerald); }
 .swarm :global(.error-icon) { color: var(--rose); }
 .body { margin: 2px 0 0 15px; padding: 4px 0 4px 10px; border-left: 1px solid var(--border); }
 .stage + .stage { margin-top: 8px; }
 .stage-label { padding: 6px 8px; font-size: 11px; color: var(--muted-foreground); }
 .task { min-width: 0; }
 summary { display: flex; align-items: center; gap: 9px; padding: 6px 8px; border-radius: 8px; cursor: pointer; list-style: none; }
 summary::-webkit-details-marker { display: none; }
 summary :global(.task-chevron) { color: var(--subtle-foreground); flex: none; }
 .task-dot { height: 6px; width: 6px; border: 1px solid var(--muted-foreground); border-radius: 50%; flex: none; }
 .task[data-state='running'] .task-dot { border-color: var(--primary); border-top-color: transparent; animation: spin .9s linear infinite; }
 .task[data-state='completed'] .task-dot { border-color: var(--emerald); background: var(--emerald); }
 .task[data-state='failed'] .task-dot, .task[data-state='blocked'] .task-dot { border-color: var(--rose); }
 .task-title { min-width: 0; font-size: 12px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
 .agent { min-width: 0; flex: 1; font-size: 11px; color: var(--muted-foreground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
 .task-body { margin: 2px 0 6px 11px; border-left: 1px solid var(--border); padding: 6px 10px; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
 p { margin: 0 0 6px; } .dependencies, .muted { color: var(--muted-foreground); font-size: 12px; }
 pre { white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; font: 11px/1.7 var(--font-mono); max-height: 24rem; overflow: auto; margin: 6px 0; }
 footer { padding: 4px 8px; }
 .transcript-link { display: inline-flex; align-items: center; gap: 6px; padding: 5px 0; color: var(--primary); font-size: 11px; text-decoration: none; }
 .transcript-link:hover { text-decoration: underline; }
 .text-button { background: transparent; color: var(--muted-foreground); border: 0; padding: 5px 0; font-size: 11px; text-align: left; }
 .text-button:hover { text-decoration: underline; } .text-button.danger { color: var(--rose); }
 .cancel-confirm, .refresh { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; font-size: 11px; }
 .notice { padding: 10px 8px; font-size: 12px; line-height: 1.6; margin: 0; }
 .raw-result { margin: 0 8px 8px; }
 @keyframes spin { to { transform: rotate(360deg); } }
 @media (prefers-reduced-motion: reduce) { .swarm :global(.running-icon), .task[data-state='running'] .task-dot { animation: none; } }
 @media (max-width: 639px) { .heading, summary { min-height: 44px; } .text-button, .transcript-link { min-height: 44px; } .task-title { flex: 1; } .agent { flex: 0 1 70px; } }
</style>

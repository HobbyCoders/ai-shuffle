<script lang="ts">
 /**
  * Which worker the assistant's background work runs on. Shown only when the
  * account has another active worker; with one worker nothing renders. A
  * worker that cannot take the work is listed but cannot be picked.
  */
 import { onMount } from 'svelte';
 import { Check } from 'lucide-svelte';
 import { getAssistantWorker, updateAssistantWorker, assistantError, type AssistantWorkerChoice, type AssistantWorkerOption } from '$lib/api/assistant';
 import { sessionOwnerEpoch } from '$lib/stores/session-owner';

 const owner = sessionOwnerEpoch();
 let disposed = false;
 const current = () => !disposed && owner === sessionOwnerEpoch();
 let choice = $state<AssistantWorkerChoice | null>(null);
 let busy = $state(false);
 let error = $state('');
 let saved = $state(false);
 const blocked = (worker: AssistantWorkerOption) => !worker.remote_task ? 'Needs update' : !worker.online ? 'Offline' : '';

 async function load() {
  // A failed read leaves the row hidden: it is optional, not worth an error.
  try { const next = await getAssistantWorker(); if (current()) choice = next; }
  catch { /* keep what was shown */ }
 }
 onMount(() => {
  void load();
  const tick = setInterval(() => { if (document.visibilityState === 'visible' && !busy) void load(); }, 30000);
  return () => { disposed = true; clearInterval(tick); };
 });
 async function pick(event: Event) {
  const select = event.currentTarget as HTMLSelectElement;
  const previous = choice?.device_id ?? '';
  if (busy || select.value === previous) return;
  busy = true; error = ''; saved = false;
  try { const next = await updateAssistantWorker(select.value || null); if (current()) { choice = next; saved = true; } }
  catch (e) { if (current()) { error = assistantError(e); select.value = previous; } }
  finally { if (current()) busy = false; }
 }
</script>

{#if choice?.workers.length}
 <div class="field">
  <div class="field-copy"><label for="work-worker">Run work on</label><small id="work-worker-help">Where background jobs run</small></div>
  <div class="controls">
   <select id="work-worker" aria-describedby="work-worker-help" value={choice.device_id ?? ''} disabled={busy} onchange={pick}>
    <option value="">This worker</option>
    {#each choice.workers as worker (worker.device_id)}
     <option value={worker.device_id} disabled={!!blocked(worker)}>{worker.name}{blocked(worker) ? ` · ${blocked(worker)}` : ''}</option>
    {/each}
   </select>
   <!-- Keeps this dropdown in the same column as the ones beside a switch. -->
   <span class="switch-room" aria-hidden="true"></span>
  </div>
 </div>
 {#if error}<p class="message" role="alert">{error}</p>{/if}
 {#if saved}<p class="note" role="status"><Check size={14} />Saved</p>{/if}
{/if}

<style>
 .field { display:flex; align-items:center; justify-content:space-between; gap:12px 20px; flex-wrap:wrap; padding:16px 0; border-bottom:1px solid var(--border); max-width:640px; }
 .field-copy { display:flex; flex-direction:column; gap:4px; min-width:0; flex:1 1 200px; }
 .field-copy label { font-size:13px; font-weight:500; }
 .field-copy small { font-size:11px; color:var(--muted-foreground); }
 .controls { display:flex; align-items:center; gap:10px; flex-wrap:wrap; min-width:0; }
 select { border:1px solid var(--border); background:var(--background); color:var(--foreground); border-radius:7px; padding:0 12px; font:inherit; font-size:13px; min-height:44px; min-width:130px; max-width:100%; cursor:pointer; transition:border-color .2s; }
 select:hover:not(:disabled) { border-color:var(--input-border-hover, var(--primary)); }
 select:focus-visible { outline:2px solid var(--ring); outline-offset:3px; }
 select:disabled { opacity:.55; cursor:not-allowed; }
 .switch-room { width:64px; flex:none; }
 .note { display:flex; align-items:center; gap:6px; font-size:11px; color:var(--muted-foreground); margin:8px 0 0; }
 .note :global(svg) { color:var(--primary); }
 .message { padding:12px 14px; border:1px solid var(--destructive); border-radius:8px; font-size:12px; margin:14px 0 0; max-width:640px; }
 @media(prefers-reduced-motion:reduce) { select { transition:none; } }
</style>

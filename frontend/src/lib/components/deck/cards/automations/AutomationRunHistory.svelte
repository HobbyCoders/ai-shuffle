<script lang="ts">
	import { runTriggerLabel, type AutomationRun } from '$lib/api/automations';
	import { RowGroup } from '$lib/components/layout/view';
	import {
		Loader2,
		ChevronDown,
		ChevronRight
	} from 'lucide-svelte';

	interface Props {
		runs: AutomationRun[];
		runsLoading: boolean;
		expandedRunIds: Set<string>;
		onToggleRunExpanded: (runId: string) => void;
		formatTime: (dateStr: string | null) => string;
		formatCost: (cost: number) => string;
		formatDuration: (ms: number) => string;
		getRunStatusBadgeClass: (status: AutomationRun['status']) => string;
	}

	let {
		runs,
		runsLoading,
		expandedRunIds,
		onToggleRunExpanded,
		formatTime,
		formatCost,
		formatDuration,
		getRunStatusBadgeClass
	}: Props = $props();
</script>
<RowGroup title="Run history" bare>
 {#if runsLoading}<p class="st-note" role="status">Loading runs…</p>
 {:else if !runs.length}<p class="st-note">No runs yet</p>
 {:else}
  <div class="runs-list">
   {#each runs as run (run.id)}
    <div class="run">
     <button class="run-row" aria-expanded={expandedRunIds.has(run.id)} onclick={() => onToggleRunExpanded(run.id)}>
      {#if expandedRunIds.has(run.id)}<ChevronDown size={14} />{:else}<ChevronRight size={14} />{/if}
      <span class:failed={run.status === 'failed'}>{run.status}</span>
      <span>{runTriggerLabel(run.trigger_data)}</span>
      <span>{formatCost(run.cost_usd)}</span><span>{formatDuration(run.duration_ms)}</span>
      <span class="run-time">{formatTime(run.created_at)}</span>
     </button>
     {#if expandedRunIds.has(run.id)}
      <div class="run-expanded">
       {#if run.response}<h4>Response</h4><p class="response">{run.response}</p>{/if}
       {#if run.error}<h4>Error</h4><p class="response failed">{run.error}</p>{/if}
       <div class="run-facts"><span>Tokens in: {run.tokens_in}</span><span>Tokens out: {run.tokens_out}</span><span>Output: {run.output_status}</span>{#if run.session_id}<span>Session: {run.session_id.slice(0,8)}...</span>{/if}</div>
      </div>
     {/if}
    </div>
   {/each}
  </div>
 {/if}
</RowGroup>
<style>
 .run { border-bottom: 1px solid var(--st-border); }
 .run-row { display:flex; flex-wrap:wrap; align-items:center; gap:8px 12px; width:100%; min-height:52px; padding:12px 0; background:transparent; border:0; color:var(--st-mfg); font-size:13px; text-align:left; font-variant-numeric:tabular-nums; transition:background .2s ease-out; }
 .run-row:hover { background:var(--st-hover); }
 .run-row:active { background:var(--st-accent); }
 .run-time { margin-left:auto; }
 .run-expanded { padding:4px 0 20px 26px; font-size:13px; }
 h4 { font-weight:500; margin:12px 0 4px; }
 .response { white-space:pre-wrap; overflow-wrap:anywhere; max-height:320px; overflow:auto; }
 .failed { color:var(--st-destructive); }
 .run-facts { display:flex; flex-wrap:wrap; gap:8px 16px; color:var(--st-mfg); margin-top:16px; font-size:12px; }
 @media (max-width:600px) { .run-time { flex-basis:100%; margin-left:26px; } }
 @media (prefers-reduced-motion:reduce) { .run-row { transition:none; } }
</style>

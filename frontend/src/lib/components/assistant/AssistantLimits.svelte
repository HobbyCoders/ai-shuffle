<script lang="ts">
 /**
  * The owner's limits for the assistant: a daily spending limit for it and
  * every job it starts, and the longest one background job may run. The
  * server enforces both; switching the spending limit off asks first.
  */
 import { onMount } from 'svelte';
 import { Check, Gauge } from 'lucide-svelte';
 import { getAssistantLimits, updateAssistantLimits, assistantError, type AssistantLimits, type AssistantLimitsPatch } from '$lib/api/assistant';
 import { sessionOwnerEpoch } from '$lib/stores/session-owner';

 const owner = sessionOwnerEpoch();
 let disposed = false;
 const current = () => !disposed && owner === sessionOwnerEpoch();
 let limits = $state<AssistantLimits | null>(null);
 let amount = $state('');
 let busy = $state(false);
 let error = $state('');
 let saved = $state(false);
 let confirmingOff = $state(false);
 const JOB_TIMES = [30, 60, 120, 240, 480, 1440];
 const jobTimes = $derived(limits && !JOB_TIMES.includes(limits.max_job_minutes) ? [...JOB_TIMES, limits.max_job_minutes].sort((a, b) => a - b) : JOB_TIMES);
 const money = (n: number) => new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(n);
 const duration = (minutes: number) => minutes % 60 === 0 ? `${minutes / 60} ${minutes === 60 ? 'hour' : 'hours'}` : `${minutes} minutes`;
 const used = $derived(limits && limits.spend_limit_enabled && limits.daily_spend_limit_usd > 0 ? Math.min(100, limits.spent_today_usd / limits.daily_spend_limit_usd * 100) : 0);

 function accept(next: AssistantLimits) {
  limits = next;
  amount = String(next.daily_spend_limit_usd);
 }
 async function load() {
  try { const next = await getAssistantLimits(); if (current()) { accept(next); error = ''; } }
  catch (e) { if (current()) error = assistantError(e); }
 }
 onMount(() => {
  void load();
  const tick = setInterval(() => { if (document.visibilityState === 'visible' && !busy && !confirmingOff) void load(); }, 30000);
  return () => { disposed = true; clearInterval(tick); };
 });
 async function save(patch: AssistantLimitsPatch) {
  if (busy) return;
  busy = true; error = ''; saved = false;
  try { const next = await updateAssistantLimits(patch); if (current()) { accept(next); saved = true; confirmingOff = false; } }
  catch (e) { if (current()) { error = assistantError(e); if (limits) amount = String(limits.daily_spend_limit_usd); } }
  finally { if (current()) busy = false; }
 }
 function toggleSpend() {
  if (!limits) return;
  if (limits.spend_limit_enabled) { confirmingOff = true; saved = false; return; }
  void save({ spend_limit_enabled: true });
 }
 function saveAmount() {
  if (!limits) return;
  const value = Number(amount);
  if (!Number.isFinite(value) || value < 0.5 || value > 10000) { error = 'Enter an amount from $0.50 to $10,000.'; return; }
  if (value !== limits.daily_spend_limit_usd) void save({ daily_spend_limit_usd: Math.round(value * 100) / 100 });
 }
</script>

<section class="limits" aria-labelledby="limits-heading">
 <div class="section-heading"><div><span class="eyebrow">YOUR MONEY</span><h2 id="limits-heading">Limits</h2></div><Gauge size={20} /></div>
 {#if error}<p class="message error" role="alert">{error}</p>{/if}
 {#if !limits}
  {#if !error}<p class="muted" role="status">Loading limits…</p>{/if}
 {:else}
  <div class="today">
   {#if limits.spend_limit_enabled}
    <p><strong>{money(limits.spent_today_usd)}</strong> of {money(limits.daily_spend_limit_usd)} spent today</p>
    <div class="meter" class:warn={limits.warning} class:full={limits.limit_reached} role="progressbar" aria-label="Spent today" aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(used)}><span style:width={`${used}%`}></span></div>
    {#if limits.limit_reached}<p class="note reached">Limit reached. New work waits until tomorrow.</p>{/if}
   {:else}
    <p><strong>{money(limits.spent_today_usd)}</strong> spent today · no daily limit</p>
   {/if}
  </div>

  <div class="field">
   <div class="field-copy"><label for="limit-amount">Daily spending limit</label><small id="limit-amount-help">All work, at API prices</small></div>
   <div class="controls">
    <span class="amount" class:off={!limits.spend_limit_enabled}><span aria-hidden="true">$</span><input id="limit-amount" type="number" inputmode="decimal" min="0.5" max="10000" step="1" aria-describedby="limit-amount-help" bind:value={amount} disabled={busy || !limits.spend_limit_enabled} onchange={saveAmount} /></span>
    <button class="switch" role="switch" aria-checked={limits.spend_limit_enabled} aria-label="Daily spending limit" disabled={busy || confirmingOff} onclick={toggleSpend}><span aria-hidden="true"></span><span>{limits.spend_limit_enabled ? 'On' : 'Off'}</span></button>
   </div>
  </div>
  {#if confirmingOff}
   <div class="confirm" role="group" aria-labelledby="limit-off-question">
    <p id="limit-off-question"><strong>Turn off the daily spending limit?</strong> Your assistant and its jobs could then spend without a cap.</p>
    <div class="confirm-actions"><button class="danger" disabled={busy} onclick={() => save({ spend_limit_enabled: false, confirm_spend_limit_off: true })}>{busy ? 'Turning off…' : 'Turn off limit'}</button><button class="secondary" disabled={busy} onclick={() => confirmingOff = false}>Keep it on</button></div>
   </div>
  {/if}

  <div class="field">
   <div class="field-copy"><label for="limit-job">Longest background job</label><small id="limit-job-help">Applies to each job</small></div>
   <div class="controls">
    <select id="limit-job" aria-describedby="limit-job-help" value={limits.max_job_minutes} disabled={busy || !limits.job_limit_enabled} onchange={event => save({ max_job_minutes: Number((event.currentTarget as HTMLSelectElement).value) })}>
     {#each jobTimes as minutes (minutes)}<option value={minutes}>{duration(minutes)}</option>{/each}
    </select>
    <button class="switch" role="switch" aria-checked={limits.job_limit_enabled} aria-label="Longest background job" disabled={busy} onclick={() => limits && save({ job_limit_enabled: !limits.job_limit_enabled })}><span aria-hidden="true"></span><span>{limits.job_limit_enabled ? 'On' : 'Off'}</span></button>
   </div>
  </div>
  {#if saved}<p class="note saved" role="status"><Check size={14} />Saved</p>{/if}
 {/if}
</section>

<style>
 .limits { padding-top:34px; margin-top:30px; border-top:1px solid var(--border); }
 .section-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
 .section-heading> :global(svg) { color:var(--primary); flex:none; }
 .eyebrow { font-family:var(--font-mono); font-size:10px; letter-spacing:.14em; color:var(--muted-foreground); }
 h2 { font-size:18px; font-weight:550; letter-spacing:-.025em; margin:7px 0 0; }
 .muted { font-size:12px; color:var(--muted-foreground); margin:14px 0 0; }
 .today { margin:18px 0 6px; max-width:520px; }
 .today p { font-size:13px; color:var(--muted-foreground); margin:0 0 10px; }
 .today strong { color:var(--foreground); font-size:16px; font-weight:550; }
 .meter { height:6px; border-radius:6px; background:var(--muted); overflow:hidden; }
 .meter span { display:block; height:100%; background:var(--primary); border-radius:inherit; transition:width .3s; }
 .meter.warn span { background:var(--warning, var(--primary)); }
 .meter.full span { background:var(--destructive); }
 .note { display:flex; align-items:center; gap:6px; font-size:11px; color:var(--muted-foreground); margin:8px 0 0; }
 .note.reached { color:var(--destructive); }
 .note.saved :global(svg) { color:var(--primary); }
 .field { display:flex; align-items:center; justify-content:space-between; gap:12px 20px; flex-wrap:wrap; padding:16px 0; border-bottom:1px solid var(--border); max-width:640px; }
 .field-copy { display:flex; flex-direction:column; gap:4px; min-width:0; flex:1 1 200px; }
 .field-copy label { font-size:13px; font-weight:500; }
 .field-copy small { font-size:11px; color:var(--muted-foreground); }
 .controls { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
 .amount { display:inline-flex; align-items:center; gap:6px; border:1px solid var(--border); border-radius:7px; padding:0 10px; background:var(--background); min-height:44px; }
 .amount.off { opacity:.55; }
 .amount input { width:90px; border:0; background:transparent; color:var(--foreground); font:inherit; min-height:42px; padding:0; }
 .amount input:focus-visible { outline:none; }
 .amount:focus-within { outline:2px solid var(--ring); outline-offset:3px; }
 select { border:1px solid var(--border); background:var(--background); color:var(--foreground); border-radius:7px; padding:0 12px; font:inherit; font-size:13px; min-height:44px; min-width:130px; }
 select:disabled { opacity:.55; cursor:not-allowed; }
 button { cursor:pointer; font:inherit; }
 button:disabled { opacity:.55; cursor:not-allowed; }
 button:focus-visible,select:focus-visible { outline:2px solid var(--ring); outline-offset:3px; }
 .switch { display:inline-flex; align-items:center; gap:8px; min-height:44px; min-width:64px; color:var(--muted-foreground); background:transparent; border:0; font-size:11px; padding:0; }
 .switch>span:first-child { width:30px; height:18px; border:1px solid var(--border); border-radius:20px; background:var(--muted); position:relative; flex:none; }
 .switch>span:first-child::after { content:''; position:absolute; width:12px; height:12px; top:2px; left:2px; border-radius:50%; background:var(--muted-foreground); transition:transform .2s; }
 .switch[aria-checked=true]>span:first-child { background:var(--primary); border-color:var(--primary); }
 .switch[aria-checked=true]>span:first-child::after { transform:translateX(12px); background:var(--primary-foreground); }
 .confirm { max-width:640px; margin:12px 0 4px; padding:14px 16px; border:1px solid var(--destructive); border-radius:8px; background:var(--card); }
 .confirm p { font-size:12px; line-height:1.7; margin:0; }
 .confirm-actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
 .danger,.secondary { display:inline-flex; align-items:center; justify-content:center; min-height:44px; border-radius:8px; padding:0 14px; font-size:12px; font-weight:550; }
 .danger { background:var(--destructive); color:var(--destructive-foreground, #fff); border:1px solid var(--destructive); }
 .secondary { background:var(--card); color:var(--foreground); border:1px solid var(--border); }
 .message { padding:12px 14px; border:1px solid var(--destructive); border-radius:8px; font-size:12px; margin:14px 0 0; max-width:640px; }
 @media(prefers-reduced-motion:reduce) { .meter span,.switch>span:first-child::after { transition:none; } }
</style>

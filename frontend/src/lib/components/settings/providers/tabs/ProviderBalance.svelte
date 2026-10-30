<script lang="ts">
 import { onMount } from 'svelte';
 import { getErrorMessage } from '$lib/api/client';
 import { getProviderBalance, saveProviderBalance, removeProviderBalance, type ProviderBalance } from '$lib/api/provider-balances';
 import { getSettingsAccess } from '$lib/settings/access.svelte';
 import RowGroup from '../../rows/RowGroup.svelte';
 import SettingRow from '../../rows/SettingRow.svelte';
 import TextField from '../../rows/TextField.svelte';
 import ValueText from '../../rows/ValueText.svelte';

 let { providerId }: { providerId: string } = $props();
 const access = getSettingsAccess();
 let status = $state<ProviderBalance | null>(null);
 let now = $state(Date.now());
 let key = $state('');
 let busy = $state(false);
 let error = $state('');
 const reading = $derived(!error && !status?.last_error_kind && !status?.needs_reauth && status?.status === 'fresh' && status.fetched_at && now <= (status.fresh_until ?? status.fetched_at) ? status.credits : null);
 let disposed = false;
 let reread: ReturnType<typeof setTimeout> | undefined;
 async function refresh() {
  const value = await getProviderBalance(providerId);
  if (disposed) return;
  now = Date.now();
  status = value;
  error = '';
  clearTimeout(reread);
  if (value.refreshing) reread = setTimeout(() => {
   refresh().catch((e) => { if (!disposed) error = getErrorMessage(e); });
  }, 1000);
 }
 onMount(() => {
  refresh().catch((e) => { if (!disposed) error = getErrorMessage(e); });
  const clock = setInterval(() => { now = Date.now(); }, 10_000);
  return () => { disposed = true; clearInterval(clock); clearTimeout(reread); key = ''; };
 });
 async function save(remove = false) {
  if (busy || access.readOnly) return;
  busy = true; error = '';
  const value = key;
  key = ''; // Never keep the write-only key after dispatch, even on failure.
  try {
   status = await (remove ? removeProviderBalance(providerId) : saveProviderBalance(providerId, value));
   await refresh();
  } catch (e) { error = getErrorMessage(e); }
  finally { busy = false; }
 }
</script>

<RowGroup title="xAI Management API balance">
 <SettingRow label="Remaining" hint={status?.source ?? 'Provider-reported prepaid credit'}>
  <ValueText value={reading ? `$${reading.balance_remaining.toFixed(2)}` : status?.configured ? 'Balance unavailable' : 'Not reported yet'} />
 </SettingRow>
 {#if status?.fetched_at}<p class="balance-note">Last fetched: {new Date(status.fetched_at).toLocaleString()} · {status.status}</p>{/if}
 {#if status?.needs_reauth}<p role="status">Management access refused. Replace or re-save the management key.</p>
 {:else if status?.configured && !reading}<p role="status">Balance unavailable; no current reading. Refresh after polling completes.</p>{/if}
 {#if reading?.spend_month !== undefined}<SettingRow label="Cycle usage"><ValueText value={`$${reading.spend_month.toFixed(2)}`} /></SettingRow>{/if}
 {#if reading?.spending_limit !== undefined}<SettingRow label="Postpaid spending limit"><ValueText value={`$${reading.spending_limit.toFixed(2)}`} /></SettingRow>{/if}
 <p class="balance-note">Create a management key in console.x.ai → Settings → Management Keys with billing read access. The normal API key resolves its team; the management key reads prepaid balance and invoice preview.</p>
 <SettingRow label="Management key" hint="Write-only · synced encrypted">
  <div class="access-controls">
   <TextField bind:value={key} type="password" label="xAI management key" autocomplete="off" disabled={busy} />
   <button type="button" class="st-btn primary sm" disabled={!key.trim() || busy || access.readOnly} onclick={() => save()}>Save access</button>
   <button type="button" class="st-btn ghost sm" disabled={!status?.configured || busy || access.readOnly} onclick={() => save(true)}>Disconnect balance</button>
  </div>
 </SettingRow>
 <button type="button" class="st-btn ghost sm" disabled={busy} onclick={() => refresh().catch((e) => { error = getErrorMessage(e); })}>Refresh balance status</button>
 {#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
</RowGroup>

<style>
 .balance-note { color: var(--st-mfg); font-size: 12px; margin: 8px 16px; overflow-wrap: anywhere; }
 .access-controls { display: flex; flex-wrap: wrap; gap: 8px; width: 100%; min-width: 0; }
 .access-controls :global(.st-txt) { flex: 1 1 100%; }
 @media (max-width: 600px), (pointer: coarse) {
  .st-btn, .access-controls :global(.st-txt) { min-height: 44px; }
 }
</style>

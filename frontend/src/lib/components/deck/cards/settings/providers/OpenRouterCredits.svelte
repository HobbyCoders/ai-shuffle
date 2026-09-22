<script lang="ts">
 import { onMount } from 'svelte';
 import type { ModelProvider } from '$lib/api/model-providers';
 import { getOpenRouterUsage, setOpenRouterUsage, disconnectOpenRouterUsage, type OpenRouterUsageStatus } from '$lib/api/openrouter-billing';
 let { provider }: { provider: ModelProvider } = $props();
 let status = $state<OpenRouterUsageStatus | null>(null);
 let managementKey = $state('');
 let busy = $state(false);
 let error = $state('');
 let disposed = false;
 const money = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
 async function load() {
  busy = true;
  try { const next = await getOpenRouterUsage(provider.id); if (!disposed) status = next; }
  catch { if (!disposed) error = 'Could not load account credit settings.'; }
  finally { busy = false; }
 }
 async function save() {
  busy = true; error = '';
  try { const next = await setOpenRouterUsage(provider.id, managementKey); if (!disposed) status = next; }
  catch { if (!disposed) error = 'Could not confirm account credit access. Read cached status to check whether the key was saved, then try again when connected.'; }
  finally { managementKey = ''; busy = false; }
 }
 async function disconnect() {
  busy = true; error = '';
  try { const next = await disconnectOpenRouterUsage(provider.id); if (!disposed) status = next; }
  catch { if (!disposed) error = 'Could not remove the management key.'; }
  finally { busy = false; }
 }
 onMount(() => { void load(); return () => { disposed = true; managementKey = ''; }; });
</script>

<section class="account-credits" aria-label="OpenRouter account credits">
 <h4>Account credits</h4>
 <p>Optional access through OpenRouter’s official management API. The model key reports its own spend and limit separately.</p>
 <p class="muted">Use a management key from the same account as this provider. OpenRouter management keys can also create, edit and delete API keys. This app uses it only to read account credits and stores it encrypted on this worker.</p>
 <a href="https://openrouter.ai/settings/management-keys" target="_blank" rel="noopener noreferrer">Manage OpenRouter management keys</a>
 {#if status?.configured}
  <p role="status">Management key saved{status.fetched_at ? '' : ' — awaiting a successful usage refresh'}.</p>
  {#if status.last_error_kind}
   <p class="warning">{status.last_error_kind === 'billing_access_denied' ? 'OpenRouter refused management access. Polling is paused for this key; check its permissions, then save or replace it.' : 'Account credits are unavailable. The last successful reading is retained; retries are delayed.'}</p>
  {/if}
  {#if status.credits}<p>{money.format(status.credits.balance_remaining)} account credit remaining</p>{/if}
  {#if status.fetched_at}<p class="muted">Last successful update: {new Date(status.fetched_at).toLocaleString()}. Provider reporting may be delayed.</p>{/if}
 {:else if status}
  <p>Account credit access is not configured.</p>
 {/if}
 <form onsubmit={(event) => { event.preventDefault(); void save(); }}>
  <label>Management API key<input type="password" bind:value={managementKey} autocomplete="off" required disabled={busy} /></label>
  <button type="submit" disabled={busy || !managementKey.trim()}>{status?.configured ? 'Replace management key' : 'Save management key'}</button>
 </form>
 <p class="muted">Credits refresh on demand, at most once every five minutes per credential. Account credit is reported by OpenRouter, not estimated from this app’s conversations.</p>
 <div class="actions">
  <button type="button" onclick={() => { error = ''; void load(); }} disabled={busy}>Read cached status</button>
  {#if status?.configured}<button type="button" onclick={() => void disconnect()} disabled={busy}>Remove management key</button>{/if}
 </div>
 <a href="https://openrouter.ai/settings/credits" target="_blank" rel="noopener noreferrer">View credits in OpenRouter</a>
 {#if error}<p role="alert">{error}</p>{/if}
</section>

<style>
 .account-credits { display: grid; gap: .7rem; padding: 1rem 0; min-width: 0; }
 h4, p { margin: 0; }
 p, a { font-size: .85rem; overflow-wrap: anywhere; }
 .muted { opacity: .75; }
 form, label { display: grid; gap: .4rem; min-width: 0; }
 form { gap: .7rem; }
 input { width: 100%; min-width: 0; box-sizing: border-box; padding: .6rem; background: var(--background); color: inherit; border: 1px solid var(--border); border-radius: .4rem; }
 button { padding: .5rem .75rem; border-radius: .4rem; border: 1px solid var(--border); background: transparent; color: inherit; cursor: pointer; }
 button:disabled { opacity: .5; cursor: default; }
 .actions { display: flex; flex-wrap: wrap; gap: .5rem; }
 .warning, [role='alert'] { color: var(--destructive); }
</style>

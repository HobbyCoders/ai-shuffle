<script lang="ts">
 import { onMount } from 'svelte';
 import type { ModelProvider } from '$lib/api/model-providers';
 import { getOpenAIUsage, setOpenAIUsage, disconnectOpenAIUsage, type OpenAIUsageStatus } from '$lib/api/openai-billing';
 let { provider }: { provider: ModelProvider } = $props();
 let status = $state<OpenAIUsageStatus | null>(null);
 let adminKey = $state('');
 let organization = $state('');
 let busy = $state(false);
 let error = $state('');
 let removing = $state(false);
 let disposed = false;
 const money = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
 async function load() {
  busy = true; error = '';
  try {
   const next = await getOpenAIUsage(provider.id);
   if (!disposed) { status = next; organization = next.organization_id ?? ''; }
  } catch { if (!disposed) error = 'Could not load usage settings.'; }
  finally { busy = false; }
 }
 async function save() {
  busy = true; error = '';
  try {
   const next = await setOpenAIUsage(provider.id, adminKey, organization.trim());
   if (!disposed) status = next;
  } catch { if (!disposed) error = 'Could not save usage access. Check the organization ID and try again.'; }
  finally { adminKey = ''; busy = false; }
 }
 async function disconnect() {
  busy = true; error = '';
  try { status = await disconnectOpenAIUsage(provider.id); removing = false; organization = ''; }
  catch { error = 'Could not disconnect usage access.'; }
  finally { busy = false; }
 }
 onMount(() => { void load(); return () => { disposed = true; adminKey = ''; }; });
</script>

<section class="usage-admin" aria-label="OpenAI account usage">
 <h4>Organization usage and spend</h4>
 <p>Uses OpenAI’s official Admin API. This does not retrieve your prepaid credit balance or ChatGPT subscription allowance.</p>
 <p class="muted">Optional access, stored encrypted on this worker only. Your model API key is unchanged. Use an Admin API key with permission to read organization usage and costs; grant no other permissions where OpenAI allows it.</p>
 <a href="https://platform.openai.com/settings/organization/admin-keys" target="_blank" rel="noopener noreferrer">Manage OpenAI Admin API keys</a>
 {#if status?.configured}
  <p role="status">Admin key saved{status.fetched_at ? '' : ' — awaiting a successful usage refresh'}.</p>
  {#if status.last_error_kind}
   <p class="warning">{status.last_error_kind === 'billing_access_denied' ? 'OpenAI refused usage access. Polling is paused for this key; check its organization and permissions, then replace it.' : 'Usage is unavailable. The last successful reading is retained; retries are delayed.'}</p>
  {/if}
  {#if status.spend}
   <p>{money.format(status.spend.spend_month)} organization spend this month (UTC)</p>
   <p>{status.spend.num_model_requests.toLocaleString()} completions requests · {status.spend.input_tokens.toLocaleString()} input tokens · {status.spend.output_tokens.toLocaleString()} output tokens</p>
  {/if}
  {#if status.fetched_at}<p class="muted">Last successful update: {new Date(status.fetched_at).toLocaleString()}. Provider reporting may be delayed.</p>{/if}
 {:else if status}
  <p>Admin usage access is not configured. Previous browser credit tracking is no longer used.</p>
 {/if}
 <form onsubmit={(event) => { event.preventDefault(); void save(); }}>
  <label>Organization ID<input bind:value={organization} placeholder="org-…" required pattern="org-(?:[A-Za-z0-9_]|-)+" disabled={busy} /></label>
  <label>Admin API key<input type="password" bind:value={adminKey} autocomplete="off" required disabled={busy} /></label>
  <button type="submit" disabled={busy || !adminKey.trim() || !organization.trim()}>{status?.configured ? 'Replace admin key' : 'Save admin key'}</button>
 </form>
 <p class="muted">Usage refreshes on demand, no more than once every 15 minutes per endpoint and credential. Opening Usage cannot bypass this limit. Spend covers the organization; token counts cover completions, not every API product.</p>
 <div class="actions">
  <button type="button" onclick={() => void load()} disabled={busy}>Read cached status</button>
  {#if status?.configured}<button type="button" onclick={() => removing = true} disabled={busy}>Disconnect usage</button>{/if}
 </div>
 {#if removing}
  <p>Remove this worker’s admin key and saved usage? Your model API key will remain connected.</p>
  <button type="button" onclick={() => void disconnect()} disabled={busy}>Remove admin key</button>
  <button type="button" onclick={() => removing = false} disabled={busy}>Cancel</button>
 {/if}
 <a href="https://platform.openai.com/settings/organization/billing/overview" target="_blank" rel="noopener noreferrer">View prepaid balance in OpenAI billing</a>
 {#if error}<p role="alert">{error}</p>{/if}
</section>

<style>
 .usage-admin { display: grid; gap: .7rem; padding: 1rem 0; min-width: 0; }
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

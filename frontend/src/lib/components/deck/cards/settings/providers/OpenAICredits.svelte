<script lang="ts">
 import { onMount } from 'svelte';
 import type { ModelProvider } from '$lib/api/model-providers';
 import { getOpenAIBillingStatus, connectOpenAIBilling, confirmOpenAIBilling, cancelOpenAIBilling, disconnectOpenAIBilling, type OpenAIBillingStatus } from '$lib/api/openai-billing';
 import BrowserLiveView from '$lib/components/browser/BrowserLiveView.svelte';
 import { createBrowserStreamClient, type BrowserStreamClient, type FrameMessageMetadata, type StreamStatus } from '$lib/browser-panel/stream-client';

 let { provider }: { provider: ModelProvider } = $props();
 let status = $state<OpenAIBillingStatus | null>(null);
 let busy = $state(false);
 let error = $state('');
 let open = $state(false);
 let disconnecting = $state(false);
 let dialog = $state<HTMLDialogElement>();
 let frame = $state<{ data: string; metadata: FrameMessageMetadata } | null>(null);
 let streamStatus = $state<StreamStatus>('connecting');
 let stream: BrowserStreamClient | null = null;
 let timer: ReturnType<typeof setTimeout> | undefined;
 let disposed = false;
 let ownedFlow = $state<string | null>(null);
 let revision = 0;
 let bound = $derived(status ? status.status === 'connected' || Boolean(status.organization_id && status.status !== 'ready') : Boolean(provider.openai_billing_org_id));
 let reconnecting = $state(false);
 let browserElement = $state<HTMLDivElement>();
 let liveView = $state<BrowserLiveView>();
 let keyboardOpen = $state(false);
 let keyboardEnabled = $derived(open && Boolean(ownedFlow && frame) && streamStatus === 'live' && !busy);
 function sendViewport() {
  if (!browserElement) return;
  const { width, height } = browserElement.getBoundingClientRect();
  if (width > 0 && height > 0) stream?.send({ type: 'viewport', width: Math.round(width), height: Math.round(height) });
 }
 $effect(() => {
  if (!browserElement) return;
  const observer = new ResizeObserver(sendViewport);
  observer.observe(browserElement);
  return () => observer.disconnect();
 });
 let remote = $derived(status?.status === 'other_installation');
 let stale = $derived(status?.status === 'reconnect_required' || status?.status === 'unavailable');
 let savedBalance = $derived(status && ['connected', 'other_installation', 'reconnect_required', 'unavailable'].includes(status.status) && typeof status.credits?.balance_remaining === 'number' && Number.isFinite(status.credits.balance_remaining) ? status.credits.balance_remaining : null);
 const currency = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
 let ready = $derived(status?.status === 'ready' && status.flow_id === ownedFlow && Boolean(status.observation_id && status.organization_id) && typeof status.credits?.balance_remaining === 'number' && Number.isFinite(status.credits.balance_remaining));
 const labels: Record<OpenAIBillingStatus['status'], string> = {
  not_connected: 'Not connected', pending: 'Waiting for sign-in', ready: 'Review organization', connected: 'Connected',
  reconnect_required: 'Sign in again to update credits', unavailable: 'Credit tracking unavailable', other_installation: 'Connected on another installation'
 };
 function stopStream() { stream?.destroy(); stream = null; frame = null; }
 function clearPoll() { if (timer) clearTimeout(timer); timer = undefined; }
 function startStream() {
  if (stream || !ownedFlow) return;
  streamStatus = 'connecting';
  stream = createBrowserStreamClient({
   providerId: provider.id,
   getAuthToken: () => document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('session='))?.slice(8) ?? null,
   onOpen: sendViewport,
   onStatus: state => { streamStatus = state; if (state === 'live') sendViewport(); },
   onFrame: (data, metadata) => { frame = { data, metadata }; },
   onError: () => { error = 'The browser connection was interrupted. Close this dialog and try signing in again.'; }
  });
  stream.connect();
 }
 async function load(poll = false) {
  const current = revision;
  try {
   const next = await getOpenAIBillingStatus(provider.id);
   if (disposed || current !== revision) return;
   status = next;
   if (open && ownedFlow && (next.flow_id !== ownedFlow || !['pending', 'ready'].includes(next.status))) {
    ownedFlow = null;
    stopStream();
   }
  } catch { if (!disposed && current === revision) error = 'Could not load credit tracking. Try again.'; }
  finally { if (poll && open && !disposed && current === revision) timer = setTimeout(() => void load(true), 2000); }
 }
 async function begin() {
  reconnecting = false;
  busy = true; error = ''; revision++; clearPoll();
  try {
   const next = await connectOpenAIBilling(provider.id);
   if (disposed) {
    if (next.flow_id && ['pending', 'ready'].includes(next.status)) void cancelOpenAIBilling(provider.id, next.flow_id).catch(() => {});
    return;
   }
   status = next;
   ownedFlow = ['pending', 'ready'].includes(next.status) ? next.flow_id : null;
   open = true;
   startStream();
   timer = setTimeout(() => void load(true), 2000);
  } catch { error = 'Could not start credit sign-in. Make sure the browser is running, then try again.'; }
  finally { busy = false; }
 }
 async function close() {
  if (busy) return;
  busy = true; revision++; clearPoll(); error = '';
  try {
   if (ownedFlow) status = await cancelOpenAIBilling(provider.id, ownedFlow);
   ownedFlow = null; stopStream(); open = false;
  } catch { error = 'Could not cancel sign-in. Try closing again.'; }
  finally { busy = false; }
 }
 async function confirm() {
  if (!ready || !status?.flow_id || !status.observation_id || !status.organization_id) return;
  busy = true; error = ''; revision++; clearPoll();
  try {
   status = await confirmOpenAIBilling(provider.id, { flow_id: status.flow_id, observation_id: status.observation_id, organization_id: status.organization_id });
   ownedFlow = null; stopStream(); open = false;
  } catch {
   error = 'Could not confirm this organization. Check the latest balance and try again.';
   void load(true);
  } finally { busy = false; }
 }
 async function disconnect() {
  busy = true; error = ''; revision++; clearPoll();
  try { status = await disconnectOpenAIBilling(provider.id); disconnecting = false; }
  catch { error = 'Could not disconnect credit tracking. Try again.'; }
  finally { busy = false; }
 }
 $effect(() => {
  if (open && dialog && !dialog.open) dialog.showModal();
  else if (!open && dialog?.open) dialog.close();
 });
 onMount(() => {
  void load();
  return () => {
   disposed = true; revision++; clearPoll(); stopStream();
   if (ownedFlow) void cancelOpenAIBilling(provider.id, ownedFlow).catch(() => {});
  };
 });
</script>

<section class="credits" aria-label="OpenAI credits">
 <div class="heading">Credits</div>
 <p class="state">{status ? labels[status.status] : 'Loading credit tracking…'}</p>
 <p class="muted">Track your OpenAI Platform balance with a separate sign-in. Your API key stays unchanged.</p>
 {#if status?.organization_id && status.status !== 'ready'}<p class="organization">{status.organization_id}</p>{/if}
 {#if savedBalance !== null}
  <p class="state">{stale ? 'Last known balance: ' : ''}{currency.format(savedBalance)}{stale ? ' (stale)' : ' available'}</p>
  {#if status?.fetched_at != null}<p class="muted">Last checked <time datetime={new Date(status.fetched_at).toISOString()}>{new Date(status.fetched_at).toLocaleString()}</time></p>{/if}
 {/if}
 {#if status?.status === 'reconnect_required'}<p class="muted">Reconnect credits and sign in again to update your balance.</p>
 {:else if status?.status === 'unavailable'}<p class="muted">The latest balance could not be checked. Check status again, or reconnect credits if the problem continues.</p>{/if}
 {#if remote}
  <p class="muted">Manage this connection on installation <span class="organization">{status?.owner_instance_id}</span>. This installation cannot take it over.</p>
 {:else if status}
  {#if !status.browser_available}<p class="muted">Start the app’s browser in Settings → Browser, then check again to sign in.</p>{/if}
  <div class="actions">
   <button class="ed-btn" disabled={busy || !status.browser_available} onclick={() => bound || status?.status === 'reconnect_required' ? reconnecting = true : void begin()}>{bound || status.status === 'reconnect_required' ? 'Reconnect credits' : 'Sign in for credits'}</button>
   <button class="ed-btn" disabled={busy} onclick={() => void load()}>Check status</button>
   {#if bound || status.organization_id}<button class="ed-btn" disabled={busy} onclick={() => disconnecting = true}>Disconnect credits</button>{/if}
  </div>
 {:else if error}<button class="ed-btn" onclick={() => void load()}>Try again</button>{/if}
 {#if reconnecting}
  <div class="confirmation">
   <p>Reconnect credit tracking? Your current organization stays connected until you confirm a new organization. This uses the app’s shared browser sign-in.</p>
   <div class="actions"><button class="ed-btn primary" disabled={busy} onclick={begin}>Continue to sign-in</button><button class="ed-btn" disabled={busy} onclick={() => reconnecting = false}>Keep current connection</button></div>
  </div>
 {/if}
 {#if disconnecting}
  <div class="confirmation">
   <p>Disconnect credit tracking? The saved balance will be removed. Your API key and shared browser sign-in stay unchanged.</p>
   <div class="actions"><button class="ed-btn danger" disabled={busy} onclick={disconnect}>Disconnect credits</button><button class="ed-btn" disabled={busy} onclick={() => disconnecting = false}>Keep connected</button></div>
  </div>
 {/if}
 {#if error && !open}<p class="error" role="alert">{error}</p>{/if}
</section>

<dialog bind:this={dialog} oncancel={event => { event.preventDefault(); void close(); }} aria-labelledby="credits-title-{provider.id}">
 {#if open}
 <div class="dialog-content">
  <header>
   <h2 id="credits-title-{provider.id}">Sign in for OpenAI credits</h2>
   <div class="actions">
    <button class="ed-btn" disabled={!keyboardOpen && (!keyboardEnabled || !liveView)} aria-pressed={keyboardOpen}
     onpointerdown={event => event.preventDefault()}
     onclick={() => { if (keyboardOpen) liveView?.dismissKeyboard(); else liveView?.openKeyboard(); }}
    >{keyboardOpen ? 'Hide keyboard' : 'Show keyboard'}</button>
    <button class="ed-btn" disabled={busy} onclick={close}>Close</button>
   </div>
  </header>
  <p class="muted">This uses the app’s shared browser profile. Existing sign-ins may appear here. Complete sign-in and any verification in the browser below, then confirm the organization you want to track.</p>
  <p class="muted">Closing cancels an unconfirmed connection. Disconnecting credit tracking does not sign out of the shared browser.</p>
  {#if ownedFlow}
   <div class="browser" bind:this={browserElement}>
    {#if frame}<BrowserLiveView bind:this={liveView} bind:keyboardOpen {keyboardEnabled} {frame} interactive={keyboardEnabled} onInput={payload => stream?.send({ type: 'input', ...payload })} />
    {:else}<p class="browser-message" role="status">{streamStatus === 'browser_off' ? 'The app’s browser is not running. Start it in Settings → Browser.' : 'Connecting to the sign-in browser…'}</p>{/if}
   </div>
   {#if streamStatus === 'disconnected'}<p role="status" class="muted">Reconnecting to the browser…</p>{/if}
  {/if}
  {#if ready && status}
   <div class="confirmation">
    <p>Track credits for this organization?</p>
    <div class="review"><span class="organization">{status.organization_id}</span><strong>${status.credits!.balance_remaining.toFixed(2)} available</strong></div>
    {#if status.fetched_at}<p class="muted">Observed {new Date(status.fetched_at).toLocaleTimeString()}</p>{/if}
    <button class="ed-btn primary" disabled={busy} onclick={confirm}>Confirm organization</button>
   </div>
  {:else}<p role="status" class="state">{status ? labels[status.status] : 'Waiting for sign-in'}</p>{/if}
  {#if !ownedFlow && status?.status !== 'connected' && status?.status !== 'other_installation'}
   <p class="muted">This sign-in is no longer active. Close this dialog, then sign in for credits again.</p>
  {/if}
  {#if error}<p class="error" role="alert">{error}</p>{/if}
 </div>
 {/if}
</dialog>

<style>
 .credits { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border); font-size: 12px; }
 .heading { font-size: 11px; font-weight: 600; letter-spacing: .03em; text-transform: uppercase; color: var(--muted-foreground); }
 p { margin: 8px 0; line-height: 1.5; }
 .state { font-weight: 600; }
 .muted { color: var(--muted-foreground); }
 .organization, .review strong { font-family: var(--font-mono, monospace); overflow-wrap: anywhere; }
 .actions, .review { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
 .review { justify-content: space-between; margin-bottom: 10px; }
 .ed-btn { font-size: 12px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 7px; background: var(--background); color: var(--foreground); cursor: pointer; }
 .ed-btn.primary { background: var(--primary); color: var(--primary-foreground); border-color: var(--primary); }
 .ed-btn.danger { color: var(--destructive); }
 button:focus-visible { outline: 2px solid var(--ring); outline-offset: 3px; }
 button:disabled { opacity: .5; cursor: not-allowed; }
 .confirmation { padding: 12px; border: 1px solid var(--border); border-radius: 8px; margin-top: 12px; background: var(--muted); }
 .error { color: var(--destructive); }
 dialog { margin: auto; width: min(1000px, calc(100vw - 40px)); max-width: none; max-height: 94dvh; padding: 0; border: 1px solid var(--border); border-radius: 12px; background: var(--card); color: var(--foreground); }
 dialog::backdrop { background: rgb(0 0 0 / .6); }
 .dialog-content { padding: 18px; font-size: 12px; }
 header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
 h2 { font-size: 16px; font-weight: 600; margin: 0; }
 .browser { display: flex; flex-direction: column; height: clamp(260px, 48dvh, 560px); position: relative; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; margin-top: 12px; background: var(--background); }
 .browser-message { display: grid; place-items: center; height: 100%; padding: 24px; text-align: center; color: var(--muted-foreground); }
 @media (max-width: 640px) {
  dialog { width: 100vw; height: 100dvh; max-height: 100dvh; margin: 0; border: 0; border-radius: 0; }
  .dialog-content { padding: max(12px, env(safe-area-inset-top)) 12px max(16px, env(safe-area-inset-bottom)); }
  .browser { height: 50dvh; }
 }
</style>

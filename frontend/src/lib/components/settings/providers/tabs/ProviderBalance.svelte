<script lang="ts">
 import { onMount, tick } from 'svelte';
 import { getErrorMessage } from '$lib/api/client';
 import { balancePath, getProviderBalance, saveProviderBalance, removeProviderBalance, startPlatformSignIn, type ProviderBalance } from '$lib/api/provider-balances';
 import { wsUrl } from '$lib/api/runtime';
 import { getSettingsAccess } from '$lib/settings/access.svelte';
 import RowGroup from '../../rows/RowGroup.svelte';
 import SettingRow from '../../rows/SettingRow.svelte';
 import TextField from '../../rows/TextField.svelte';
 import ValueText from '../../rows/ValueText.svelte';

 let { providerId, openAI, onconfigured = () => {} }: { providerId: string; openAI: boolean; onconfigured?: (configured: boolean) => void } = $props();
 const access = getSettingsAccess();
 let status = $state<ProviderBalance | null>(null);
 let now = $state(Date.now());
 let key = $state('');
 let busy = $state(false);
 let error = $state('');
 const reading = $derived(!error && !status?.last_error_kind && !status?.needs_reauth && status?.status === 'fresh' && status.fetched_at && now - status.fetched_at <= 300_000 ? status.credits : null);
 let frame = $state('');
 let signingIn = $state(false);
 let signInButton = $state<HTMLButtonElement>();
 let signInScreen = $state<HTMLDivElement>();
 $effect(() => { if (signingIn && signInScreen) signInScreen.focus(); });
 let socket: WebSocket | null = null;
 let disposed = false;
 async function refresh() {
  const value = await getProviderBalance(providerId);
  if (disposed) return;
  now = Date.now();
  status = value;
  error = '';
  onconfigured(value.configured);
 }
 onMount(() => {
  refresh().catch((e) => { if (!disposed) error = getErrorMessage(e); });
  const clock = setInterval(() => { now = Date.now(); }, 10_000);
  return () => { disposed = true; clearInterval(clock); socket?.close(); key = ''; };
 });
 async function save(remove = false) {
  if (busy || access.readOnly) return;
  busy = true; error = '';
  const value = key;
  key = ''; // Never keep the write-only key after dispatch, even on failure.
  try {
   status = await (remove ? removeProviderBalance(providerId) : saveProviderBalance(providerId, value));
   onconfigured(status.configured);
  } catch (e) { error = getErrorMessage(e); }
  finally { busy = false; }
 }
 async function signIn() {
  if (access.readOnly || signingIn || busy) return;
  busy = true; error = '';
  try {
   const { ticket } = await startPlatformSignIn(providerId);
   if (disposed) return;
   signingIn = true;
   socket = new WebSocket(wsUrl(`/api/v1${balancePath(providerId)}/browser`));
   socket.onopen = () => socket?.send(JSON.stringify({ ticket }));
   socket.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === 'frame') frame = `data:image/jpeg;base64,${msg.data}`;
    else if (msg.type === 'saved') {
     closeSignIn();
     refresh().catch((e) => { error = getErrorMessage(e); });
    } else if (msg.type === 'error') { error = msg.message; closeSignIn(); }
   };
   socket.onerror = () => { error = 'Sign-in browser unavailable. Retry or paste a session key.'; closeSignIn(); };
   socket.onclose = () => { signingIn = false; frame = ''; };
  } catch (e) { error = getErrorMessage(e); }
  finally { busy = false; }
 }
 function closeSignIn() { socket?.close(); socket = null; signingIn = false; frame = ''; void tick().then(() => { if (!disposed) signInButton?.focus(); }); }
 function input(value: object) { if (!access.readOnly && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', ...value })); }
 function mouse(event: MouseEvent, kind: 'down' | 'up') {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
  input({ kind: 'mouse', event: kind, x: (event.clientX - rect.left) * 1000 / rect.width,
   y: (event.clientY - rect.top) * 750 / rect.height, button: 'left', clickCount: 1 });
 }
 function keyboard(event: KeyboardEvent, kind: 'down' | 'up') {
  // Keep the local browser's escape routes available to keyboard users.
  if (event.key === 'Tab') return;
  if (event.key === 'Escape') {
   if (kind === 'down') closeSignIn();
   return;
  }
  event.preventDefault();
  input({ kind: 'key', event: kind, key: event.key, code: event.code, keyCode: event.keyCode,
   text: kind === 'down' && event.key.length === 1 && !event.ctrlKey && !event.metaKey ? event.key : '',
   modifiers: (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0) });
 }
</script>

<RowGroup title={openAI ? 'OpenAI Platform balance (unofficial)' : 'xAI Management API balance'}>
 <SettingRow label="Remaining" hint={status?.source ?? 'Provider-reported prepaid credit'}>
  <ValueText value={reading ? `$${reading.balance_remaining.toFixed(2)}` : status?.configured ? 'Balance unavailable' : 'Not reported yet'} />
 </SettingRow>
 {#if status?.fetched_at}<p class="balance-note">Last fetched: {new Date(status.fetched_at).toLocaleString()} · {status.status}</p>{/if}
 {#if status?.needs_reauth}<p role="status">{openAI ? 'Session expired or refused. Sign in again to OpenAI Platform.' : 'Management access refused. Replace or re-save the management key.'}</p>
 {:else if status?.configured && !reading}<p role="status">Balance unavailable; no current reading. Refresh after polling completes.</p>{/if}
 {#if reading?.spend_month !== undefined}<SettingRow label="Cycle usage"><ValueText value={`$${reading.spend_month.toFixed(2)}`} /></SettingRow>{/if}
 {#if reading?.spending_limit !== undefined}<SettingRow label="Postpaid spending limit"><ValueText value={`$${reading.spending_limit.toFixed(2)}`} /></SettingRow>{/if}
 {#if openAI}
  <p class="balance-note">Unofficial integration. Sign in to your own OpenAI Platform account in the isolated app browser. The dashboard session is encrypted on this worker; it is separate from ChatGPT/Codex and may expire or stop working.</p>
  <button bind:this={signInButton} type="button" class="st-btn primary sm" disabled={busy || signingIn || access.readOnly} onclick={signIn}>Sign in to OpenAI Platform</button>
 {:else}<p class="balance-note">Create a management key in console.x.ai → Settings → Management Keys with billing read access. The normal API key resolves its team; the management key reads prepaid balance and invoice preview.</p>{/if}
 <SettingRow label={openAI ? 'Paste session key (fallback)' : 'Management key'} hint="Write-only · encrypted on this worker">
  <div class="access-controls">
   <TextField bind:value={key} type="password" label={openAI ? 'Platform session key' : 'xAI management key'} autocomplete="off" disabled={busy || signingIn} />
   <button type="button" class="st-btn primary sm" disabled={!key.trim() || busy || signingIn || access.readOnly} onclick={() => save()}>Save access</button>
   <button type="button" class="st-btn ghost sm" disabled={!status?.configured || busy || signingIn || access.readOnly} onclick={() => save(true)}>Disconnect balance</button>
  </div>
 </SettingRow>
 <button type="button" class="st-btn ghost sm" disabled={busy} onclick={() => refresh().catch((e) => { error = getErrorMessage(e); })}>Refresh balance status</button>
 {#if signingIn}
  <p class="balance-note">Click the sign-in screen to type. Tab returns to the app; Escape cancels sign-in. Closing this view destroys its cookies. If the provider refuses this browser, use the session-key fallback.</p>
  {#if frame}
   <div bind:this={signInScreen} class="signin-screen" role="textbox" aria-multiline="true" aria-label="OpenAI Platform sign-in browser" tabindex="0"
    onmousedown={(e) => mouse(e, 'down')} onmouseup={(e) => mouse(e, 'up')}
    onkeydown={(e) => keyboard(e, 'down')} onkeyup={(e) => keyboard(e, 'up')}
    onpaste={(e) => { e.preventDefault(); input({ kind: 'text', text: e.clipboardData?.getData('text') ?? '' }); }}
    onwheel={(e) => { e.preventDefault(); input({ kind: 'mouse', event: 'wheel', x: 500, y: 375, deltaX: e.deltaX, deltaY: e.deltaY }); }}>
    <img src={frame} alt="Private OpenAI Platform sign-in screen" draggable="false" />
   </div>
  {:else}<p role="status">Opening sign-in browser…</p>{/if}
  <button type="button" class="st-btn ghost sm" onclick={closeSignIn}>Cancel sign-in</button>
 {/if}
 {#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
</RowGroup>

<style>
 .balance-note { color: var(--st-mfg); font-size: 12px; margin: 8px 16px; overflow-wrap: anywhere; }
 .access-controls { display: flex; flex-wrap: wrap; gap: 8px; width: 100%; min-width: 0; }
 .access-controls :global(.st-txt) { flex: 1 1 100%; }
 @media (max-width: 600px), (pointer: coarse) {
  .st-btn, .access-controls :global(.st-txt) { min-height: 44px; }
 }
 .signin-screen { width: 100%; max-width: 1000px; aspect-ratio: 4 / 3; border: 1px solid var(--st-input); }
 .signin-screen:focus { outline: 2px solid var(--st-accent); }
 .signin-screen img { width: 100%; height: 100%; user-select: none; }
</style>

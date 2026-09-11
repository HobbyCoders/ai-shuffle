<script lang="ts">
    import { onMount, onDestroy } from 'svelte';
    import { api } from '$lib/api/client';
    let { connected = false, onConnected }: { connected?: boolean; onConnected: () => void } = $props();
    type Status = { configured: boolean; client_id?: string; environment?: string; country_codes?: string[]; redirect_uri?: string; suggested_redirect_uri: string };
    let status = $state<Status | null>(null);
    let error = $state('');
    let busy = $state(false);
    let setup = $state(false);
    let clientId = $state('');
    let secret = $state('');
    let environment = $state('sandbox');
    let countries = $state('US');
    let redirect = $state('');
    let popup: Window | null = null;
    let timer: ReturnType<typeof setInterval> | undefined;
    async function load() {
        try {
            status = await api.get<Status>('/connectors/oauth/plaid/status');
            clientId = status.client_id || ''; environment = status.environment || 'sandbox';
            countries = (status.country_codes || ['US']).join(',');
            redirect = status.redirect_uri || status.suggested_redirect_uri;
        } catch (e: any) { error = e.detail || 'Could not load Plaid settings.'; }
    }
    function stop() { if (timer) clearInterval(timer); timer = undefined; busy = false; }
    function receive(event: MessageEvent) {
        if (event.origin !== location.origin || event.source !== popup || event.data?.type !== 'plaid-connected') return;
        stop(); popup?.close(); popup = null; onConnected();
    }
    onMount(() => { void load(); window.addEventListener('message', receive); });
    onDestroy(() => { stop(); popup?.close(); window.removeEventListener('message', receive); });
    function connect() {
        error = '';
        popup = window.open('/plaid-link', '_blank', 'popup,width=560,height=780');
        if (!popup) { error = 'Allow pop-ups for this app, then try again.'; return; }
        busy = true;
        const deadline = Date.now() + 1800000;
        timer = setInterval(() => {
            if (popup?.closed || Date.now() > deadline) { stop(); error = 'Bank sign-in ended. If you did not finish, try again.'; }
        }, 1000);
    }
    async function save() {
        busy = true; error = '';
        try {
            await api.put('/connectors/oauth/plaid/app', { client_id: clientId, client_secret: secret, environment, country_codes: countries.split(',').map(s => s.trim().toUpperCase()).filter(Boolean), redirect_uri: redirect });
            secret = ''; setup = false; await load();
        } catch (e: any) { error = e.detail || 'Could not save Plaid settings.'; }
        finally { busy = false; }
    }
</script>

<section aria-label="Personal finance connection" class="space-y-3 rounded-lg border border-border p-4">
    <h3 class="font-semibold">Bank &amp; credit-card accounts</h3>
    <p class="text-sm">Connect your own accounts through Plaid Link. Agents can read accounts, balances, and purchases. No payments, transfers, full card numbers, or bank login credentials are exposed to agents.</p>
    {#if error}<p role="alert" class="text-sm text-destructive">{error}</p>{/if}
    {#if status?.configured}
        <p class="text-sm">Environment: <strong>{status.environment}</strong>{status.environment === 'sandbox' ? ' — test institutions and sample transactions only.' : ' — real financial accounts. Institution support varies and Plaid usage fees may apply.'}</p>
        {#if connected}
            <p class="text-sm">This connection is saved. To replace an expired authorization, add a new connection and remove the old one. Removing a connection here does not revoke consent at your bank; you can revoke access in your institution’s settings or Plaid Portal.</p>
        {:else}
            <button type="button" class="rounded bg-primary px-4 py-2 text-primary-foreground" disabled={busy} onclick={connect}>{busy ? 'Waiting for Plaid…' : 'Connect bank with Plaid'}</button>
        {/if}
    {:else if status}
        <p class="text-sm">One-time administrator setup is required: create a Plaid app with Transactions enabled. Production access and institution OAuth approval may be required. This is not Stripe merchant access.</p>
    {/if}
    <button type="button" class="text-sm text-primary" disabled={busy} aria-expanded={setup} onclick={() => setup = !setup}>{setup ? 'Hide app setup' : 'Plaid app setup'}</button>
    {#if setup}
        <div class="space-y-2">
            <label class="block text-sm">Client ID<input class="block w-full rounded border border-border bg-input p-2" bind:value={clientId} autocomplete="off" /></label>
            <label class="block text-sm">Secret<input class="block w-full rounded border border-border bg-input p-2" type="password" bind:value={secret} autocomplete="new-password" /></label>
            <label class="block text-sm">Environment<select class="block w-full rounded border border-border bg-input p-2" bind:value={environment}><option value="sandbox">Sandbox (sample data)</option><option value="production">Production (real accounts)</option></select></label>
            <label class="block text-sm">Country codes<input class="block w-full rounded border border-border bg-input p-2" bind:value={countries} placeholder="US,CA" /></label>
            <label class="block text-sm">OAuth return URI<input class="block w-full rounded border border-border bg-input p-2" bind:value={redirect} /></label>
            <p class="text-xs">Use this app’s HTTPS address ending in /plaid-link. Add the exact URI to the allowed redirect URIs in your Plaid dashboard. Secrets are encrypted on this installation.</p>
            <button type="button" class="rounded bg-primary px-4 py-2 text-primary-foreground" disabled={busy || !clientId || !secret || !redirect} onclick={save}>Save Plaid app</button>
        </div>
    {/if}
</section>

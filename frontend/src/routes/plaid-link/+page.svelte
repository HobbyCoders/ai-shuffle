<script lang="ts">
    import { onMount, onDestroy } from 'svelte';
    import { api } from '$lib/api/client';
    import { loadPlaid, type LinkHandler } from '$lib/utils/plaid-link';
    const key = 'shuffle-plaid-link';
    let error = $state('');
    let done = $state(false);
    let handler: LinkHandler | undefined;
    let destroyed = false;
    onDestroy(() => { destroyed = true; handler?.destroy(); });
    onMount(() => { void start(); });
    async function start() {
        try {
            // Plaid redirects this same window back after institution OAuth.
            // Only an ephemeral Link token is in tab storage, never an access token.
            const returning = new URLSearchParams(location.search).has('oauth_state_id');
            let flow: { flow_id: string; link_token: string };
            if (returning) {
                const saved = sessionStorage.getItem(key);
                if (!saved) throw new Error('This bank sign-in expired. Start again from Plugins.');
                flow = JSON.parse(saved);
            } else {
                sessionStorage.removeItem(key);
                flow = await api.post('/connectors/oauth/plaid/start', {});
                sessionStorage.setItem(key, JSON.stringify(flow));
            }
            await loadPlaid();
            if (destroyed) return;
            handler = window.Plaid!.create({
                token: flow.link_token,
                ...(returning ? { receivedRedirectUri: location.href } : {}),
                onSuccess: async (public_token) => {
                    sessionStorage.removeItem(key);
                    history.replaceState({}, '', '/plaid-link');
                    try {
                        const result = await api.post<{ connector_id: string }>('/connectors/oauth/plaid/exchange', { flow_id: flow.flow_id, public_token });
                        done = true;
                        window.opener?.postMessage({ type: 'plaid-connected', connector_id: result.connector_id }, location.origin);
                        if (window.opener) window.close();
                    } catch (e: any) { error = e.detail || 'Could not save the bank connection. Start again from Plugins.'; }
                },
                onExit: () => { sessionStorage.removeItem(key); error = 'Bank sign-in did not finish. No connection was saved. Start again from Plugins.'; }
            });
            handler.open();
        } catch (e: any) { sessionStorage.removeItem(key); error = e.detail || e.message || 'Could not open Plaid Link.'; }
    }
</script>
<svelte:head><title>Connect bank accounts — AI Shuffle</title><meta name="referrer" content="no-referrer" /></svelte:head>
<main class="mx-auto max-w-lg space-y-4 p-6">
    <h1 class="text-xl font-semibold">{done ? 'Bank accounts connected' : 'Connect bank accounts'}</h1>
    <p>Read-only access to accounts, balances, and transactions through Plaid. No payments or transfers.</p>
    {#if error}<p role="alert" class="text-destructive">{error}</p>{:else if !done}<p>Opening secure Plaid Link…</p>{/if}
    <a href="/" class="text-primary">Return to AI Shuffle</a>
</main>

<script lang="ts">
	import { onMount } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { Check, Copy } from 'lucide-svelte';

	type Connection = { authorization_url?: string | null; signed_in: boolean; status: string; code: string | null; qr_svg: string | null; error: string | null };
	let { workerName = 'My worker' }: { workerName?: string } = $props();
	let connection = $state<Connection | null>(null);
	// Linking a worker is available before it has an owner account. The auth
	// sign-in flag instead describes browser access to an already-linked worker.
	const loginAvailable = $derived(connection !== null && connection.status !== 'locked' && connection.status !== 'unavailable');
	let checking = $state(true);
	let busy = $state(false);
	let statusError = $state('');
	let actionError = $state('');
	let copied = $state(false);
	let loginInitiated = $state(false);
	let showPairingCode = $state(false);
	let stopped = false;
	let requestVersion = 0;
	let poll: ReturnType<typeof setTimeout> | undefined;

	function scheduleRefresh() {
		clearTimeout(poll);
		if (!stopped) poll = setTimeout(refresh, 4000);
	}

	async function refresh() {
		if (busy) { scheduleRefresh(); return; }
		const version = ++requestVersion;
		try {
			const next = await api.get<Connection>('/mobile/hub/connection');
			if (stopped || version !== requestVersion) return;
			connection = next;
			statusError = '';
			if (next.signed_in) { loginInitiated = false; showPairingCode = false; }
		} catch (e) {
			if (!stopped && version === requestVersion) statusError = getErrorMessage(e, 'Could not check account status');
		} finally {
			if (!stopped && version === requestVersion) { checking = false; scheduleRefresh(); }
		}
	}

	onMount(() => {
		void refresh();
		return () => { stopped = true; requestVersion++; clearTimeout(poll); };
	});

	async function connect() {
		if (busy || !loginAvailable || !connection || connection.signed_in) return;
		loginInitiated = true;
		actionError = ''; copied = false; showPairingCode = false;
		// Reopen an existing offer without creating a second one.
		if (connection.code) return;
		busy = true;
		requestVersion++;
		try {
			connection = await api.post<Connection>('/mobile/hub/connection', { name: workerName.trim().slice(0, 80) || 'My worker' });
			statusError = '';
		} catch (e) {
			actionError = getErrorMessage(e, 'Could not start log in');
		} finally { busy = false; scheduleRefresh(); }
	}

	async function copyCode() {
		if (!connection?.code) return;
		try { await navigator.clipboard.writeText(connection.code); copied = true; }
		catch { actionError = 'Select the code to copy it.'; }
	}

	async function disconnect() {
		if (busy) return;
		busy = true; actionError = ''; requestVersion++;
		try {
			const result = await api.delete<Connection>('/mobile/hub/connection');
			if (result) {
				connection = result;
				// A local logout can succeed while remote cleanup fails. Keep that warning
				// visible even if a later status response no longer includes it.
				actionError = result.error || '';
				statusError = '';
				loginInitiated = false; showPairingCode = false;
			}
		} catch (e) {
			actionError = getErrorMessage(e, 'Could not log out');
		} finally { busy = false; scheduleRefresh(); }
	}
</script>

<section class="account-connection" aria-label="Account connection">
	<div class="account-row">
		<div class="account-label">
			<h4>Account</h4>
			<span class="account-status" role="status">
				{#if connection}{connection.signed_in ? 'Logged in' : 'Not logged in'}
				{:else}{checking ? 'Checking account…' : 'Account status unavailable'}{/if}
			</span>
		</div>
		{#if connection?.signed_in}
			<button type="button" class="account-button" disabled={busy} onclick={disconnect}>{busy ? 'Logging out…' : 'Log out'}</button>
		{:else if connection && !(loginInitiated && connection.code)}
			<button type="button" class="account-button" disabled={busy || !loginAvailable} aria-describedby={!loginAvailable && connection.error ? 'account-connection-error' : undefined} onclick={connect}>{busy ? 'Preparing…' : 'Log in'}</button>
		{/if}
	</div>
	{#if loginAvailable && loginInitiated && connection?.code && !connection.signed_in}
		<div class="login-flow">
			{#if connection.authorization_url}
				<a class="account-button primary" href={connection.authorization_url} target="_blank" rel="noopener noreferrer">Continue to account</a>
				<p>Approve this worker, then return here.</p>
				<button type="button" class="account-button" aria-expanded={showPairingCode} onclick={() => showPairingCode = !showPairingCode}>{showPairingCode ? 'Hide phone code' : 'Use my phone instead'}</button>
			{/if}
			{#if showPairingCode || !connection.authorization_url}
				<p>In the Shuffle app, log in and choose <strong>Add worker</strong>.</p>
				{#if connection.qr_svg}<div class="qr" aria-label="Worker connection QR code">{@html connection.qr_svg}</div>{/if}
				<div class="code"><code>{connection.code.match(/.{1,4}/g)?.join(' ')}</code><button type="button" class="account-button" onclick={copyCode} aria-label="Copy connection code">{#if copied}<Check size={14} /> Copied{:else}<Copy size={14} /> Copy{/if}</button></div>
				<p>Scan or enter the code within ten minutes. Only approve a worker you own.</p>
			{/if}
			<button type="button" class="account-button cancel" disabled={busy} onclick={disconnect}>{busy ? 'Cancelling…' : 'Cancel log in'}</button>
		</div>
	{/if}
	{#if statusError}
		<div class="account-error"><p role="alert">{statusError}</p><button type="button" class="account-button" disabled={busy} onclick={refresh}>Retry account status</button></div>
	{/if}
	{#if actionError || connection?.error}<p id="account-connection-error" class="account-error" role="alert">{actionError || connection?.error}</p>{/if}
</section>

<style>
	.account-connection { padding: 12px 18px; border-bottom: 1px solid var(--border); color: var(--foreground); }
	.account-row, .account-label { display: flex; align-items: center; gap: 12px; }
	.account-row { justify-content: space-between; min-height: 40px; }
	.account-label { flex-wrap: wrap; gap: 4px 12px; min-width: 0; }
	h4 { margin: 0; font-family: var(--font-sans); font-size: 13px; font-weight: 600; }
	.account-status { font-size: 13px; color: var(--muted-foreground); }
	.account-button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 40px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; background: transparent; color: var(--foreground); font: inherit; font-size: 12px; line-height: 1.4; text-decoration: none; cursor: pointer; flex-shrink: 0; }
	.account-button:hover:not(:disabled) { background: var(--hover-overlay); }
	.account-button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.account-button:disabled { opacity: .55; cursor: default; }
	.account-button.primary { background: var(--primary); color: var(--primary-foreground); border-color: var(--primary); }
	.login-flow { padding: 8px 0 4px; }
	p { font-size: 12px; line-height: 1.5; margin: 10px 0; color: var(--muted-foreground); overflow-wrap: anywhere; }
	.qr { width: 180px; max-width: 100%; box-sizing: border-box; padding: 10px; background: white; border-radius: 8px; margin: 12px 0; }
	.qr :global(svg) { display: block; width: 100%; height: auto; }
	.code { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; }
	.code code { font-family: var(--font-mono); font-size: 17px; letter-spacing: .06em; user-select: all; overflow-wrap: anywhere; }
	.account-button.cancel { margin-top: 8px; }
	.account-error, .account-error p { color: var(--destructive); }
	@media (max-width: 480px) { .account-connection { padding: 10px 12px; } .account-label { flex-direction: column; align-items: flex-start; } .account-button { min-height: 44px; } }
</style>

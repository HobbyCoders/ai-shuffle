<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { api } from '$lib/api/client';
	let { name = '', config = {}, connectorId, onConnected }: { name?: string; config?: Record<string, any>; connectorId?: string; onConnected: () => void } = $props();
	type Status = { configured: boolean; client_id: string; redirect_uri: string; suggested_redirect_uri: string; environment_managed: boolean };
	let status = $state<Status | null>(null);
	let error = $state('');
	let busy = $state(false);
	let setup = $state(false);
	let clientId = $state('');
	let clientSecret = $state('');
	let redirectUri = $state('');
	let popup: Window | null = null;
	let flowId = '';
	let timer: ReturnType<typeof setInterval> | undefined;

	async function load() {
		try {
			status = await api.get<Status>('/connectors/oauth/google/status');
			clientId = status.client_id; redirectUri = status.redirect_uri || status.suggested_redirect_uri;
		} catch (e: any) { error = e.detail || 'Could not load Google sign-in settings.'; }
	}
	function stop() {
		if (timer) clearInterval(timer);
		timer = undefined; flowId = ''; busy = false;
	}
	function receive(event: MessageEvent) {
		if (event.origin !== window.location.origin || event.source !== popup || event.data?.type !== 'connector-oauth' || event.data.flow_id !== flowId) return;
		const result = event.data;
		stop(); popup?.close(); popup = null;
		if (result.error) error = result.error;
		else if (result.connector_id) onConnected();
	}
	onMount(() => { void load(); window.addEventListener('message', receive); });
	onDestroy(() => { window.removeEventListener('message', receive); stop(); popup?.close(); });
	async function connect() {
		error = '';
		popup = window.open('about:blank', '_blank', 'popup,width=560,height=740');
		if (!popup) { error = 'Allow pop-ups for this app, then try again.'; return; }
		busy = true;
		try {
			const result = await api.post<{ authorization_url: string; flow_id: string }>('/connectors/oauth/google/start', { name, connector_id: connectorId || null, options: Object.fromEntries(['labels', 'watch_inbox', 'enabled_action_categories', 'poll_interval_seconds', 'max_results'].filter(key => config[key] !== undefined && config[key] !== '').map(key => [key, config[key]])) });
			flowId = result.flow_id;
			popup.location.href = result.authorization_url;
			const deadline = Date.now() + 600_000;
			timer = setInterval(() => {
				if (popup?.closed || Date.now() >= deadline) { stop(); error = 'Sign-in ended. If you did not finish, try again.'; }
			}, 1000);
		} catch (e: any) { popup?.close(); stop(); error = e.detail || 'Could not start Google sign-in.'; }
	}
	async function saveApp() {
		busy = true; error = '';
		try {
			await api.put('/connectors/oauth/google/app', { client_id: clientId, client_secret: clientSecret, redirect_uri: redirectUri });
			clientSecret = ''; setup = false; await load();
		} catch (e: any) { error = e.detail || 'Could not save the Google app settings.'; }
		finally { busy = false; }
	}
</script>

<section class="google-signin" aria-label="Google account">
	{#if error}<p role="alert">{error}</p>{/if}
	{#if status?.configured}
		<p>Connect your own Gmail account—not a bot. Agents can use Gmail tools when you ask; connecting does not start email chats or auto-replies. Google will ask you to allow reading, composing, sending, and organizing email. AI Shuffle keeps your account credentials encrypted on this installation.</p>
		<button class="connect" disabled={busy} onclick={connect}>{busy ? 'Waiting for Google…' : connectorId ? 'Reconnect with Google' : 'Connect with Google'}</button>
	{:else if status}
		<p>Google sign-in needs a one-time administrator setup for this installation. After that, accounts connect through Google without copying tokens.</p>
	{:else}<p role="status">Loading Google sign-in…</p>{/if}
	{#if status && !status.environment_managed}
		<button class="settings" aria-expanded={setup} disabled={busy} onclick={() => setup = !setup}>{setup ? 'Hide Google app setup' : 'Google app setup (administrator)'}</button>
		{#if setup}
			<p>Create a Web application OAuth client in <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener noreferrer">Google Cloud</a>. Enable the Gmail API and configure the consent screen. During testing, add the Google accounts that may connect. Public distribution requires Google's review of the requested Gmail permission.</p>
			<label>Google client ID<input autocomplete="off" bind:value={clientId} /></label>
			<label>Google client secret<input type="password" autocomplete="new-password" bind:value={clientSecret} /></label>
			<label>Authorized redirect URI<input readonly value={redirectUri} /></label>
			<p>Register this exact URI with Google. Use localhost or HTTPS. Saving replaces the Google app settings for this installation; existing connected accounts retain their credentials.</p>
			<button disabled={busy || !clientId || !clientSecret} onclick={saveApp}>Save Google app settings</button>
		{/if}
	{:else if status?.environment_managed}<p>Google app settings are managed by this host's environment.</p>{/if}
</section>

<style>
	.google-signin { display: grid; gap: .75rem; font-size: .875rem; }
	p { color: var(--muted-foreground); line-height: 1.5; }
	[role='alert'] { color: var(--destructive); }
	button { border: 1px solid var(--border); border-radius: .5rem; padding: .6rem .85rem; justify-self: start; }
	.connect { background: var(--primary); color: var(--primary-foreground); }
	.settings { font-size: .8rem; }
	button:disabled { opacity: .5; }
	label { display: grid; gap: .3rem; }
	input { min-width: 0; width: 100%; padding: .5rem; border: 1px solid var(--border); border-radius: .35rem; background: var(--input); }
	a { text-decoration: underline; }
</style>

<script lang="ts">
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount, untrack } from 'svelte';
	let { sessionId, request, savedConnections, vaultAvailable, onComplete }: { sessionId: string; request: { id: string; kind: string; purpose: string }; savedConnections: {id: string; label: string; target: string; fingerprint: string}[]; vaultAvailable: boolean; onComplete: () => void } = $props();
	let secureContext = $state(false);
	onMount(() => { secureContext = window.isSecureContext; });
	let savedId = $state('');
	let saveConnection = $state(false);
	let label = $state('Temporary access');
	let minutes = $state(60);
	let path = $state('');
	let writable = $state(false);
	let host = $state('');
	let port = $state(22);
	let username = $state('');
	let fingerprint = $state('');
	let password = $state('');
	let privateKey = $state('');
	let passphrase = $state('');
	let acknowledged = $state(false);
	let busy = $state(false);
	let error = $state('');
	const endpoint = untrack(() => `/chat-access/${encodeURIComponent(sessionId)}/requests/${encodeURIComponent(request.id)}`);
	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (!acknowledged || busy || !secureContext) return;
		busy = true; error = '';
		const connection = {kind: request.kind, label, minutes, path, writable, host, port, username, fingerprint, password, private_key: privateKey, passphrase};
		const body = {connection: savedId ? null : connection, saved_id: savedId, minutes, save_connection: !savedId && saveConnection};
		// No transcript, localStorage, sessionStorage, analytics, or generic question API.
		password = ''; privateKey = ''; passphrase = '';
		try { await api.post(endpoint, body); onComplete(); }
		catch (e) { error = getErrorMessage(e, 'Private access could not be granted.'); }
		finally { connection.password = ''; connection.private_key = ''; connection.passphrase = ''; busy = false; }
	}
	async function decline() {
		busy = true; password = ''; privateKey = ''; passphrase = ''; error = '';
		try { await api.delete(endpoint); onComplete(); }
		catch (e) { error = getErrorMessage(e, 'Could not decline the request.'); }
		finally { busy = false; }
	}
</script>

<section class="private-card" aria-label="Private access request">
	<header><span aria-hidden="true">🔒</span><h3>Private access request</h3><span class="badge">Not sent to the model</span></header>
	<p class="purpose">{request.purpose}</p>
	<p class="muted">These inputs go directly to this worker, not into chat history. The model receives only a temporary access handle. Session access expires or ends on worker restart. Connection details are retained only if you choose to save them encrypted.</p>
	{#if error}<p class="error" role="alert">{error}</p>{/if}
	{#if !secureContext}<p class="error" role="alert">Open the desktop app or an HTTPS connection before entering private details.</p>{/if}
	<form onsubmit={submit} autocomplete="off">
		{#if request.kind === 'machine' && savedConnections.length}
			<label>Connection<select bind:value={savedId}><option value="">Enter new private details</option>{#each savedConnections as saved}<option value={saved.id}>{saved.label}</option>{/each}</select></label>
		{/if}
		{#if savedId}
			<p>{savedConnections.find(s => s.id === savedId)?.target}</p>
			<p class="muted">Pinned host key: {savedConnections.find(s => s.id === savedId)?.fingerprint}</p>
			<label>Expires in (minutes)<input required type="number" min="1" max="480" bind:value={minutes} /></label>
			<p class="warning">Approve new session access using the saved credentials. This account can access its server’s files and network. Commands still require approval.</p>
		{:else}
		<div class="grid">
			<label>Resource name<input required maxlength="80" bind:value={label} /></label>
			<label>Expires in (minutes)<input required type="number" min="1" max="480" bind:value={minutes} /></label>
		</div>
		{#if request.kind === 'folder'}
			<label>Folder on this worker<input required placeholder="/home/me/documents" bind:value={path} /></label>
			<label class="check"><input type="checkbox" bind:checked={writable} />Allow text writes, with approval for each write</label>
			<p class="muted">Linux file confinement only. No shell, links, mount traversal, or files larger than 64 KiB.</p>
		{:else}
			<div class="grid">
				<label>Server IP address<input required placeholder="10.0.0.200" bind:value={host} /></label>
				<label>SSH port<input required type="number" min="1" max="65535" bind:value={port} /></label>
				<label>Username<input required autocomplete="off" bind:value={username} /></label>
				<label>Password<input type="password" autocomplete="new-password" bind:value={password} /></label>
			</div>
			<label>SSH host-key fingerprint<input required placeholder="SHA256:…" spellcheck="false" bind:value={fingerprint} /></label>
			<p class="muted">Obtain the fingerprint from the server console or another trusted source. It prevents connecting to an impersonated server.</p>
			<details><summary>Use a private key instead of a password</summary>
				<label>Private key<textarea rows="3" autocomplete="off" spellcheck="false" bind:value={privateKey}></textarea></label>
				<label>Key passphrase<input type="password" autocomplete="new-password" bind:value={passphrase} /></label>
			</details>
			<p class="warning">Machine access uses this account’s authority, including its files and network. It is not a sandbox on the server. Each command still needs your approval.</p>
		{/if}
		{#if request.kind === 'machine'}
			<label class="check"><input type="checkbox" disabled={!vaultAvailable} bind:checked={saveConnection} />Save this connection encrypted on this worker for future requests</label>
			{#if !vaultAvailable}<p class="muted">Secure storage is locked or unavailable. Temporary access still works.</p>{/if}
		{/if}
		{/if}
		<label class="check"><input required type="checkbox" bind:checked={acknowledged} />I approve this temporary access for this conversation.</label>
		<div class="buttons"><button type="button" disabled={busy} onclick={decline}>Decline</button><button class="primary" type="submit" disabled={busy || !acknowledged || !secureContext}>{busy ? 'Connecting…' : 'Connect privately'}</button></div>
	</form>
</section>

<style>
	.private-card { min-width: 0; max-height: calc(100dvh - 230px); overflow: auto; overscroll-behavior: contain; padding: 1rem; border: 1px solid var(--border); border-radius: 12px; background: var(--card); color: var(--foreground); }
	header { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }
	h3 { margin: 0; font-size: 1rem; }
	.badge { font-size: .7rem; color: var(--muted-foreground); }
	p { margin: .65rem 0; font-size: .85rem; line-height: 1.5; overflow-wrap: anywhere; }
	.purpose { white-space: pre-wrap; }
	.muted { color: var(--muted-foreground); }
	.warning { border-left: 2px solid var(--border); padding-left: .6rem; }
	.error { color: var(--destructive); }
	form, label { display: flex; flex-direction: column; gap: .4rem; }
	form { gap: .8rem; }
	label { min-width: 0; font-size: .8rem; }
	.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 190px), 1fr)); gap: .8rem; }
	input:not([type=checkbox]), textarea, select { width: 100%; min-width: 0; box-sizing: border-box; padding: .5rem; color: var(--foreground); background: var(--background); border: 1px solid var(--border); border-radius: 6px; font: inherit; }
	textarea { resize: vertical; }
	.check { flex-direction: row; align-items: flex-start; }
	.check input { margin-top: .2rem; flex-shrink: 0; }
	.buttons { display: flex; justify-content: flex-end; gap: .5rem; flex-wrap: wrap; }
	button { font: inherit; padding: .5rem .75rem; border: 1px solid var(--border); border-radius: 6px; cursor: pointer; background: var(--background); color: var(--foreground); }
	.primary { background: var(--primary); color: var(--primary-foreground); }
	button:disabled { opacity: .5; cursor: default; }
	summary { cursor: pointer; font-size: .8rem; }
</style>

<script lang="ts">
	import '../deck/cards/card-design-system.css';
	import './vault.css';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount, untrack } from 'svelte';
	import ServiceAccessRequest from './ServiceAccessRequest.svelte';
	import PluginServiceAccess from './PluginServiceAccess.svelte';
	import VaultSignInPicker from './VaultSignInPicker.svelte';
	import VaultDuration from './VaultDuration.svelte';
	type Saved = {id: string; label: string; target: string; fingerprint: string; kind?: string; writable?: boolean};
	let { sessionId, request, savedConnections, vaultAvailable, passwordSignIn = false, owner = null, clearSecretsKey = 0, onComplete }: { sessionId: string; request: { id: string; kind: string; purpose: string; expires_at: number; saved_id?: string; suggested_path?: string; suggested_host?: string; suggested_port?: number; suggested_username?: string; service_provider?: string; suggested_url?: string; plugin?: string; service?: string }; savedConnections: Saved[]; vaultAvailable: boolean; passwordSignIn?: boolean; owner?: string | null; clearSecretsKey?: number; onComplete: () => void } = $props();
	// A mirrored conversation's access lives on the device that runs it; this worker only relays.
	const where = $derived(owner ?? 'this worker');
	const sentTo = $derived(owner ? `Sent only to ${owner} through this worker` : 'Sent only to this worker');
	const machine = untrack(() => request.kind === 'machine');
	const service = untrack(() => request.kind === 'service');
	// HTTPS setup lives in ServiceAccessRequest; these fields are SSH/folder only.
	// Entries from older workers have no kind and are SSH.
	const savable = machine;
	const savedOptions = $derived(savable ? savedConnections.filter(s => (s.kind ?? 'machine') === request.kind) : []);
	let secureContext = $state(false);
	let now = $state(Date.now());
	onMount(() => {
		secureContext = window.isSecureContext;
		const tick = setInterval(() => { now = Date.now(); }, 1000);
		return () => clearInterval(tick);
	});
	// An explicit saved ID or structured hints can preselect a saved machine.
	// Never infer a target from purpose or fall back to an unrelated entry.
	const normalizeHost = (value: string) => {
		const host = value.toLowerCase().replace(/\.$/, '');
		if (!host.includes(':')) return host;
		try { return new URL(`http://[${host}]`).hostname; }
		catch { return host; }
	};
	/** Whether a saved machine is the one the request's structured hints name. */
	function matchesHints(entry: Saved): boolean {
		if (!machine || !request.suggested_host || (entry.kind ?? 'machine') !== 'machine') return false;
		// The catalogue formats targets as username@host:port, including IPv6.
		const target = /^([^@]+)@(.+):([0-9]+)$/.exec(entry.target);
		return !!target && normalizeHost(target[2]) === normalizeHost(request.suggested_host)
			&& (request.suggested_port === undefined || Number(target[3]) === request.suggested_port)
			&& (!request.suggested_username || target[1] === request.suggested_username);
	}
	/** The sign-in the hints chose, until the owner chooses for themselves. */
	let hinted = untrack(() => request.saved_id ? '' : savedConnections.find(matchesHints)?.id ?? '');
	let savedId = $state(untrack(() => request.saved_id
		? savedConnections.find(s => s.id === request.saved_id && (s.kind ?? 'machine') === request.kind)?.id ?? ''
		: hinted));
	// The Vault can change while the card is open (Settings, another tab). A
	// sign-in the owner did not pick stays chosen only while it is still the one
	// the request names; a removed one is never sent. Either way the owner
	// chooses again.
	$effect(() => {
		if (savedId !== hinted) hinted = '';
		if (!savedId) return;
		const entry = savedOptions.find(s => s.id === savedId);
		if (!entry || (savedId === hinted && !matchesHints(entry))) { hinted = ''; savedId = ''; }
	});
	let saveConnection = $state(false);
	let label = $state(machine ? 'SSH server' : 'Folder');
	let minutes = $state(60);
	let path = $state(untrack(() => request.kind === 'folder' ? request.suggested_path ?? '' : ''));
	let writable = $state(false);
	let host = $state(untrack(() => machine ? request.suggested_host ?? '' : ''));
	let port = $state(untrack(() => machine ? request.suggested_port ?? 22 : 22));
	let username = $state(untrack(() => machine ? request.suggested_username ?? '' : ''));
	let fingerprint = $state('');
	let password = $state('');
	let privateKey = $state('');
	let passphrase = $state('');
	let useKey = $state(false);
	/** The private-key and host-key fields, opened on request. */
	let more = $state(false);
	const moreOpen = $derived(more || useKey || !!fingerprint.trim());
	// Closing the Vault panel bumps clearSecretsKey: drop the typed
	// password, key and passphrase (the connection details stay for a retry).
	let previousClearKey = untrack(() => clearSecretsKey);
	$effect(() => {
		if (previousClearKey !== clearSecretsKey) { previousClearKey = clearSecretsKey; password = ''; privateKey = ''; passphrase = ''; }
	});
	// A typed secret is for the sign-in being typed: choosing a saved one drops it.
	$effect(() => { void savedId; untrack(() => { password = ''; privateKey = ''; passphrase = ''; }); });
	let busy = $state(false);
	let error = $state('');
	const remaining = $derived(Math.max(0, Math.floor(request.expires_at - now / 1000)));
	const expired = $derived(remaining <= 0);
	const clock = $derived(`${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`);
	const saved = $derived(savedOptions.find(s => s.id === savedId));
	const endpoint = untrack(() => `/vault/sessions/${encodeURIComponent(sessionId)}/requests/${encodeURIComponent(request.id)}`);
	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (busy || expired || !secureContext) return;
		busy = true; error = '';
		const connection: Record<string, unknown> = {kind: request.kind, label, minutes, path, writable, host: host.trim(), port, username: username.trim(),
				fingerprint: fingerprint.trim(), password: useKey ? '' : password,
				private_key: useKey ? privateKey : '', passphrase: useKey ? passphrase : ''};
		const body = {connection: savedId ? null : connection, saved_id: savedId, minutes, save_connection: !savedId && saveConnection};
		// No transcript, localStorage, sessionStorage, analytics, or generic question API.
		password = ''; privateKey = ''; passphrase = '';
		try { await api.post(endpoint, body); onComplete(); }
		catch (e) { error = getErrorMessage(e, 'Access could not be granted.'); }
		finally {
			for (const name of ['password', 'private_key', 'passphrase']) if (name in connection) connection[name] = '';
			busy = false;
		}
	}
	async function decline() {
		busy = true; password = ''; privateKey = ''; passphrase = ''; error = '';
		try { await api.delete(endpoint); onComplete(); }
		catch (e) { error = getErrorMessage(e, 'Could not decline the request.'); }
		finally { busy = false; }
	}
</script>

{#snippet chevron()}
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7" /></svg>
{/snippet}

{#if service && request.plugin && request.service}
	<PluginServiceAccess {sessionId} request={{ ...request, plugin: request.plugin, service: request.service }} {owner} {onComplete} />
{:else if service}
	<ServiceAccessRequest {sessionId} {request} {savedConnections} {vaultAvailable} {passwordSignIn} {owner} {clearSecretsKey} {onComplete} />
{:else}
<section class="card-system pa-card" aria-label="Vault request">
	<header class="pa-head">
		<div class="pa-icon" aria-hidden="true">
			{#if machine}
				<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="7" rx="2" /><rect x="3" y="13" width="18" height="7" rx="2" /><path stroke-linecap="round" d="M7 7.5h.01M7 16.5h.01" /></svg>
			{:else}
				<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linejoin="round" d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" /></svg>
			{/if}
		</div>
		<div class="pa-title">
			<h3><span>{machine ? 'Connect to a machine' : 'Share a folder'}</span></h3>
			<!-- What the agent said it needs this for, in its own words. -->
			<p class="pa-subtitle pa-quoted" title={request.purpose}>{request.purpose}</p>
		</div>
		<span class="pa-timer" class:urgent={remaining < 60} role="timer" aria-label="Time left to respond">{expired ? 'Expired' : clock}</span>
	</header>

	<form onsubmit={submit} autocomplete="off">
		<div class="pa-body">
			{#if error}<p class="pa-error" role="alert">{error}</p>{/if}
			{#if !secureContext}<p class="pa-error" role="alert">Open the desktop app or an HTTPS connection before entering private details.</p>{/if}

			{#if savedOptions.length}
				<VaultSignInPicker options={savedOptions} bind:value={savedId} disabled={busy} />
			{/if}

			{#if saved}
				<!-- Nothing to type: the picker shows the address and the host key. -->
			{:else if machine}
				<div class="pa-grid host">
					<label class="pa-field"><span>Address</span><input class="card-form-input" required maxlength="253" placeholder="IP or hostname" spellcheck="false" bind:value={host} /></label>
					<label class="pa-field"><span>Port</span><input class="card-form-input" required type="number" min="1" max="65535" bind:value={port} /></label>
				</div>
				<div class="pa-grid">
					<label class="pa-field"><span>Username</span><input class="card-form-input" required autocomplete="off" spellcheck="false" bind:value={username} /></label>
					{#if !useKey}
						<label class="pa-field"><span>Password</span><input class="card-form-input" type="password" required autocomplete="new-password" bind:value={password} /></label>
					{/if}
				</div>
				<!-- Stays open while a key or a fingerprint is in use, so nothing that
				     will be sent is ever hidden. -->
				<button type="button" class="pa-toggle" aria-expanded={moreOpen} onclick={() => more = !more}>{@render chevron()}Use a private key or check the host key</button>
				{#if moreOpen}
					<div class="pa-reveal">
						<label class="pa-option"><input type="checkbox" bind:checked={useKey} onchange={() => { password = ''; privateKey = ''; passphrase = ''; }} /><strong>Sign in with a private key</strong><small>Instead of a password.</small></label>
						{#if useKey}
							<label class="pa-field"><span>Private key</span><textarea class="card-form-input pa-secret-area" rows="3" required autocomplete="off" spellcheck="false" bind:value={privateKey}></textarea></label>
							<label class="pa-field"><span>Key passphrase</span><input class="card-form-input" type="password" autocomplete="new-password" bind:value={passphrase} /></label>
						{/if}
						<label class="pa-field"><span>Host-key fingerprint</span><input class="card-form-input card-form-input--mono" placeholder="SHA256:…" spellcheck="false" bind:value={fingerprint} /></label>
						<p class="pa-hint">Optional. Any key type the server offers is accepted. When left blank, the key the server presents now is pinned for this access and shown once connected.</p>
					</div>
				{/if}
			{:else}
				<label class="pa-field"><span>Folder on {where}</span><input class="card-form-input card-form-input--mono" required placeholder="/home/me/documents" spellcheck="false" bind:value={path} /></label>
				<p class="pa-hint">Review or change the proposed path before granting access. The folder must already exist on {where}. For Docker, use its mounted container path, not an unmounted host path.</p>
				<label class="pa-option"><input type="checkbox" bind:checked={writable} /><strong>Allow text writes</strong><small>Writes follow this session’s permission mode; bypass skips repeat approvals.</small></label>
			{/if}

			{#if !saved}
				<label class="pa-field"><span>Name</span><input class="card-form-input" required maxlength="80" bind:value={label} /></label>
				{#if savable}
					<label class="pa-option" class:disabled={!vaultAvailable}>
						<input type="checkbox" disabled={!vaultAvailable} bind:checked={saveConnection} />
						<strong>Save to Vault under this name</strong>
						<small>{vaultAvailable ? `Encrypted on ${where}. Every use still asks you first.` : 'The Vault is locked or unavailable. Access for this chat still works.'}</small>
					</label>
				{/if}
			{/if}
		</div>

		<div class="pa-section">
			<VaultDuration bind:minutes disabled={busy} />
			<p class="pa-promise">
				{#if machine}<strong>The agent never sees the password or key.</strong> {saved ? `It stays encrypted on ${where}.` : `${sentTo}, never to the model or chat history.`} Commands run with this account’s authority and follow this session’s permission mode; bypass skips repeat approvals.
				{:else}<strong>The agent reaches this folder only.</strong> {sentTo}. Kernel file confinement: no shell, links or mount traversal, and files up to 64 KiB.{/if}
			</p>
			<div class="pa-actions">
				<button type="button" class="card-btn-secondary" disabled={busy} onclick={decline}>Decline</button>
				<button type="submit" class="card-btn-primary" disabled={busy || expired || !secureContext}>{busy ? 'Connecting…' : machine ? 'Connect' : 'Grant access'}</button>
			</div>
		</div>
	</form>
</section>
{/if}

<style>
	form { display: contents; }
	/* A pasted key is as private as a password: masked where the engine can. */
	.pa-secret-area { -webkit-text-security: disc; }
</style>

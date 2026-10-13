<script lang="ts" module>
	export type SavedConnection = {
		id: string; label: string; target: string; fingerprint: string; kind?: string; writable?: boolean;
		host?: string; port?: number; username?: string; uses_key?: boolean;
		auth_header?: string; auth_scheme?: string;
	};
	export type EditResult = { updated: boolean; revoked: number; host_key_cleared: boolean };
</script>

<script lang="ts">
	/**
	 * Inline edit form for one saved private-access connection, shown inside
	 * its row of the Private access panel (never a nested dialog).
	 *
	 * Stored secrets never reach this form: secret fields start empty and say
	 * "Unchanged". A blank secret keeps the stored one; a typed one replaces
	 * it. Only changed fields are sent, and typed secrets are cleared after
	 * every submit and whenever the panel closes (clearSecretsKey).
	 */
	import './private-access.css';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount, untrack } from 'svelte';

	let { endpoint, saved, owner = null, clearSecretsKey = 0, onSaved, onCancel }: {
		endpoint: string; saved: SavedConnection; owner?: string | null; clearSecretsKey?: number;
		onSaved: (result: EditResult, label: string) => void; onCancel: () => void;
	} = $props();

	const service = untrack(() => saved.kind === 'service');
	// Older catalogues carry only `username@host:port`.
	const parsed = untrack(() => /^([^@]+)@(.+):([0-9]+)$/.exec(saved.target));
	const initial = untrack(() => ({
		label: saved.label,
		host: saved.host ?? parsed?.[2] ?? '',
		port: saved.port ?? Number(parsed?.[3] ?? 22),
		username: saved.username ?? parsed?.[1] ?? '',
		url: saved.target,
		authHeader: saved.auth_header ?? 'Authorization',
		authScheme: saved.auth_scheme ?? 'Bearer',
		writable: !!saved.writable,
		usesKey: !!saved.uses_key
	}));
	let label = $state(initial.label);
	let host = $state(initial.host);
	let port = $state<number>(initial.port);
	let username = $state(initial.username);
	let url = $state(initial.url);
	let authHeader = $state(initial.authHeader);
	let authScheme = $state(initial.authScheme);
	let writable = $state(initial.writable);
	let useKey = $state(initial.usesKey);
	let fingerprint = $state('');
	// Secrets: always start empty; the stored values never come back.
	let password = $state('');
	let privateKey = $state('');
	let passphrase = $state('');
	let secret = $state('');
	let busy = $state(false);
	let error = $state('');
	let secureContext = $state(false);
	let nameInput = $state<HTMLInputElement | null>(null);
	onMount(() => { secureContext = window.isSecureContext; nameInput?.focus({ preventScroll: true }); nameInput?.scrollIntoView?.({ block: 'nearest' }); });
	function clearSecrets() { password = ''; privateKey = ''; passphrase = ''; secret = ''; }
	let lastClearKey = untrack(() => clearSecretsKey);
	$effect(() => {
		const key = clearSecretsKey;
		untrack(() => { if (key !== lastClearKey) { lastClearKey = key; clearSecrets(); } });
	});

	const where = $derived(owner ?? 'this worker');
	const addressChanged = $derived(service
		? url.trim() !== initial.url
		: host.trim() !== initial.host || Number(port) !== initial.port);
	// Switching sign-in method needs the new secret; keeping it may leave it blank.
	const needsPassword = $derived(!service && !useKey && initial.usesKey);
	const needsKey = $derived(!service && useKey && !initial.usesKey);
	const uid = $props.id();

	function changes(): Record<string, unknown> {
		const body: Record<string, unknown> = {};
		if (label.trim() !== initial.label) body.label = label.trim();
		if (fingerprint.trim()) body.fingerprint = fingerprint.trim();
		if (service) {
			if (url.trim() !== initial.url) body.url = url.trim();
			if (authHeader.trim() !== initial.authHeader) body.auth_header = authHeader.trim();
			if (authScheme.trim() !== initial.authScheme) body.auth_scheme = authScheme.trim();
			if (writable !== initial.writable) body.writable = writable;
			if (secret) body.secret = secret;
		} else {
			if (host.trim() !== initial.host) body.host = host.trim();
			if (Number(port) !== initial.port) body.port = Number(port);
			if (username.trim() !== initial.username) body.username = username.trim();
			if (useKey) {
				if (privateKey) body.private_key = privateKey;
				if (passphrase) body.passphrase = passphrase;
			} else if (password) {
				body.password = password;
			}
		}
		return body;
	}

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (busy || !secureContext) return;
		const body = changes();
		if (!Object.keys(body).length) { clearSecrets(); onCancel(); return; }
		busy = true; error = '';
		// Held only for this request; never stored, logged or shown again.
		clearSecrets();
		try {
			const result = await api.patch<EditResult>(`${endpoint}/${encodeURIComponent(saved.id)}`, body);
			onSaved(result, label.trim() || initial.label);
		} catch (e) {
			error = getErrorMessage(e, 'Could not save the changes.');
		} finally {
			for (const name of ['password', 'private_key', 'passphrase', 'secret']) if (name in body) body[name] = '';
			busy = false;
		}
	}
	function cancel() { clearSecrets(); onCancel(); }
</script>

{#snippet chevron()}
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7" /></svg>
{/snippet}

<form class="pa-edit" onsubmit={submit} autocomplete="off" aria-label="Edit {saved.label}">
	{#if !secureContext}<p class="pa-error" role="alert">Open the desktop app or an HTTPS connection before editing private details.</p>{/if}
	{#if error}<p class="pa-error" role="alert">{error}</p>{/if}
	<label class="pa-field"><span>Name</span><input class="card-form-input" required maxlength="80" bind:this={nameInput} bind:value={label} /></label>
	{#if service}
		<label class="pa-field"><span>Service address</span><input class="card-form-input card-form-input--mono" type="url" required placeholder="https://api.example.com/v1" spellcheck="false" bind:value={url} /></label>
		<div class="pa-grid">
			<label class="pa-field"><span>Credential header</span><input class="card-form-input" required maxlength="64" spellcheck="false" bind:value={authHeader} /></label>
			<label class="pa-field"><span>Scheme</span><input class="card-form-input" maxlength="32" placeholder="None" spellcheck="false" bind:value={authScheme} /></label>
		</div>
		<label class="pa-field"><span>API key or token</span><input class="card-form-input" disabled={!secureContext} type="password" minlength="8" placeholder="Unchanged" autocomplete="new-password" spellcheck="false" aria-describedby="{uid}-secret" bind:value={secret} /></label>
		<p class="pa-hint" id="{uid}-secret">Leave blank to keep the saved token. A new one replaces it.</p>
		<label class="pa-field"><span>Certificate SHA-256 fingerprint</span><input class="card-form-input card-form-input--mono" maxlength="95" pattern={'[0-9A-Fa-f]{64}|([0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}'} placeholder={saved.fingerprint && !addressChanged ? 'Unchanged' : 'Optional, for a self-signed server'} spellcheck="false" bind:value={fingerprint} /></label>
		{#if addressChanged && saved.fingerprint && !fingerprint.trim()}<p class="pa-hint" role="status">A new address clears the trusted certificate. Enter its fingerprint here if the server is self-signed.</p>{/if}
		<label class="pa-option"><input type="checkbox" bind:checked={writable} /><strong>Allow changes</strong><small>POST, PUT, PATCH and DELETE follow the session’s permission mode.</small></label>
	{:else}
		<div class="pa-grid host">
			<label class="pa-field"><span>Server IP address</span><input class="card-form-input" required inputmode="decimal" spellcheck="false" bind:value={host} /></label>
			<label class="pa-field"><span>Port</span><input class="card-form-input" required type="number" min="1" max="65535" bind:value={port} /></label>
		</div>
		<label class="pa-field"><span>Username</span><input class="card-form-input" required autocomplete="off" spellcheck="false" bind:value={username} /></label>
		{#if !useKey}
			<label class="pa-field"><span>Password</span><input class="card-form-input" disabled={!secureContext} type="password" required={needsPassword} placeholder={needsPassword ? 'Required to switch to a password' : 'Unchanged'} autocomplete="new-password" bind:value={password} /></label>
		{/if}
		<button type="button" class="pa-toggle" aria-expanded={useKey} onclick={() => { useKey = !useKey; password = ''; privateKey = ''; passphrase = ''; }}>{@render chevron()}{initial.usesKey ? 'Sign in with a private key' : 'Use a private key instead of a password'}</button>
		{#if useKey}
			<div class="pa-reveal">
				<label class="pa-field"><span>Private key</span><textarea class="card-form-input" disabled={!secureContext} rows="3" required={needsKey} placeholder={needsKey ? 'Paste the private key' : 'Unchanged'} autocomplete="off" spellcheck="false" bind:value={privateKey}></textarea></label>
				<label class="pa-field"><span>Key passphrase</span><input class="card-form-input" disabled={!secureContext} type="password" placeholder={initial.usesKey && !privateKey ? 'Unchanged' : 'None'} autocomplete="new-password" bind:value={passphrase} /></label>
			</div>
		{/if}
		<p class="pa-hint">Leave a secret blank to keep the saved one. A new one replaces it.</p>
		<label class="pa-field"><span>Host-key fingerprint</span><input class="card-form-input card-form-input--mono" placeholder={saved.fingerprint && !addressChanged ? 'Unchanged' : 'SHA256:… (optional)'} spellcheck="false" bind:value={fingerprint} /></label>
		{#if addressChanged && !fingerprint.trim()}<p class="pa-hint" role="status">A new address or port clears the pinned host key. The next connection trusts and saves the key the server presents, unless you enter it here.</p>{/if}
	{/if}
	<p class="pa-hint">Encrypted on {where}. Saving any change except the name revokes access that uses this connection.</p>
	<div class="pa-actions">
		<button type="button" class="card-btn-secondary" disabled={busy} onclick={cancel}>Cancel</button>
		<button type="submit" class="card-btn-primary" disabled={busy || !secureContext}>{busy ? 'Saving…' : 'Save changes'}</button>
	</div>
</form>

<style>
	.pa-edit {
		flex: 1 1 100%;
		min-width: 0;
		display: grid;
		gap: 0.625rem;
		padding-top: 0.25rem;
	}
</style>

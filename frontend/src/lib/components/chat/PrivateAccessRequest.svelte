<script lang="ts">
	import '../deck/cards/card-design-system.css';
	import './private-access.css';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount, untrack } from 'svelte';
	import ServiceAccessRequest from './ServiceAccessRequest.svelte';
	type Saved = {id: string; label: string; target: string; fingerprint: string; kind?: string; writable?: boolean};
	let { sessionId, request, savedConnections, vaultAvailable, owner = null, clearSecretsKey = 0, onComplete }: { sessionId: string; request: { id: string; kind: string; purpose: string; expires_at: number; suggested_path?: string; service_provider?: string; suggested_url?: string }; savedConnections: Saved[]; vaultAvailable: boolean; owner?: string | null; clearSecretsKey?: number; onComplete: () => void } = $props();
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
	// Offer the saved server the model asked for (by host in its request), else
	// the first saved one; "New connection" stays one choice away.
	let savedId = $state(untrack(() => {
		const options = savable ? savedConnections.filter(s => (s.kind ?? 'machine') === request.kind) : [];
		if (!options.length) return '';
		const host = (target: string) => target.split('@').pop()?.split(':')[0] ?? '';
		return (options.find(s => host(s.target) && request.purpose.includes(host(s.target))) ?? options[0]).id;
	}));
	let saveConnection = $state(false);
	let label = $state(machine ? 'SSH server' : 'Folder');
	let minutes = $state(60);
	let path = $state(untrack(() => request.kind === 'folder' ? request.suggested_path ?? '' : ''));
	let writable = $state(false);
	let host = $state('');
	let port = $state(22);
	let username = $state('');
	let fingerprint = $state('');
	let password = $state('');
	let privateKey = $state('');
	let passphrase = $state('');
	let useKey = $state(false);
	let verifyKey = $state(false);
	let busy = $state(false);
	let error = $state('');
	const durations = [[15, '15 minutes'], [30, '30 minutes'], [60, '1 hour'], [120, '2 hours'], [240, '4 hours'], [480, '8 hours']] as const;
	const remaining = $derived(Math.max(0, Math.floor(request.expires_at - now / 1000)));
	const expired = $derived(remaining <= 0);
	const clock = $derived(`${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`);
	const saved = $derived(savedOptions.find(s => s.id === savedId));
	const endpoint = untrack(() => `/chat-access/${encodeURIComponent(sessionId)}/requests/${encodeURIComponent(request.id)}`);
	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (busy || expired || !secureContext) return;
		busy = true; error = '';
		const connection: Record<string, unknown> = {kind: request.kind, label, minutes, path, writable, host: host.trim(), port, username: username.trim(),
				fingerprint: verifyKey ? fingerprint.trim() : '', password: useKey ? '' : password,
				private_key: useKey ? privateKey : '', passphrase: useKey ? passphrase : ''};
		const body = {connection: savedId ? null : connection, saved_id: savedId, minutes, save_connection: !savedId && saveConnection};
		// No transcript, localStorage, sessionStorage, analytics, or generic question API.
		password = ''; privateKey = ''; passphrase = '';
		try { await api.post(endpoint, body); onComplete(); }
		catch (e) { error = getErrorMessage(e, 'Private access could not be granted.'); }
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

{#if service}
	<ServiceAccessRequest {sessionId} {request} {savedConnections} {vaultAvailable} {owner} {clearSecretsKey} {onComplete} />
{:else}
<section class="card-system pa-card" aria-label="Private access request">
	<header class="pa-head">
		<div class="pa-icon" aria-hidden="true">
			{#if machine}
				<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="7" rx="2" /><rect x="3" y="13" width="18" height="7" rx="2" /><path stroke-linecap="round" d="M7 7.5h.01M7 16.5h.01" /></svg>
			{:else}
				<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linejoin="round" d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" /></svg>
			{/if}
		</div>
		<div class="pa-title">
			<h3><span>{machine ? 'SSH access' : 'Folder access'}</span><span class="card-badge card-badge--muted">Private</span></h3>
			<p class="pa-subtitle" title={request.purpose}>{request.purpose}</p>
		</div>
		<span class="pa-timer" class:urgent={remaining < 60} role="timer" aria-label="Time left to respond">{expired ? 'Expired' : clock}</span>
	</header>

	<form onsubmit={submit} autocomplete="off">
		<div class="pa-body">
			{#if error}<p class="pa-error" role="alert">{error}</p>{/if}
			{#if !secureContext}<p class="pa-error" role="alert">Open the desktop app or an HTTPS connection before entering private details.</p>{/if}

			{#if savedOptions.length}
				<label class="pa-field"><span>Connection</span>
					<select class="card-form-input" bind:value={savedId}>
						<option value="">New connection</option>
						{#each savedOptions as option}<option value={option.id}>{option.label} · {option.target}</option>{/each}
					</select>
				</label>
			{/if}

			{#if saved}
				<div class="pa-note">
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m5.6-4.6A12 12 0 0112 3a12 12 0 01-8.6 3.4A12 12 0 003 9c0 5.6 3.8 10.3 9 11.6 5.2-1.3 9-6 9-11.6 0-1.2-.2-2.4-.4-3.6z" /></svg>
					<div class="pa-saved-detail">
						<span>Uses the saved sign-in for</span>
						<strong>{saved.target}</strong>
						{#if saved.fingerprint}
							<details class="pa-fingerprint">
								<summary>Host-key fingerprint</summary>
								<code>{saved.fingerprint}</code>
							</details>
						{/if}
					</div>
				</div>
			{:else if machine}
				<div class="pa-grid host">
					<label class="pa-field"><span>Server IP address</span><input class="card-form-input" required inputmode="decimal" spellcheck="false" bind:value={host} /></label>
					<label class="pa-field"><span>Port</span><input class="card-form-input" required type="number" min="1" max="65535" bind:value={port} /></label>
				</div>
				<div class="pa-grid">
					<label class="pa-field"><span>Username</span><input class="card-form-input" required autocomplete="off" spellcheck="false" bind:value={username} /></label>
					{#if !useKey}
						<label class="pa-field"><span>Password</span><input class="card-form-input" type="password" required autocomplete="new-password" bind:value={password} /></label>
					{/if}
				</div>
				<button type="button" class="pa-toggle" aria-expanded={useKey} onclick={() => useKey = !useKey}>{@render chevron()}Use a private key instead of a password</button>
				{#if useKey}
					<div class="pa-reveal">
						<label class="pa-field"><span>Private key</span><textarea class="card-form-input" rows="3" required autocomplete="off" spellcheck="false" bind:value={privateKey}></textarea></label>
						<label class="pa-field"><span>Key passphrase</span><input class="card-form-input" type="password" autocomplete="new-password" bind:value={passphrase} /></label>
					</div>
				{/if}
				<button type="button" class="pa-toggle" aria-expanded={verifyKey} onclick={() => verifyKey = !verifyKey}>{@render chevron()}Verify the host key</button>
				{#if verifyKey}
					<div class="pa-reveal">
						<label class="pa-field"><span>Host-key fingerprint</span><input class="card-form-input card-form-input--mono" placeholder="SHA256:…" spellcheck="false" bind:value={fingerprint} /></label>
						<p class="pa-hint">Optional. Any key type the server offers is accepted. When left blank, the key the server presents now is pinned for this access and shown once connected.</p>
					</div>
				{/if}
			{:else}
				<label class="pa-field"><span>Folder on {where}</span><input class="card-form-input card-form-input--mono" required placeholder="/home/me/documents" spellcheck="false" bind:value={path} /></label>
				<p class="pa-hint">Review or change the proposed path before granting access. The folder must already exist on {where}. For Docker or Unraid, use its mounted container path, not an unmounted host path.</p>
				<label class="pa-option"><input type="checkbox" bind:checked={writable} /><strong>Allow text writes</strong><small>Writes follow this session’s permission mode; bypass skips repeat approvals.</small></label>
			{/if}

			<div class="pa-grid">
				{#if !saved}<label class="pa-field"><span>Resource name</span><input class="card-form-input" required maxlength="80" bind:value={label} /></label>{/if}
				<label class="pa-field"><span>Access for</span>
					<select class="card-form-input" bind:value={minutes}>{#each durations as [value, text]}<option {value}>{text}</option>{/each}</select>
				</label>
			</div>

			<p class="pa-note">
				<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2" /><path stroke-linecap="round" d="M8 11V7a4 4 0 118 0v4" /></svg>
				<span>
					{#if machine}{sentTo}, never to the model or chat history. Commands run with this account’s authority and follow this session’s permission mode. Bypass skips repeat approvals after you grant access.
					{:else}{sentTo}. Kernel file confinement: no shell, links or mount traversal, and files up to 64 KiB.{/if}
				</span>
			</p>
		</div>

		<div class="pa-section">
			{#if savable && !saved}
				<label class="pa-option" class:disabled={!vaultAvailable}>
					<input type="checkbox" disabled={!vaultAvailable} bind:checked={saveConnection} />
					<strong>Save this connection</strong>
					<small>{vaultAvailable ? `Encrypted on ${where} for future requests. Access still needs approval each time.` : 'Secure storage is locked or unavailable. Temporary access still works.'}</small>
				</label>
			{/if}
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
</style>

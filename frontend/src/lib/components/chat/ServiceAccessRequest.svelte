<script lang="ts">
	import { onMount, onDestroy, tick, untrack } from 'svelte';
	import { api } from '$lib/api/client';
	import '../deck/cards/card-design-system.css';
	import './vault.css';
	import VaultSignInPicker from './VaultSignInPicker.svelte';
	import VaultDuration from './VaultDuration.svelte';

	type Saved = { id: string; label: string; target: string; fingerprint: string; kind?: string; writable?: boolean };
	type Request = { id: string; kind: string; purpose: string; expires_at: number; saved_id?: string; service_provider?: string; suggested_url?: string };
	let { sessionId, request, savedConnections, vaultAvailable, passwordSignIn = false, owner = null, clearSecretsKey = 0, onComplete }: {
		sessionId: string; request: Request; savedConnections: Saved[]; vaultAvailable: boolean;
		/** The worker that runs this chat can sign in with a username and password (HTTP Basic). */
		passwordSignIn?: boolean;
		owner?: string | null; clearSecretsKey?: number; onComplete: () => void;
	} = $props();
	const uid = $props.id();
	const cloudflareUrl = 'https://api.cloudflare.com/client/v4';
	const where = $derived(owner ?? 'this worker');
	const savedOptions = $derived(savedConnections.filter(s => s.kind === 'service'));
	// An explicit saved ID preselects only; the owner must still approve.
	let savedId = $state(untrack(() => savedConnections.find(s => s.id === request.saved_id && s.kind === 'service')?.id ?? ''));
	const saved = $derived(savedOptions.find(s => s.id === savedId));
	let provider = $state(untrack(() => request.service_provider === 'cloudflare' ? 'cloudflare' : 'custom'));
	let serviceUrl = $state(untrack(() => request.suggested_url || (request.service_provider === 'cloudflare' ? cloudflareUrl : '')));
	let label = $state(untrack(() => request.service_provider === 'cloudflare' ? 'Cloudflare' : 'API service'));
	let step = $state(1);
	let stepHeading = $state<HTMLHeadingElement>();
	let authHeader = $state('Authorization');
	let authScheme = $state('Bearer');
	// A token the service issued, or an account's username and password. The
	// worker encodes the pair itself and passes neither to the agent.
	let method = $state<'token' | 'password'>('token');
	const basic = $derived(method === 'password' && passwordSignIn && provider === 'custom');
	let username = $state('');
	let secret = $state('');
	let fingerprint = $state('');
	let pinCertificate = $state(false);
	let writable = $state(false);
	let saveConnection = $state(false);
	let consent = $state(false);
	let minutes = $state(60);
	let busy = $state(false);
	let terminal = $state(false);
	let complete = $state(false);
	let error = $state('');
	let secureContext = $state(false);
	let now = $state(Date.now());
	const expired = $derived(now / 1000 >= request.expires_at);
	const unavailable = $derived(expired || terminal || complete);
	const deadline = $derived(new Date(request.expires_at * 1000).toLocaleString());
	const endpoint = $derived(`/vault/sessions/${encodeURIComponent(sessionId)}/requests/${encodeURIComponent(request.id)}`);

	function clearSecret() { secret = ''; consent = false; }
	onMount(() => {
		secureContext = window.isSecureContext;
		const timer = setInterval(() => { now = Date.now(); }, 1000);
		return () => clearInterval(timer);
	});
	onDestroy(clearSecret);
	// Dialogs remain mounted when closed. Keep only the non-secret form choices.
	let previousClearKey = untrack(() => clearSecretsKey);
	$effect(() => {
		if (previousClearKey !== clearSecretsKey) { previousClearKey = clearSecretsKey; clearSecret(); }
	});
	$effect(() => { if (unavailable) clearSecret(); });
	// A typed secret belongs to one destination: this request in this session
	// on this worker, sent to one service address or saved sign-in, as a token
	// or as a password. It is cleared when that changes and when it is sent,
	// never by the form's own choices (duration, saving, write access, header,
	// scheme, certificate pinning; #673).
	// Compare values, not snapshot object identity: background refreshes must
	// not erase a token the owner is still preparing to submit.
	const credentialContext = $derived(JSON.stringify([
		sessionId, request.id, owner, provider, savedId, saved?.target, serviceUrl.trim(), method
	]));
	$effect(() => { void credentialContext; untrack(clearSecret); });

	function changeProvider() {
		clearSecret();
		serviceUrl = provider === 'cloudflare' ? cloudflareUrl : '';
		label = provider === 'cloudflare' ? 'Cloudflare' : 'API service';
		authHeader = 'Authorization'; authScheme = 'Bearer'; method = 'token'; username = '';
		pinCertificate = false; fingerprint = ''; writable = false;
	}
	async function goTo(next: number) {
		if (busy || unavailable) return;
		clearSecret(); error = ''; step = next;
		await tick(); stepHeading?.focus();
	}
	function validAddress(): boolean {
		try {
			const url = new URL(serviceUrl.trim());
			return url.protocol === 'https:' && !url.username && !url.password && !url.search && !url.hash;
		} catch { return false; }
	}
	async function advance() {
		if (savedId && !saved) { error = 'This saved sign-in is no longer available. Choose another.'; return; }
		if (!saved && !validAddress()) { error = 'Use an HTTPS API base address without credentials, a query, or a fragment.'; return; }
		await goTo(saved ? 3 : 2);
	}
	function failure(e: unknown, cancelling = false) {
		const status = (e as { status?: number })?.status;
		if ([401, 403, 404, 409, 410].includes(status ?? 0)) {
			terminal = true; clearSecret();
			error = 'This request is no longer available or you no longer have access. No new access can be granted here. Reopen the Vault to check its status.';
		} else {
			// Do not reflect server text that might contain user-entered credentials.
			error = cancelling ? 'Could not cancel. Try again; the request remains open.'
				: 'Access could not be granted. Check the details and retry. Anything you typed as a token or password was cleared from this form; enter it again.';
		}
	}
	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (busy || unavailable || !secureContext || step !== 3 || !consent) return;
		if (savedId && !saved) { clearSecret(); error = 'This saved sign-in is no longer available. Go back and choose another.'; return; }
		if (!saved && (!validAddress() || secret.length < 8)) return;
		if (!saved && basic && (!username.trim() || username.includes(':'))) return;
		busy = true; error = '';
		const connection = saved ? null : {
			kind: 'service', label: label.trim(), minutes, url: serviceUrl.trim(),
			auth_header: basic ? 'Authorization' : authHeader.trim(), auth_scheme: basic ? 'Basic' : authScheme.trim(),
			...(basic ? { username: username.trim() } : {}),
			secret, writable, fingerprint: pinCertificate ? fingerprint.trim() : ''
		};
		// The request body holds the only copy from here on: a failed attempt
		// leaves neither the secret nor the approval in the form.
		clearSecret();
		try {
			await api.post(endpoint, { connection, saved_id: savedId, minutes, save_connection: !saved && saveConnection });
			complete = true; onComplete();
		} catch (e) { failure(e); }
		finally { if (connection) connection.secret = ''; busy = false; }
	}
	async function cancel() {
		if (busy || unavailable) return;
		busy = true; clearSecret(); error = '';
		try { await api.delete(endpoint); complete = true; onComplete(); }
		catch (e) { failure(e, true); }
		finally { busy = false; }
	}
</script>

<section class="card-system pa-card service-setup" aria-label="Vault request">
	<header class="pa-head">
		<div class="pa-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7zM8 12l3 3 5-6" /></svg></div>
		<div class="pa-title"><h3><span>Connect a service</span></h3><p class="pa-subtitle pa-quoted" title={request.purpose}>{request.purpose}</p></div>
	</header>
	<ol class="setup-steps" aria-label="Connection steps">
		{#each ['Service', 'Prepare', 'Approve'] as title, index}<li aria-current={step === index + 1 ? 'step' : undefined} class:current={step === index + 1}><span>{index + 1}</span>{title}</li>{/each}
	</ol>
	<p class="pa-hint setup-wait">Nothing is granted until you approve in step 3. Take your time: this request waits until <time datetime={new Date(request.expires_at * 1000).toISOString()}>{deadline}</time> (up to 24 hours), unless it is cancelled or {where} restarts.</p>
	<form onsubmit={submit} autocomplete="off">
		<fieldset disabled={busy || unavailable}>
			<div class="pa-body">
				{#if error}<p class="pa-error" role="alert">{error}</p>{/if}
				{#if expired}<p class="pa-error" role="alert">Preparation expired. Ask for a new service request when you’re ready. This card cannot grant access.</p>{/if}
				{#if complete}<p class="pa-note" role="status">Request completed. Check the Vault for the current status.</p>{/if}
				<h4 bind:this={stepHeading} tabindex="-1">{step === 1 ? 'Choose the sign-in' : step === 2 ? (basic ? 'Prepare the account' : 'Prepare your API token') : 'Review and grant access'}</h4>
				{#if step === 1}
					{#if savedOptions.length}
						<VaultSignInPicker options={savedOptions} bind:value={savedId} pinLabel="Certificate" />
					{/if}
					{#if saved}
						<p class="pa-note">Uses the sign-in kept in the Vault on {where}. You still approve access for this chat.</p>
					{:else}
						<label class="pa-field"><span>Service</span><select class="card-form-input" bind:value={provider} onchange={changeProvider}><option value="cloudflare">Cloudflare</option><option value="custom">Other HTTPS service</option></select></label>
						<label class="pa-field"><span>Service address</span><input class="card-form-input card-form-input--mono" type="url" required readonly={provider === 'cloudflare'} placeholder="https://api.example.com/v1" spellcheck="false" bind:value={serviceUrl} /></label>
						<p class="pa-hint">{provider === 'cloudflare' ? 'Cloudflare’s official API address and Bearer authentication are filled in for you.' : 'Use the API base address from the service’s documentation, not its sign-in page. Do not put a key or token in this address.'}</p>
						{#if provider === 'custom' && passwordSignIn}
							<div class="pa-seg" role="radiogroup" aria-labelledby="{uid}-method">
								<span class="pa-seg-legend" id="{uid}-method">Signs in with</span>
								<div class="pa-seg-row">
									<label class:selected={method === 'token'}><input type="radio" name="{uid}-method" value="token" bind:group={method} /><span>API key or token</span></label>
									<label class:selected={method === 'password'}><input type="radio" name="{uid}-method" value="password" bind:group={method} /><span>Username and password</span></label>
								</div>
							</div>
						{/if}
					{/if}
				{:else if step === 2}
					{#if provider === 'cloudflare'}
						<ol class="prepare-list">
							<li><strong>Create a scoped API token.</strong><span>In Cloudflare, open My Profile → API Tokens → Create Token → Create Custom Token. Do not use your Global API key.</span></li>
							<li><strong>Limit what it can access.</strong><span>For read-only work, add only Read permissions for the resources in the request above. Limit access to the accounts or zones needed, rather than all resources. Choose a token expiry that fits the task.</span></li>
							<li><strong>Keep the token private.</strong><span>Review the summary, create the token, and keep it in your password manager. Cloudflare shows it once. Paste it only in the next step, never in chat.</span></li>
						</ol>
						<div class="setup-links"><a href="https://dash.cloudflare.com/profile/api-tokens/" target="_blank" rel="noopener noreferrer">Open Cloudflare API Tokens ↗</a><a href="https://developers.cloudflare.com/fundamentals/api/get-started/create-token/" target="_blank" rel="noopener noreferrer">Official token setup guide ↗</a><a href="https://developers.cloudflare.com/fundamentals/api/reference/permissions/" target="_blank" rel="noopener noreferrer">Find the minimum API permissions ↗</a></div>
						<p class="pa-hint">This connection uses an API token, not Cloudflare OAuth sign-in.</p>
					{:else if basic}
						<ol class="prepare-list"><li><strong>Use an account made for this.</strong><span>Create or choose an account on the service that holds only what this request needs, not your main administrator account.</span></li><li><strong>Start with read-only access.</strong><span>Give the account only the permissions and resources this request needs.</span></li><li><strong>Keep the password private.</strong><span>The username and password go to the service as HTTP Basic sign-in, over HTTPS only. Type them only in the next step, never in chat.</span></li></ol>
					{:else}
						<ol class="prepare-list"><li><strong>Find the service’s API credentials page.</strong><span>Use its official dashboard or documentation to create a dedicated API key or token. {passwordSignIn ? 'For an account’s username and password, go back and choose that instead. OAuth sign-in is not supported here.' : 'This form does not support password or OAuth sign-in.'}</span></li><li><strong>Start with read-only access.</strong><span>Choose only the permissions and resources needed for this request. Keep the token’s expiry short enough for the task.</span></li><li><strong>Check how the API accepts the token.</strong><span>Bearer authentication is filled in. For another format, open Authentication and certificate settings in the next step. Paste the token only in the private field, never in chat.</span></li></ol>
					{/if}
					<p class="pa-note">{basic ? 'No password is needed yet.' : 'No token is needed yet.'} You can leave this and come back: your choices stay while this chat remains open. Reloading restores the original request, not your edits.</p>
				{:else}
					<div class="connection-review"><span>Access on {where}</span><strong>{saved?.label ?? label}</strong><code>{saved?.target ?? serviceUrl}</code><small>{(saved ? saved.writable : writable) ? 'Changes allowed under this session’s permission mode' : 'Read-only · GET and HEAD only'}</small></div>
					{#if !secureContext}<p class="pa-error" role="alert">Open the desktop app or an HTTPS connection before entering private details.</p>{/if}
					{#if saved}
						{#if saved.fingerprint}<details class="pa-fingerprint"><summary>Certificate SHA-256 fingerprint</summary><code>{saved.fingerprint}</code></details>{/if}
					{:else}
						{#if basic}
							<div class="pa-grid">
								<label class="pa-field"><span>Username</span><input class="card-form-input" required maxlength="128" pattern="[^:]+" title="A username cannot contain a colon." autocomplete="off" spellcheck="false" bind:value={username} /></label>
								<label class="pa-field"><span>Password</span><input class="card-form-input" disabled={!secureContext} type="password" required minlength="8" autocomplete="new-password" spellcheck="false" bind:value={secret} /></label>
							</div>
						{:else}
							<label class="pa-field"><span>API key or token</span><input class="card-form-input" disabled={!secureContext} type="password" required minlength="8" autocomplete="new-password" spellcheck="false" bind:value={secret} /></label>
						{/if}
						<p class="pa-hint">Kept only in this open form until you submit. Going back clears it.</p>
						<details class="auth-settings"><summary>{basic ? 'Certificate settings' : 'Authentication and certificate settings'}</summary><div class="pa-reveal">
							{#if !basic}
							<div class="pa-grid"><label class="pa-field"><span>Credential header</span><input class="card-form-input" required maxlength="64" spellcheck="false" bind:value={authHeader} /></label><label class="pa-field"><span>Scheme</span><input class="card-form-input" maxlength="32" placeholder="None" spellcheck="false" bind:value={authScheme} /></label></div>
							<p class="pa-hint">Default: Authorization with Bearer. For X-API-Key, change the header and leave Scheme empty. Do not paste a token into either setting.</p>
							{/if}
							<label class="pa-option"><input type="checkbox" bind:checked={pinCertificate} /><strong>Trust a specific certificate</strong><small>For a self-signed home server. Normal certificate checks remain the default.</small></label>
							{#if pinCertificate}<label class="pa-field"><span>Certificate SHA-256 fingerprint</span><input class="card-form-input" required maxlength="95" pattern={'[0-9A-Fa-f]{64}|([0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}'} spellcheck="false" bind:value={fingerprint} /></label><p class="pa-hint">Verify this fingerprint through a trusted channel. Only this exact certificate is accepted; a replacement needs new approval. Saving to the Vault remembers this fingerprint.</p>{/if}
						</div></details>
						<label class="pa-option"><input type="checkbox" bind:checked={writable} /><strong>Allow changes</strong><small>POST, PUT, PATCH and DELETE follow this session’s permission mode. Bypass skips repeat approval after this initial grant.</small></label>
					{/if}
					{#if !saved}
						<label class="pa-field"><span>Name</span><input class="card-form-input" required maxlength="80" bind:value={label} /></label>
						<label class="pa-option" class:disabled={!vaultAvailable}><input type="checkbox" disabled={!vaultAvailable} bind:checked={saveConnection} /><strong>Save to Vault under this name</strong><small>{vaultAvailable ? `Encrypted on ${where}. Every use still asks you first.` : 'The Vault is locked or unavailable. Access for this chat still works.'}</small></label>
					{/if}
					<VaultDuration bind:minutes />
					<p class="pa-hint">Access duration starts when approved, not while you prepare. One-shot action approvals have their own deadlines.</p>
					<p class="pa-promise"><strong>The agent never sees the token or password.</strong> {saved ? `It stays encrypted on ${where}.` : `${owner ? `Sent only to ${owner} through this worker` : 'Sent only to this worker'}, never to the model or chat history.`} Requests stay under the approved address; redirects are not followed. TLS verification stays enabled unless you explicitly pin a certificate. Limit the {basic ? 'account' : 'token'} itself: the agent can use its allowed authority under that address.</p>
					<label class="pa-option"><input type="checkbox" disabled={!secureContext} bind:checked={consent} /><strong>I approve access for this conversation</strong><small>Only Grant access below creates a grant. You can revoke it from Vault.</small></label>
				{/if}
			</div>
			<div class="pa-section"><div class="pa-actions">
				<button type="button" class="card-btn-secondary" onclick={cancel}>{busy ? 'Please wait…' : 'Cancel request'}</button>
				{#if step === 1}<button type="button" class="card-btn-primary" onclick={advance}>{saved ? 'Continue to review' : 'Continue to setup'}</button>
				{:else if step === 2}<button type="button" class="card-btn-primary" onclick={() => goTo(3)}>{basic ? 'I have the sign-in' : 'I have a token'}</button>
				{:else}<button type="submit" class="card-btn-primary" disabled={!secureContext || !consent || (!saved && (secret.length < 8 || (basic && !username.trim())))}>{busy ? 'Granting access…' : 'Grant access'}</button>{/if}
			</div>{#if step > 1}<button type="button" class="setup-back" onclick={() => goTo(step === 3 && !saved ? 2 : 1)}>Back — clears {basic ? 'the entered password' : 'any entered token'}</button>{/if}</div>
		</fieldset>
	</form>
</section>

<style>
	fieldset { border: 0; margin: 0; padding: 0; min-width: 0; }
	.setup-steps { display: flex; gap: .75rem; list-style: none; margin: 0; padding: .875rem 1rem .5rem; }
	.setup-wait { padding: 0 1rem .75rem; border-bottom: 1px solid var(--border); }
	.setup-steps li { flex: 1; display: flex; align-items: center; gap: .375rem; font-size: .75rem; color: var(--muted-foreground); }
	.setup-steps span { display: grid; place-items: center; width: 1.5rem; height: 1.5rem; border: 1px solid var(--border); border-radius: 50%; }
	.setup-steps .current { font-weight: 600; color: var(--foreground); }
	.current span { background: var(--primary); color: var(--primary-foreground); border-color: var(--primary); }
	h4 { margin: 0; font-size: .9375rem; line-height: 1.4; }
	h4:focus { outline: none; }
	.prepare-list { margin: 0; padding-left: 1.25rem; display: grid; gap: 1rem; font-size: .8125rem; line-height: 1.5; }
	.prepare-list li { padding-left: .25rem; }
	.prepare-list strong, .prepare-list span { display: block; }
	.prepare-list span { color: var(--muted-foreground); margin-top: .2rem; }
	.setup-links { display: grid; gap: .25rem; }
	.setup-links a { display: flex; align-items: center; min-height: 44px; color: var(--foreground); text-underline-offset: 3px; font-size: .8125rem; }
	.setup-links a:hover { color: var(--primary); }
	.connection-review { display: grid; gap: .375rem; padding: .875rem; border: 1px solid var(--border); border-left: 3px solid var(--primary); border-radius: .5rem; overflow-wrap: anywhere; }
	.connection-review span, .connection-review small { font-size: .75rem; color: var(--muted-foreground); }
	.connection-review strong { font-size: .9375rem; }
	.connection-review code { font-family: var(--card-font-mono); font-size: .75rem; }
	.auth-settings { border: 1px solid var(--border); border-radius: .5rem; padding: 0 .75rem; }
	summary { display: flex; align-items: center; min-height: 44px; cursor: pointer; font-size: .75rem; }
	summary::before { content: '+'; margin-right: .5rem; }
	details[open] summary::before { content: '−'; }
	.auth-settings .pa-reveal { padding-bottom: .75rem; }
	.setup-back { min-height: 44px; border: 0; background: none; color: var(--muted-foreground); font: inherit; font-size: .75rem; text-decoration: underline; text-underline-offset: 3px; cursor: pointer; }
	.setup-back:hover { color: var(--foreground); }
	button, a, summary { transition: color .2s ease-out, background .2s ease-out; }
	button:active:not(:disabled) { transform: translateY(1px); }
	button:disabled { opacity: .5; cursor: not-allowed; }
	a:focus-visible, button:focus-visible, summary:focus-visible { outline: 2px solid var(--ring); outline-offset: 3px; }
	.service-setup :global(.card-form-input) { min-height: 44px; }
	.service-setup :global(.pa-subtitle) { display: block; -webkit-line-clamp: unset; line-clamp: unset; overflow: visible; }
	@media (max-width: 380px) { .setup-steps { gap: .375rem; } .setup-steps li { font-size: .6875rem; } }
	@media (prefers-reduced-motion: reduce) { button, a, summary { transition: none; } }
</style>

<script lang="ts">
	/**
	 * PluginServiceAccess: the private access card for a service an installed
	 * plugin declares with an OAuth sign-in (RequestChatAccess with plugin and
	 * service). One flow: the primary button signs in at the provider, then
	 * becomes "Grant access" for this conversation. No key or token is typed
	 * here; the grant is `{sign_in: true}` and the worker attaches the token.
	 * The manual client setup appears only when no client is available.
	 */
	import { onDestroy, onMount, tick, untrack } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { isBundledMobile } from '$lib/api/runtime';
	import { getSignInStatus, type PluginSignInStatus, type ServiceSignIn } from '$lib/api/plugin-oauth';
	import ClientSetupForm from '$lib/components/plugins/ClientSetupForm.svelte';
	import { SignInFlow, accessText, clientLine, pluginName, reasonText, returnAddressUsable, signInView } from '$lib/components/plugins/plugin-sign-in.svelte';
	import '../deck/cards/card-design-system.css';
	import './private-access.css';

	type Request = { id: string; kind: string; purpose: string; expires_at: number; suggested_url?: string; plugin: string; service: string };
	let { sessionId, request, owner = null, onComplete }: {
		sessionId: string; request: Request; owner?: string | null; onComplete: () => void;
	} = $props();

	const plugin = untrack(() => request.plugin);
	const service = untrack(() => request.service);
	const endpoint = untrack(() => `/chat-access/${encodeURIComponent(sessionId)}/requests/${encodeURIComponent(request.id)}`);
	const durations = [[15, '15 minutes'], [30, '30 minutes'], [60, '1 hour'], [120, '2 hours'], [240, '4 hours'], [480, '8 hours']] as const;

	let status = $state<PluginSignInStatus | null>(null);
	let loadError = $state('');
	let gone = $state(false);
	let minutes = $state(60);
	let writable = $state(false);
	let busy = $state(false);
	let error = $state('');
	let terminal = $state(false);
	let complete = $state(false);
	let justSignedIn = $state(false);
	let now = $state(Date.now());
	let grantButton = $state<HTMLButtonElement>();
	let mobileApp = $state(false);

	const entry = $derived<ServiceSignIn | undefined>(status?.services.find(s => s.service === service));
	const view = $derived(entry && status ? signInView(entry, status.enabled) : null);
	const host = $derived(entry?.authorize_host || hostOf(request.suggested_url) || service);
	const scopes = $derived(entry?.requested_scopes ?? []);
	const canWrite = $derived(entry?.access === 'write');
	const expired = $derived(now / 1000 >= request.expires_at);
	const closed = $derived(expired || terminal || complete);
	/** A mirrored conversation's sign-in lives on the device that runs it. */
	const remote = $derived(!!owner);
	const client = $derived(entry && !remote ? clientLine(entry) : null);
	const returnOk = $derived(!status || returnAddressUsable(status.redirect_uri));
	const canSignIn = $derived(!remote && !mobileApp && returnOk && view?.step === 'sign_in');
	const canGrant = $derived(remote ? !gone : view?.step === 'signed_in');

	const flow = new SignInFlow(plugin, service, async () => {
		const next = await load();
		return next?.state ?? null;
	});

	function hostOf(url: string | undefined): string {
		try { return url ? new URL(url).hostname : ''; } catch { return ''; }
	}

	async function load(): Promise<ServiceSignIn | null> {
		if (remote) return null;
		try {
			const before = entry?.state;
			status = await getSignInStatus(plugin, service);
			loadError = '';
			gone = false;
			const next = status.services.find(s => s.service === service) ?? null;
			if (next?.state === 'signed_in' && before && before !== 'signed_in') void signedIn();
			return next;
		} catch (e) {
			const code = (e as { status?: number })?.status;
			if (code === 404) { gone = true; loadError = ''; }
			else loadError = getErrorMessage(e, 'The sign-in status could not be loaded.');
			return null;
		}
	}

	async function signedIn() {
		justSignedIn = true;
		await tick();
		grantButton?.focus();
	}

	onMount(() => {
		mobileApp = isBundledMobile();
		void load();
		const timer = setInterval(() => { now = Date.now(); }, 1000);
		return () => clearInterval(timer);
	});
	onDestroy(() => flow.dispose());
	$effect(() => { if (closed) untrack(() => flow.cancel()); });

	function failure(e: unknown, cancelling = false) {
		const code = (e as { status?: number })?.status;
		if ([401, 403, 404, 410].includes(code ?? 0)) {
			terminal = true;
			error = 'This request is no longer available. Reopen Private access to check its status.';
		} else if (cancelling) {
			error = 'Could not cancel. Try again; the request remains open.';
		} else {
			// No private input is on this card, so the worker's wording is safe to show.
			error = getErrorMessage(e, 'Access could not be granted. Try again.');
		}
	}

	async function grant() {
		if (busy || closed || !canGrant) return;
		busy = true; error = '';
		try {
			await api.post(endpoint, { sign_in: true, writable: canWrite && writable, minutes });
			complete = true; onComplete();
		} catch (e) {
			failure(e);
			if (!terminal) void load();
		} finally { busy = false; }
	}

	async function cancel() {
		if (busy || closed) return;
		busy = true; error = ''; flow.cancel();
		try { await api.delete(endpoint); complete = true; onComplete(); }
		catch (e) { failure(e, true); }
		finally { busy = false; }
	}

	async function clientSaved(next: ServiceSignIn) {
		if (status) status = { ...status, services: status.services.map(s => s.service === next.service ? { ...s, ...next } : s) };
		await load();
	}

	const rail = $derived(
		!view || remote ? 'idle'
			: flow.phase === 'waiting' ? 'waiting'
				: view.step === 'signed_in' ? 'done'
					: flow.phase === 'error' || view.again || view.step === 'plugin_off' || view.step === 'invalid' ? 'warn'
						: 'idle'
	);
	const railLabel = $derived(
		remote ? `Signed in on ${owner}?`
			: !view ? (loadError || gone ? 'Unavailable' : 'Checking…')
				: flow.phase === 'waiting' ? 'Waiting for you'
					: view.step === 'signed_in' ? 'Signed in'
						: view.step === 'setup' ? 'Needs setup'
							: view.step === 'plugin_off' ? 'Plugin is off'
								: view.step === 'invalid' ? 'Unusable'
									: view.again ? 'Sign in again' : 'Not signed in'
	);
</script>

<section class="card-system pa-card plugin-signin" aria-label="Sign in to {service}">
	<header class="pa-head">
		<div class="pa-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.7 5.7L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.6a1 1 0 01.3-.7l6-6A6 6 0 1121 9z" /></svg></div>
		<div class="pa-title">
			<h3><span>Sign in to {service}</span><span class="card-badge card-badge--muted">Private</span></h3>
			<p class="pa-subtitle">{request.purpose}</p>
		</div>
	</header>

	<div class="pa-body">
		<div class="ps-rail" data-state={rail}>
			<span class="ps-end"><span class="ps-mark" aria-hidden="true">AI</span><span class="ps-end-name">AI Shuffle</span></span>
			<span class="ps-line" aria-hidden="true"><i></i><i></i><i></i></span>
			<span class="ps-end ps-far"><span class="ps-mark" aria-hidden="true">{host.slice(0, 1).toUpperCase()}</span><span class="ps-end-name ps-host">{host}</span></span>
			<span class="ps-state" role="status" aria-live="polite">{railLabel}</span>
		</div>

		<dl class="ps-facts">
			<div><dt>Plugin</dt><dd title={plugin}>{pluginName(plugin)}</dd></div>
			<div><dt>Service</dt><dd>{service}</dd></div>
			<div><dt>Access</dt><dd>{entry ? accessText(entry.access) : '…'}</dd></div>
		</dl>
		{#if client}
			<p class="ps-client"><span>{client.text}</span>{#if client.code}<code title={client.code}>{client.code}</code>{/if}</p>
		{/if}
		{#if scopes.length}
			<details class="ps-scopes">
				<summary>Scopes asked for <span class="ps-count">{scopes.length}</span></summary>
				<ul>{#each scopes as scope (scope)}<li><code>{scope}</code></li>{/each}</ul>
			</details>
		{/if}

		{#if error}<p class="pa-error" role="alert">{error}</p>{/if}
		{#if expired}<p class="pa-error" role="alert">This request expired. Ask the agent to request access again.</p>
		{:else if complete}<p class="pa-note" role="status">Request completed. Check Private access for the current status.</p>
		{:else if remote}
			<p class="pa-note">This conversation runs on {owner}. Sign-ins stay on each device: sign in to {service} from AI Shuffle on {owner}, then grant access here.</p>
		{:else if gone}
			<p class="pa-error" role="alert">This plugin or its {service} sign-in is no longer installed here.</p>
		{:else if loadError}
			<div class="pa-error ps-retry" role="alert"><span>{loadError}</span><button type="button" class="pa-small-btn" onclick={() => load()}>Retry</button></div>
		{:else if !view}
			<div class="ps-skeleton" aria-hidden="true"><span></span><span></span></div>
		{:else if view.step === 'invalid'}
			<p class="pa-error" role="alert">This plugin’s sign-in settings are broken, so it can’t be used. Ask its author for a fix.</p>
		{:else if view.step === 'plugin_off'}
			<div class="pa-error ps-retry" role="alert"><span>{pluginName(plugin)} is switched off. Turn it on in Settings › Plugins, then check again.</span><button type="button" class="pa-small-btn" onclick={() => load()}>Check again</button></div>
		{:else if view.step === 'setup'}
			{#if entry?.reason === 'registration_failed'}<p class="ps-warn">{reasonText(entry.reason)}</p>
			{:else}<p class="pa-note">{service} needs a one-time setup before you can sign in.</p>{/if}
			<details class="ps-manual">
				<summary>Set up manually</summary>
				<div class="ps-manual-body">
					<ClientSetupForm {plugin} {service} redirectUri={status?.redirect_uri ?? ''} disabled={closed} onSaved={clientSaved} />
				</div>
			</details>
		{:else}
			{#if mobileApp && view.step === 'sign_in'}
				<p class="pa-note">Sign in from a browser or the desktop app, then grant access here.</p>
			{:else if !returnOk && view.step === 'sign_in'}
				<p class="pa-error" role="alert">Signing in needs this worker’s HTTPS address or a browser on this computer.</p>
			{/if}
			{#if view.again && view.step === 'sign_in'}<p class="ps-warn">{reasonText(entry?.reason ?? '')}</p>{/if}
			{#if flow.phase === 'error'}<p class="pa-error" role="alert">{flow.error}</p>{/if}
			{#if flow.phase === 'waiting'}
				<p class="ps-wait">Finish signing in to {host} in the window that opened.
					<button type="button" class="ps-link" onclick={() => flow.cancel()}>Stop waiting</button></p>
			{:else if flow.unfinished && view.step === 'sign_in'}
				<p class="ps-wait">Not finished yet. If you closed the window, sign in again.</p>
			{/if}
			{#if view.step === 'signed_in' && justSignedIn}<p class="ps-ok">Signed in. Grant access to finish.</p>{/if}
		{/if}

		{#if !closed && !gone && (remote || view?.step === 'sign_in' || view?.step === 'signed_in')}
			<div class="ps-options">
				<label class="pa-field"><span>Access for</span><select class="card-form-input" bind:value={minutes} disabled={busy}>{#each durations as [value, text]}<option {value}>{text}</option>{/each}</select></label>
				{#if canWrite}
					<label class="pa-option"><input type="checkbox" bind:checked={writable} disabled={busy} /><strong>Allow changes</strong><small>POST, PUT, PATCH and DELETE follow this session’s permission mode.</small></label>
				{:else if entry}
					<p class="pa-hint">Read-only: GET and HEAD requests only.</p>
				{/if}
			</div>
		{/if}
	</div>

	<div class="pa-section">
		<div class="pa-actions" class:single={closed || gone || !(canGrant || canSignIn || flow.phase === 'waiting')}>
			<button type="button" class="card-btn-secondary" disabled={busy || closed} onclick={cancel}>Cancel request</button>
			{#if !closed && !gone}
				{#if canGrant}
					<button type="button" class="card-btn-primary" bind:this={grantButton} disabled={busy} onclick={grant}>{busy ? 'Granting access…' : 'Grant access'}</button>
				{:else if flow.phase === 'waiting'}
					<button type="button" class="card-btn-primary ps-waiting" disabled aria-busy="true"><span class="ps-spin" aria-hidden="true"></span>Waiting…</button>
				{:else if canSignIn}
					<button type="button" class="card-btn-primary" disabled={busy} onclick={() => flow.begin()}>Sign in with {host}</button>
				{/if}
			{/if}
		</div>
		<p class="pa-hint">The token stays on {owner ?? 'this worker'}. The agent never sees it.</p>
	</div>
</section>

<style>
	/* The rail: AI Shuffle on the left, the provider on the right, the
	   sign-in travelling between them. */
	.ps-rail {
		--rail: var(--muted-foreground);
		display: grid;
		grid-template-columns: auto minmax(2rem, 1fr) auto;
		grid-template-areas: 'a line b' 'state state state';
		align-items: center;
		gap: .5rem .625rem;
		padding: .75rem .875rem;
		border: 1px solid var(--border);
		border-radius: .75rem;
		background:
			radial-gradient(120% 140% at 100% 0%, color-mix(in oklab, var(--rail) 9%, transparent), transparent 60%),
			color-mix(in oklab, var(--muted) 30%, var(--card));
		transition: border-color .3s ease-out, background .3s ease-out;
	}
	.ps-rail[data-state='waiting'] { --rail: var(--primary); border-color: color-mix(in oklab, var(--primary) 40%, var(--border)); }
	.ps-rail[data-state='done'] { --rail: var(--success); border-color: color-mix(in oklab, var(--success) 45%, var(--border)); }
	.ps-rail[data-state='warn'] { --rail: var(--warning, var(--destructive)); border-color: color-mix(in oklab, var(--warning, var(--destructive)) 40%, var(--border)); }
	.ps-end { grid-area: a; display: inline-flex; align-items: center; gap: .5rem; min-width: 0; }
	.ps-far { grid-area: b; justify-self: end; flex-direction: row-reverse; }
	.ps-mark {
		flex: none;
		display: grid;
		place-items: center;
		width: 1.75rem;
		height: 1.75rem;
		border-radius: .5rem;
		border: 1px solid var(--border);
		background: var(--card);
		font-family: var(--card-font-mono, ui-monospace, monospace);
		font-size: .6875rem;
		font-weight: 700;
		letter-spacing: -.02em;
	}
	.ps-far .ps-mark { border-color: color-mix(in oklab, var(--rail) 45%, var(--border)); color: var(--rail); transition: color .3s, border-color .3s; }
	.ps-end-name { font-size: .75rem; font-weight: 600; white-space: nowrap; }
	.ps-host { font-family: var(--card-font-mono, ui-monospace, monospace); font-weight: 500; overflow: hidden; text-overflow: ellipsis; max-width: 14rem; }
	.ps-line { grid-area: line; position: relative; display: flex; justify-content: space-between; align-items: center; height: 2px; background: repeating-linear-gradient(90deg, color-mix(in oklab, var(--rail) 45%, transparent) 0 4px, transparent 4px 8px); }
	.ps-rail[data-state='done'] .ps-line { background: var(--rail); }
	.ps-line i { width: 6px; height: 6px; border-radius: 50%; background: var(--rail); opacity: 0; }
	.ps-rail[data-state='waiting'] .ps-line i { animation: ps-travel 1.4s cubic-bezier(.45, 0, .55, 1) infinite; }
	.ps-rail[data-state='waiting'] .ps-line i:nth-child(2) { animation-delay: .2s; }
	.ps-rail[data-state='waiting'] .ps-line i:nth-child(3) { animation-delay: .4s; }
	.ps-rail[data-state='done'] .ps-line i:nth-child(2) { opacity: 1; width: 14px; height: 14px; background: var(--rail) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M5 12l5 5L19 7'/%3E%3C/svg%3E") center / 9px no-repeat; animation: ps-pop .35s cubic-bezier(.16, 1, .3, 1); }
	@keyframes ps-travel { 0% { opacity: 0; transform: translateX(-10px); } 40%, 60% { opacity: 1; } 100% { opacity: 0; transform: translateX(10px); } }
	@keyframes ps-pop { from { transform: scale(.4); } to { transform: none; } }
	.ps-state { grid-area: state; font-size: .6875rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: var(--rail); }

	.ps-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .5rem; margin: 0; }
	.ps-facts div { min-width: 0; padding: .5rem .625rem; border-radius: .5rem; background: color-mix(in oklab, var(--muted) 35%, transparent); }
	.ps-facts dt { font-size: .625rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted-foreground); }
	.ps-facts dd { margin: .125rem 0 0; font-size: .8125rem; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

	.ps-client { display: flex; align-items: baseline; gap: .375rem; min-width: 0; margin: -.125rem 0 0; font-size: .75rem; color: var(--muted-foreground); }
	.ps-client span { flex: none; }
	.ps-client code { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--card-font-mono, ui-monospace, monospace); font-size: .6875rem; color: var(--foreground); }

	details { border: 1px solid var(--border); border-radius: .5rem; padding: 0 .75rem; }
	summary { display: flex; align-items: center; gap: .5rem; min-height: 40px; cursor: pointer; font-size: .75rem; list-style: none; }
	summary::-webkit-details-marker { display: none; }
	summary::before { content: '+'; width: .75rem; color: var(--muted-foreground); }
	details[open] summary::before { content: '−'; }
	summary:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; border-radius: .25rem; }
	.ps-count { font-size: .6875rem; color: var(--muted-foreground); font-variant-numeric: tabular-nums; }
	.ps-scopes ul { display: flex; flex-wrap: wrap; gap: .375rem; margin: 0; padding: 0 0 .75rem; list-style: none; }
	.ps-scopes code { display: inline-block; padding: .125rem .375rem; border: 1px solid var(--border); border-radius: .375rem; font-family: var(--card-font-mono, ui-monospace, monospace); font-size: .6875rem; overflow-wrap: anywhere; }
	.ps-manual-body { padding-bottom: .75rem; }

	.ps-retry { display: flex; align-items: center; justify-content: space-between; gap: .5rem; flex-wrap: wrap; }
	.ps-warn, .ps-ok, .ps-wait { margin: 0; padding: .5rem .625rem; border-radius: .625rem; font-size: .75rem; line-height: 1.45; }
	.ps-warn { color: var(--foreground); border: 1px solid color-mix(in oklab, var(--warning, var(--destructive)) 40%, transparent); background: color-mix(in oklab, var(--warning, var(--destructive)) 10%, var(--card)); }
	.ps-ok { border: 1px solid color-mix(in oklab, var(--success) 45%, transparent); background: color-mix(in oklab, var(--success) 14%, var(--card)); color: var(--foreground); font-weight: 500; animation: pa-expand .25s ease-out; }
	.ps-wait { border: 1px dashed color-mix(in oklab, var(--primary) 40%, var(--border)); color: var(--muted-foreground); }
	.ps-link { min-height: 32px; margin-left: .25rem; padding: 0 .25rem; border: 0; background: none; color: var(--foreground); font: inherit; text-decoration: underline; text-underline-offset: 3px; cursor: pointer; }
	.ps-link:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.ps-options { display: grid; gap: .625rem; }
	.ps-skeleton { display: grid; gap: .375rem; }
	.ps-skeleton span { height: .75rem; border-radius: .25rem; background: linear-gradient(90deg, var(--muted) 0%, color-mix(in oklab, var(--muted) 40%, var(--card)) 50%, var(--muted) 100%) 0 0 / 200% 100%; animation: ps-shimmer 1.2s linear infinite; }
	.ps-skeleton span:last-child { width: 60%; }
	@keyframes ps-shimmer { to { background-position: -200% 0; } }

	.pa-actions.single { grid-template-columns: minmax(0, 1fr); }
	.ps-waiting { display: inline-flex; align-items: center; justify-content: center; gap: .5rem; }
	.ps-spin { width: .875rem; height: .875rem; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: ps-rot .8s linear infinite; }
	@keyframes ps-rot { to { transform: rotate(360deg); } }
	.plugin-signin button:disabled { opacity: .55; cursor: not-allowed; }
	.plugin-signin button:active:not(:disabled) { transform: translateY(1px); }
	.plugin-signin :global(.card-form-input) { min-height: 44px; }
	.plugin-signin :global(.pa-subtitle) { display: block; -webkit-line-clamp: unset; line-clamp: unset; overflow: visible; }
	.pa-section .pa-hint { text-align: center; }

	@media (max-width: 420px) {
		.ps-facts { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
		.ps-facts div:last-child { grid-column: 1 / -1; }
		.ps-end-name:not(.ps-host) { display: none; }
		.ps-host { max-width: 9rem; }
	}
	@media (prefers-reduced-motion: reduce) {
		.ps-rail, .ps-far .ps-mark { transition: none; }
		.ps-rail[data-state='waiting'] .ps-line i { animation: none; opacity: 1; }
		.ps-rail[data-state='done'] .ps-line i:nth-child(2), .ps-ok { animation: none; }
		.ps-spin, .ps-skeleton span { animation: none; }
	}
</style>

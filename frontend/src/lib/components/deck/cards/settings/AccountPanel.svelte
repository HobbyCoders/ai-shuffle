<script lang="ts">
	/**
	 * AccountPanel — the Shuffle account this worker belongs to, as the setup
	 * wizard shows it (Settings > Devices > Shuffle account uses the same
	 * AccountFlow with its own layout).
	 *
	 * Signed out: sign in (or create an account during setup) in the browser on
	 * the account service, or on the phone with a QR/code. Signed in: the
	 * username (with first-time setup when the account has none yet), the
	 * other workers on the account, remote access state and sign-out. The
	 * account is optional for local and LAN use.
	 */
	import { onMount } from 'svelte';
	import { Check, Copy, ExternalLink, LogOut, Pencil, RefreshCw, Server, Smartphone, UserRound } from 'lucide-svelte';
	import { openSettingsTab, USERNAME_HINT } from '$lib/api/account';
	import { AccountFlow, groupCode } from '$lib/components/settings/devices/account-flow.svelte';

	let { workerName = 'My worker', variant = 'settings' }: { workerName?: string; variant?: 'settings' | 'setup' } = $props();

	const flow = new AccountFlow(() => workerName);
	const connection = $derived(flow.connection);
	const profile = $derived(flow.profile);
	const signedIn = $derived(flow.signedIn);
	const needsUsername = $derived(flow.needsUsername);
	const accountWorkers = $derived(profile?.servers ?? []);
	const refusal = $derived(flow.refusal);
	const browserSignedInThroughAccount = $derived(variant === 'settings' && flow.browserViaAccount);
	const remoteAccess = $derived(flow.remoteAccess);

	/** Re-read the account inventory (exposed for callers holding the component). */
	export function loadProfile(force = false) {
		return flow.loadProfile(force);
	}

	onMount(() => flow.start());

	function saveUsername(event?: Event) {
		event?.preventDefault();
		void flow.saveUsername();
	}
</script>

<section class="account" class:setup={variant === 'setup'} aria-label="Shuffle account">
	<div class="head">
		<div class="title">
			<span class="icon" aria-hidden="true"><UserRound size={16} /></span>
			<div class="text">
				<h4>Shuffle account</h4>
				<div class="identity">
					<span class="status" role="status">
						{#if connection}
							{#if connection.signed_in}
								{#if profile}Signed in as <strong>@{profile.account.username}</strong>{:else if refusal}Signed in · remote access off{:else if connection.profile_ready === false}Signed in · finishing connection…{:else if flow.profileError}Signed in{:else}Signed in · loading profile…{/if}
							{:else}Not signed in{/if}
						{:else}{flow.checking ? 'Checking account…' : 'Account status unavailable'}{/if}
					</span>
					{#if signedIn && profile && !needsUsername && !flow.editingName}
						<button type="button" class="btn icon-btn edit" onclick={() => flow.startEdit()} aria-label="Edit username" title="Edit username"><Pencil size={13} /></button>
					{/if}
				</div>
				{#if signedIn && remoteAccess}<span class="remote" class:connected={connection?.status === 'connected'}><span class="connection-dot" aria-hidden="true"></span>{remoteAccess}</span>{/if}
			</div>
		</div>
		<div class="head-actions">
			{#if signedIn}
				<button type="button" class="btn" disabled={flow.busy} onclick={() => flow.signOut()}><LogOut size={14} /> {flow.busy ? 'Signing out…' : 'Sign out'}</button>
				<button type="button" class="btn icon-btn refresh" disabled={flow.loadingProfile || flow.busy || flow.savingUsername || connection?.profile_ready === false} onclick={() => flow.loadProfile()} aria-label="Refresh account" title="Refresh account"><RefreshCw size={15} class={flow.loadingProfile ? 'refreshing' : ''} /></button>
			{:else if connection && !(flow.intent && connection.code)}
				<button type="button" class="btn primary" disabled={flow.busy || !flow.canStart} aria-describedby={!flow.canStart && connection.error ? 'account-panel-error' : undefined} onclick={() => flow.begin('signin')}>{flow.busy && flow.intent === 'signin' ? 'Preparing…' : 'Sign in'}</button>
				{#if variant === 'setup'}<button type="button" class="btn" disabled={flow.busy || !flow.canStart} aria-describedby={!flow.canStart && connection.error ? 'account-panel-error' : undefined} onclick={() => flow.begin('signup')}>{flow.busy && flow.intent === 'signup' ? 'Preparing…' : 'Create account'}</button>{/if}
			{/if}
		</div>
	</div>

	{#if !signedIn && variant === 'setup'}
		<p class="hint">Sign in on every worker you want to see from your phone or another worker. Workers on one account share settings, profiles and provider keys; the account's settings replace this worker's own. Local use needs no account.</p>
	{/if}

	{#if flow.offerOpen && connection?.code}
		<div class="flow">
			{#if connection.authorization_url}
				<a class="btn primary" href={flow.intent === 'signup' && connection.signup_url ? connection.signup_url : connection.authorization_url} target="_blank" rel="noopener noreferrer">
					<ExternalLink size={14} /> {flow.intent === 'signup' ? 'Create account in browser' : 'Sign in in browser'}
				</a>
				<p>Finish in the browser tab that opens, then return here. This worker joins your account as soon as you do.</p>
				<button type="button" class="btn" aria-expanded={flow.showPhone} onclick={() => flow.togglePhone()}><Smartphone size={14} /> {flow.showPhone ? 'Hide phone code' : 'Use my phone instead'}</button>
			{/if}
			{#if flow.showPhone || !connection.authorization_url}
				<p>In the Shuffle app, sign in and choose <strong>Add worker</strong>, then scan this code or type it.</p>
				{#if connection.qr_svg}<div class="qr" aria-label="Worker connection QR code">{@html connection.qr_svg}</div>{/if}
				<div class="code"><code>{groupCode(connection.code)}</code><button type="button" class="btn" onclick={() => flow.copyCode()} aria-label="Copy connection code">{#if flow.copied}<Check size={14} /> Copied{:else}<Copy size={14} /> Copy{/if}</button></div>
				<p>The code expires after ten minutes and links exactly one worker.</p>
			{/if}
			<button type="button" class="btn subtle" disabled={flow.busy} onclick={() => flow.signOut()}>{flow.busy ? 'Cancelling…' : 'Cancel'}</button>
		</div>
	{/if}

	{#if signedIn}
		{#if needsUsername || flow.editingName}
			<form class="username" onsubmit={saveUsername}>
				<label for="account-username">{needsUsername ? 'Choose your username' : 'Username'}</label>
				<p class="muted">{needsUsername ? 'Your account has a temporary name. Pick the one other devices will see.' : USERNAME_HINT}</p>
				<div class="row">
					<input id="account-username" class="input" bind:value={flow.usernameInput} maxlength="64" autocomplete="off" spellcheck="false" disabled={flow.savingUsername} />
					<button type="submit" class="btn primary" disabled={flow.savingUsername || !flow.usernameInput.trim()}>{flow.savingUsername ? 'Saving…' : 'Save'}</button>
					{#if !needsUsername}<button type="button" class="btn" disabled={flow.savingUsername} onclick={() => flow.cancelEdit()}>Cancel</button>{/if}
				</div>
			</form>
		{/if}
		{#if profile && variant === 'setup'}
			<div class="workers">
				<span class="muted"><Server size={13} /> Workers on this account</span>
				<ul aria-label="Workers on this account">
					{#each accountWorkers as worker (worker.id)}<li>{worker.name}{worker.current ? ' (this worker)' : ''}</li>{/each}
				</ul>
			</div>
			<div class="workers">
				<span class="muted"><Smartphone size={13} /> Phones and browsers on this account</span>
				{#if profile.devices === undefined}
					<span class="muted">Update the account service to see its phones and browsers here.</span>
				{:else if profile.devices.length === 0}
					<span class="muted">No phones or browsers registered to this account.</span>
				{:else}
					<ul aria-label="Phones and browsers on this account">
						{#each profile.devices as device (device.id)}<li>{device.name || 'Unnamed device'}{device.platform ? ` · ${device.platform}` : ''}</li>{/each}
					</ul>
				{/if}
			</div>
			<p class="muted">These are account registrations, including offline devices. Local network members and direct pairings are separate from account registrations.</p>
		{/if}
		{#if flow.profileError}<div class="error-row"><p role="alert">{flow.profileError}</p><button type="button" class="btn" onclick={() => flow.loadProfile()}>Retry profile</button></div>{/if}
		{#if browserSignedInThroughAccount}
			<button type="button" class="btn subtle" onclick={() => flow.signOutBrowser()}><LogOut size={13} /> Sign this browser out</button>
		{/if}
	{/if}

	{#if flow.statusError}
		<div class="error-row"><p role="alert">{flow.statusError}</p><button type="button" class="btn" disabled={flow.busy} onclick={() => flow.refresh()}>Retry account status</button></div>
	{/if}
	{#if refusal}
		<div class="refusal" role="alert">
			<p>{refusal.message}</p>
			<p class="muted">{refusal.used} of {refusal.limit} remote {refusal.limit === 1 ? 'connection' : 'connections'} in use on this account. This worker keeps working here and on your local network. It asks again every hour, and right away when you add a paid plan key.</p>
			{#if variant === 'settings'}
				<button type="button" class="btn" onclick={() => openSettingsTab('cloud')}>Open Settings &gt; Cloud</button>
			{:else}
				<p class="muted">You can add a paid plan key in Settings &gt; Cloud once setup is done.</p>
			{/if}
		</div>
	{/if}
	{#if flow.errorText}<p id="account-panel-error" class="error" role="alert">{flow.errorText}</p>{/if}
</section>

<style>
	.account { padding: 10px 14px; border-bottom: 1px solid var(--border); color: var(--foreground); }
	.account.setup { padding: 0; border: 0; }
	.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
	.title { display: flex; align-items: flex-start; gap: 10px; min-width: 0; }
	.icon { display: flex; margin-top: 2px; color: var(--primary); }
	.text { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
	.account:not(.setup) .text { flex-direction: row; align-items: center; flex-wrap: wrap; gap: 0 10px; }
	.account:not(.setup) .remote { flex-basis: 100%; }
	.identity { display: flex; align-items: center; gap: 6px; min-height: 28px; }
	.remote { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; line-height: 1.5; color: var(--muted-foreground); }
	.connection-dot { width: 5px; height: 5px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
	.connected .connection-dot { color: var(--success); }
	h4 { margin: 0; font-family: var(--font-sans); font-size: 13px; font-weight: 600; }
	.status { font-size: 12px; color: var(--muted-foreground); overflow-wrap: anywhere; }
	.head-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; flex-shrink: 0; }
	.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 36px; padding: 6px 12px; border: 1px solid var(--border); border-radius: 8px; background: transparent; color: var(--foreground); font: inherit; font-size: 12px; line-height: 1.4; text-decoration: none; cursor: pointer; }
	.btn:hover:not(:disabled) { background: var(--hover-overlay); }
	.btn:focus-visible, .input:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.btn:disabled { opacity: .55; cursor: default; }
	.btn.primary { background: var(--primary); color: var(--primary-foreground); border-color: var(--primary); }
	.btn.subtle { border-color: transparent; color: var(--muted-foreground); padding-left: 6px; padding-right: 6px; }
	.btn.icon-btn { width: 36px; padding: 0; flex-shrink: 0; color: var(--muted-foreground); }
	.btn.edit { width: 28px; min-height: 28px; border-color: transparent; }
	.refresh :global(.refreshing) { animation: account-refresh 1s linear infinite; }
	@keyframes account-refresh { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) { .refresh :global(.refreshing) { animation: none; } }
	.hint, p, .muted { font-size: 12px; line-height: 1.5; color: var(--muted-foreground); overflow-wrap: anywhere; }
	.hint { margin: 8px 0 0; }
	.flow { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; margin-top: 12px; padding: 12px; border: 1px solid var(--border); border-radius: 10px; background: color-mix(in oklch, var(--primary) 4%, var(--card)); }
	.flow p { margin: 0; }
	.qr { width: 172px; max-width: 100%; box-sizing: border-box; padding: 10px; background: white; border-radius: 8px; }
	.qr :global(svg) { display: block; width: 100%; height: auto; }
	.code { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; }
	.code code { font-family: var(--font-mono); font-size: 17px; letter-spacing: .06em; user-select: all; overflow-wrap: anywhere; }
	.username { margin-top: 12px; }
	.username label { display: block; font-size: 13px; font-weight: 600; }
	.username p { margin: 4px 0 8px; }
	.row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
	.input { box-sizing: border-box; flex: 1 1 200px; min-width: 0; min-height: 36px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--background); color: var(--foreground); font: inherit; font-size: 13px; }
	.workers { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; margin-top: 10px; }
	.workers { align-items: baseline; }
	.workers .muted { display: inline-flex; align-items: center; gap: 6px; }
	.workers ul { display: flex; flex-wrap: wrap; gap: 6px; margin: 0; padding: 0; list-style: none; }
	.workers li { padding: 2px 8px; border: 1px solid var(--border); border-radius: 999px; font-size: 12px; }
	.error, .error-row p { color: var(--destructive); margin: 8px 0 0; }
	.refusal { margin-top: 8px; display: flex; flex-direction: column; align-items: flex-start; gap: 6px; font-size: 12px; }
	.refusal p { margin: 0; }
	.refusal p:first-child { color: var(--warning, var(--destructive)); }
	.error-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
	@media (max-width: 480px) { .account { padding: 12px; } .head { flex-direction: column; } .head-actions { justify-content: flex-start; } .btn { min-height: 44px; } }
</style>

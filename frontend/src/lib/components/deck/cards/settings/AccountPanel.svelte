<script lang="ts">
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	/**
	 * AccountPanel — the Shuffle account this worker belongs to.
	 *
	 * Signed out: sign in (or create an account during setup) in the browser on
	 * the account service, or on the phone with a QR/code). Signed in: the
	 * username (with first-time setup when the account has none yet), the
	 * other workers on the account, remote access state and sign-out. The
	 * account is optional for local and LAN use.
	 */
	import { onMount } from 'svelte';
	import { Check, Copy, ExternalLink, LogOut, Pencil, RefreshCw, Server, Smartphone, UserRound } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { auth } from '$lib/stores/auth.svelte';
	import {
		beginAccountConnection, disconnectAccount, getAccountConnection, getAccountProfile, normalizeUsername,
		setAccountUsername, USERNAME_HINT, USERNAME_PATTERN, type AccountConnection, type AccountProfile
	} from '$lib/api/account';

	let { workerName = 'My worker', variant = 'settings', profile = $bindable<AccountProfile | null>(null), inventoryReady = $bindable(false) }: {
		workerName?: string; variant?: 'settings' | 'setup'; profile?: AccountProfile | null; inventoryReady?: boolean;
	} = $props();

	const POLL_MS = 4000;
	const INVENTORY_MS = 30_000;
	let profileCheckedAt = 0;
	let loadingProfile = $state(false);
	let connection = $state<AccountConnection | null>(null);
	let checking = $state(true);
	let busy = $state(false);
	let statusError = $state('');
	let actionError = $state('');
	let profileError = $state('');
	/** Which button opened the browser flow; decides which link is primary. */
	let intent = $state<'signin' | 'signup' | null>(null);
	let showPhone = $state(false);
	let copied = $state(false);
	let editingName = $state(false);
	let usernameInput = $state('');
	let savingUsername = $state(false);
	let stopped = false;
	let requestVersion = 0;
	let profileVersion = 0;
	let poll: ReturnType<typeof setTimeout> | undefined;

	const canStart = $derived(connection !== null && connection.status !== 'locked' && connection.status !== 'unavailable');
	const signedIn = $derived(Boolean(connection?.signed_in));
	const needsUsername = $derived(signedIn && profile !== null && !profile.account.profile_complete);
	const accountWorkers = $derived(profile?.servers ?? []);
	const browserSignedInThroughAccount = $derived(variant === 'settings' && auth.current.status?.auth_mode === 'account');
	const remoteAccess = $derived.by(() => {
		switch (connection?.status) {
			case 'connected': return 'Remote access is on';
			case 'connecting': return 'Turning on remote access…';
			case 'reconnecting': return 'Remote access is reconnecting';
			case 'locked': case 'unavailable': return connection?.error ? '' : 'Remote access is unavailable on this worker';
			default: return '';
		}
	});

	function scheduleRefresh() {
		clearTimeout(poll);
		if (!stopped) poll = setTimeout(refresh, POLL_MS);
	}

	async function refresh() {
		if (busy) { scheduleRefresh(); return; }
		const version = ++requestVersion;
		try {
			const next = await getAccountConnection();
			if (stopped || version !== requestVersion) return;
			const wasSignedIn = connection?.signed_in ?? false;
			const wasProfilePending = connection?.profile_ready === false;
			const reconnected = connection?.status !== 'connected' && next.status === 'connected';
			connection = next;
			statusError = '';
			if (next.signed_in) {
				if (!wasSignedIn) inventoryReady = false;
				intent = null; showPhone = false;
				if (!wasSignedIn || wasProfilePending || reconnected || Date.now() - profileCheckedAt >= INVENTORY_MS) void loadProfile();
			} else {
				inventoryReady = true;
				profileVersion++; profileCheckedAt = 0;
				profile = null; profileError = ''; editingName = false;
			}
		} catch (e) {
			if (!stopped && version === requestVersion) statusError = getErrorMessage(e, 'Could not check account status');
		} finally {
			if (!stopped && version === requestVersion) { checking = false; scheduleRefresh(); }
		}
	}

	async function loadProfile() {
		if (loadingProfile || savingUsername || !connection?.signed_in || connection.profile_ready === false) return;
		loadingProfile = true;
		profileCheckedAt = Date.now();
		const version = ++profileVersion;
		try {
			const next = await getAccountProfile();
			if (stopped || version !== profileVersion) return;
			profile = next; profileError = ''; inventoryReady = true;
			if (!next.account.profile_complete && !editingName) usernameInput = next.account.username;
		} catch (e) {
			if (!stopped && version === profileVersion) {
				profileError = getErrorMessage(e, 'Could not load the account profile');
				inventoryReady = false;
			}
		} finally { loadingProfile = false; }
	}

	onMount(() => {
		void refresh();
		return () => { stopped = true; requestVersion++; profileVersion++; clearTimeout(poll); };
	});

	async function start(kind: 'signin' | 'signup') {
		if (busy || !canStart || !connection || connection.signed_in) return;
		intent = kind; actionError = ''; copied = false; showPhone = false;
		// Reserve the tab in the click handler so popup blockers do not treat
		// the asynchronous offer response as an unsolicited window.
		const tab = window.open('about:blank', '_blank');
		if (tab) tab.opener = null;
		busy = true; requestVersion++;
		try {
			// Reuse a pending offer rather than registering another worker.
			if (!connection.code) connection = await beginAccountConnection(workerName);
			const url = kind === 'signup' ? connection.signup_url || connection.authorization_url : connection.authorization_url;
			if (tab && url) tab.location.replace(url);
			else tab?.close();
			statusError = '';
		} catch (e) {
			tab?.close();
			intent = null;
			actionError = getErrorMessage(e, 'Could not start the account sign-in');
		} finally { busy = false; scheduleRefresh(); }
	}

	async function copyCode() {
		if (!connection?.code) return;
		try { await copyText(connection.code); copied = true; }
		catch { copied = false; actionError = COPY_ERROR_MESSAGE; }
	}

	async function signOut() {
		if (busy) return;
		busy = true; actionError = ''; requestVersion++; profileVersion++;
		try {
			const result = await disconnectAccount();
			if (result) {
				connection = result;
				// A local sign-out can succeed while remote cleanup fails; keep that warning visible.
				actionError = result.error || '';
				statusError = '';
			}
			profile = null; profileError = ''; profileCheckedAt = 0; inventoryReady = true; intent = null; showPhone = false; editingName = false;
		} catch (e) {
			actionError = getErrorMessage(e, 'Could not sign out');
		} finally { busy = false; scheduleRefresh(); }
	}

	async function saveUsername(event?: Event) {
		event?.preventDefault();
		const username = normalizeUsername(usernameInput);
		if (!USERNAME_PATTERN.test(username)) { profileError = USERNAME_HINT; return; }
		savingUsername = true; profileError = ''; profileVersion++;
		try {
			profile = await setAccountUsername(username);
			editingName = false;
		} catch (e) {
			profileError = getErrorMessage(e, 'Could not save the username');
		} finally { savingUsername = false; }
	}

	function startEdit() {
		usernameInput = profile?.account.username ?? '';
		profileError = ''; editingName = true;
	}

	async function signOutBrowser() {
		try { await auth.logout(); } catch (e) { actionError = getErrorMessage(e, 'Could not sign out of this browser'); }
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
								{#if profile}Signed in as <strong>@{profile.account.username}</strong>{:else if connection.profile_ready === false}Signed in · finishing connection…{:else if profileError}Signed in{:else}Signed in · loading profile…{/if}
							{:else}Not signed in{/if}
						{:else}{checking ? 'Checking account…' : 'Account status unavailable'}{/if}
					</span>
					{#if signedIn && profile && !needsUsername && !editingName}
						<button type="button" class="btn icon-btn edit" onclick={startEdit} aria-label="Edit username" title="Edit username"><Pencil size={13} /></button>
					{/if}
				</div>
				{#if signedIn && remoteAccess}<span class="remote" class:connected={connection?.status === 'connected'}><span class="connection-dot" aria-hidden="true"></span>{remoteAccess}</span>{/if}
			</div>
		</div>
		<div class="head-actions">
			{#if signedIn}
				<button type="button" class="btn" disabled={busy} onclick={signOut}><LogOut size={14} /> {busy ? 'Signing out…' : 'Sign out'}</button>
				<button type="button" class="btn icon-btn refresh" disabled={loadingProfile || busy || savingUsername || connection?.profile_ready === false} onclick={loadProfile} aria-label="Refresh account" title="Refresh account"><RefreshCw size={15} class={loadingProfile ? 'refreshing' : ''} /></button>
			{:else if connection && !(intent && connection.code)}
				<button type="button" class="btn primary" disabled={busy || !canStart} aria-describedby={!canStart && connection.error ? 'account-panel-error' : undefined} onclick={() => start('signin')}>{busy && intent === 'signin' ? 'Preparing…' : 'Sign in'}</button>
				{#if variant === 'setup'}<button type="button" class="btn" disabled={busy || !canStart} aria-describedby={!canStart && connection.error ? 'account-panel-error' : undefined} onclick={() => start('signup')}>{busy && intent === 'signup' ? 'Preparing…' : 'Create account'}</button>{/if}
			{/if}
		</div>
	</div>

	{#if !signedIn && variant === 'setup'}
		<p class="hint">Sign in on every worker you want to see from your phone or another worker. Workers on one account share settings, profiles and provider keys; the account's settings replace this worker's own. Local use needs no account.</p>
	{/if}

	{#if canStart && intent && connection?.code && !connection.signed_in}
		<div class="flow">
			{#if connection.authorization_url}
				<a class="btn primary" href={intent === 'signup' && connection.signup_url ? connection.signup_url : connection.authorization_url} target="_blank" rel="noopener noreferrer">
					<ExternalLink size={14} /> {intent === 'signup' ? 'Create account in browser' : 'Sign in in browser'}
				</a>
				<p>Finish in the browser tab that opens, then return here. This worker joins your account as soon as you do.</p>
				<button type="button" class="btn" aria-expanded={showPhone} onclick={() => (showPhone = !showPhone)}><Smartphone size={14} /> {showPhone ? 'Hide phone code' : 'Use my phone instead'}</button>
			{/if}
			{#if showPhone || !connection.authorization_url}
				<p>In the Shuffle app, sign in and choose <strong>Add worker</strong>, then scan this code or type it.</p>
				{#if connection.qr_svg}<div class="qr" aria-label="Worker connection QR code">{@html connection.qr_svg}</div>{/if}
				<div class="code"><code>{connection.code.match(/.{1,4}/g)?.join(' ')}</code><button type="button" class="btn" onclick={copyCode} aria-label="Copy connection code">{#if copied}<Check size={14} /> Copied{:else}<Copy size={14} /> Copy{/if}</button></div>
				<p>The code expires after ten minutes and links exactly one worker.</p>
			{/if}
			<button type="button" class="btn subtle" disabled={busy} onclick={signOut}>{busy ? 'Cancelling…' : 'Cancel'}</button>
		</div>
	{/if}

	{#if signedIn}
		{#if needsUsername || editingName}
			<form class="username" onsubmit={saveUsername}>
				<label for="account-username">{needsUsername ? 'Choose your username' : 'Username'}</label>
				<p class="muted">{needsUsername ? 'Your account has a temporary name. Pick the one other devices will see.' : USERNAME_HINT}</p>
				<div class="row">
					<input id="account-username" class="input" bind:value={usernameInput} maxlength="64" autocomplete="off" spellcheck="false" disabled={savingUsername} />
					<button type="submit" class="btn primary" disabled={savingUsername || !usernameInput.trim()}>{savingUsername ? 'Saving…' : 'Save'}</button>
					{#if !needsUsername}<button type="button" class="btn" disabled={savingUsername} onclick={() => (editingName = false)}>Cancel</button>{/if}
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
		{#if profileError}<div class="error-row"><p role="alert">{profileError}</p><button type="button" class="btn" onclick={loadProfile}>Retry profile</button></div>{/if}
		{#if browserSignedInThroughAccount}
			<button type="button" class="btn subtle" onclick={signOutBrowser}><LogOut size={13} /> Sign this browser out</button>
		{/if}
	{/if}

	{#if statusError}
		<div class="error-row"><p role="alert">{statusError}</p><button type="button" class="btn" disabled={busy} onclick={refresh}>Retry account status</button></div>
	{/if}
	{#if actionError || connection?.error}<p id="account-panel-error" class="error" role="alert">{actionError || connection?.error}</p>{/if}
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
	.error-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
	@media (max-width: 480px) { .account { padding: 12px; } .head { flex-direction: column; } .head-actions { justify-content: flex-start; } .btn { min-height: 44px; } }
</style>

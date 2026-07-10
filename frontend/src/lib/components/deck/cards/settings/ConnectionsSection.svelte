<script lang="ts">
	import { onMount } from 'svelte';
	import { KeyRound, Plug, Info, Image, AlertTriangle, RefreshCw, UserPlus, Check, X, Lock, Pencil, Trash2, Loader2, CircleAlert } from 'lucide-svelte';
	import { claudeAuthenticated, claudeTokenValid } from '$lib/stores/auth.svelte';
	import type { ClaudeAccount } from '$lib/api/auth';

	interface Props {
		claudeLoginLoading: boolean;
		claudeOAuthUrl: string | null;
		claudeAuthCode: string;
		onClaudeAuthCodeChange: (value: string) => void;
		claudeCompletingLogin: boolean;
		githubUser: string | null;
		githubToken: string;
		onGithubTokenChange: (value: string) => void;
		githubLoginLoading: boolean;
		onStartClaudeOAuth: () => void;
		/** Force a fresh OAuth login even if a credential already exists
		 *  (recovers from an expired/unrefreshable token). */
		onStartClaudeReauth: () => void;
		onCompleteClaudeOAuth: () => void;
		// ----- Multiple Claude accounts -----
		claudeAccounts: ClaudeAccount[];
		activeAccountId: string | null;
		/** When false the encrypted store can't be opened — show a banner, not rows. */
		accountsEncryptionReady: boolean;
		accountsLoading: boolean;
		accountsError: string;
		addAccountOAuthUrl: string | null;
		addAccountCode: string;
		onAddAccountCodeChange: (value: string) => void;
		addAccountName: string;
		onAddAccountNameChange: (value: string) => void;
		addAccountMakeActive: boolean;
		onAddAccountMakeActiveChange: (value: boolean) => void;
		/** When on, a chat that hits a Claude usage limit auto-switches to another
		 *  saved account and retries. Only meaningful with 2+ accounts. */
		claudeAutoSwitchOnLimit: boolean;
		onToggleClaudeAutoSwitch: () => void;
		addAccountLoading: boolean;
		addAccountCompleting: boolean;
		/** Id of the row mid-switch (drives its inline spinner), else null. */
		switchingAccountId: string | null;
		onStartAddAccount: () => void;
		onCompleteAddAccount: () => void;
		onCancelAddAccount: () => void;
		onActivateAccount: (id: string) => void;
		onRenameAccount: (id: string, name: string) => void;
		onDeleteAccount: (id: string) => void;
		onConnectGithub: () => void;
		onDisconnectGithub: () => void;
	}

	let {
		claudeLoginLoading,
		claudeOAuthUrl,
		claudeAuthCode,
		onClaudeAuthCodeChange,
		claudeCompletingLogin,
		githubUser,
		githubToken,
		onGithubTokenChange,
		githubLoginLoading,
		onStartClaudeOAuth,
		onStartClaudeReauth,
		onCompleteClaudeOAuth,
		claudeAccounts,
		activeAccountId,
		accountsEncryptionReady,
		accountsLoading,
		accountsError,
		addAccountOAuthUrl,
		addAccountCode,
		onAddAccountCodeChange,
		addAccountName,
		onAddAccountNameChange,
		addAccountMakeActive,
		onAddAccountMakeActiveChange,
		claudeAutoSwitchOnLimit,
		onToggleClaudeAutoSwitch,
		addAccountLoading,
		addAccountCompleting,
		switchingAccountId,
		onStartAddAccount,
		onCompleteAddAccount,
		onCancelAddAccount,
		onActivateAccount,
		onRenameAccount,
		onDeleteAccount,
		onConnectGithub,
		onDisconnectGithub,
	}: Props = $props();

	// ----- Account list local UI state -----
	// Inline rename: which row is editing + its draft text. Delete confirm: which
	// row is armed. Both are transient view state, so they live here, not in the
	// parent (the parent owns only the server-round-trip state).
	let renamingId: string | null = $state(null);
	let renameDraft = $state('');
	let confirmDeleteId: string | null = $state(null);
	// Whether to reveal the add-account panel (button → inline form). The panel
	// also stays open whenever an OAuth flow is mid-paste.
	let showAddAccount = $state(false);

	function beginRename(acc: ClaudeAccount) {
		renamingId = acc.id;
		renameDraft = acc.name || acc.email || '';
		confirmDeleteId = null;
	}
	function cancelRename() {
		renamingId = null;
		renameDraft = '';
	}
	function saveRename(id: string) {
		const next = renameDraft.trim();
		if (next) onRenameAccount(id, next);
		renamingId = null;
		renameDraft = '';
	}
	function requestDelete(id: string) {
		confirmDeleteId = id;
		renamingId = null;
	}
	function confirmDelete(id: string) {
		confirmDeleteId = null;
		onDeleteAccount(id);
	}

	// Client-side expiry check — the badge reads live from expires_at so it stays
	// accurate without a refetch. Null expiry = treat as not-expired.
	function isExpired(acc: ClaudeAccount): boolean {
		return acc.expires_at !== null && acc.expires_at < Date.now();
	}

	// Compact relative "last used" (e.g. "3h ago"). Returns '' when never used.
	function relativeLastUsed(ts: number | null): string {
		if (!ts) return '';
		const diff = Date.now() - ts;
		// Guard against non-finite input (NaN/Infinity) so a bad timestamp can
		// never render as "NaNy ago".
		if (!Number.isFinite(diff)) return '';
		if (diff < 0) return 'just now';
		const min = Math.floor(diff / 60000);
		if (min < 1) return 'just now';
		if (min < 60) return `${min}m ago`;
		const hr = Math.floor(min / 60);
		if (hr < 24) return `${hr}h ago`;
		const day = Math.floor(hr / 24);
		if (day < 30) return `${day}d ago`;
		const mo = Math.floor(day / 30);
		if (mo < 12) return `${mo}mo ago`;
		return `${Math.floor(mo / 12)}y ago`;
	}

	// Show the account section whenever there's something to show: accounts exist,
	// a store is locked (banner), or the user is mid-add. Keeps the surface hidden
	// for the plain not-yet-connected case (the connect card above covers it).
	let showAccountSection = $derived(
		!accountsEncryptionReady || claudeAccounts.length > 0 || showAddAccount || !!addAccountOAuthUrl
	);
	// The add-account panel is open when the user toggled it OR a flow is in progress.
	let addPanelOpen = $derived(showAddAccount || !!addAccountOAuthUrl);

	function openAddPanel() {
		showAddAccount = true;
	}
	function closeAddPanel() {
		showAddAccount = false;
		onCancelAddAccount();
	}

	// Token present but expired/invalid: the file exists (so $claudeAuthenticated
	// is true) yet the OAuth token can't be used — generations fail with 401 and
	// there's no refresh token to renew it. Surface this honestly and steer the
	// user to the Re-authenticate action.
	let tokenExpired = $derived($claudeAuthenticated && !$claudeTokenValid);

	// ----- Anthropic API key (self-contained: this section owns its state) -----
	// This key is a Claude Code AUTHENTICATION method. When set, Claude Code
	// runs all sessions with it (injected as ANTHROPIC_API_KEY), overriding the
	// connected Claude account. When cleared, Claude Code reverts to the account.
	let anthropicKeySet = $state(false);
	let anthropicKeyMasked = $state('');
	let anthropicKeyInput = $state('');
	let anthropicSaving = $state(false);
	let anthropicError = $state('');
	let anthropicSuccess = $state('');

	// Which auth method Claude Code is actively using, from the backend:
	// 'api_key' (key overrides the account) or 'account' (connected OAuth).
	// API-key login wins whenever a key is set — no silent billing switch.
	let claudeCodeAuthMethod = $state('account');
	let usingApiKeyAuth = $derived(claudeCodeAuthMethod === 'api_key');

	// ----- fal.ai key (self-contained) -----
	// Lives here, not on AI Models, so every credential has a single home
	// ("Connections"). AI Models keeps only model selection & parameters.
	let falKeySet = $state(false);
	let falKeyMasked = $state('');
	let falKeyInput = $state('');
	let falSaving = $state(false);
	let falError = $state('');
	let falSuccess = $state('');

	// Both keys come from the same /settings/integrations payload.
	async function loadKeyStatus() {
		try {
			const res = await fetch('/api/v1/settings/integrations', { credentials: 'include' });
			if (!res.ok) return;
			const data = await res.json();
			anthropicKeySet = !!data.anthropic_api_key_set;
			anthropicKeyMasked = data.anthropic_api_key_masked ?? '';
			claudeCodeAuthMethod = data.claude_code_auth_method ?? (data.anthropic_api_key_set ? 'api_key' : 'account');
			falKeySet = !!data.fal_api_key_set;
			falKeyMasked = data.fal_api_key_masked ?? '';
		} catch (e) {
			// Silent — non-critical, user can retry by reopening settings
			console.warn('Failed to load credential status', e);
		}
	}

	async function saveAnthropicKey() {
		if (!anthropicKeyInput.trim()) return;
		anthropicSaving = true;
		anthropicError = '';
		anthropicSuccess = '';
		try {
			const res = await fetch('/api/v1/settings/integrations/anthropic', {
				method: 'POST',
				credentials: 'include',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ api_key: anthropicKeyInput.trim() }),
			});
			if (!res.ok) {
				const body = await res.json().catch(() => ({}));
				anthropicError = body.detail ?? `Save failed (${res.status})`;
				return;
			}
			const data = await res.json();
			anthropicKeySet = true;
			anthropicKeyMasked = data.masked_key ?? '';
			anthropicKeyInput = '';
			// Setting a key makes Claude Code authenticate with it, overriding
			// the connected account.
			claudeCodeAuthMethod = 'api_key';
			anthropicSuccess = 'Saved — Claude Code now uses this API key';
			setTimeout(() => (anthropicSuccess = ''), 2500);
		} catch (e) {
			anthropicError = e instanceof Error ? e.message : 'Network error';
		} finally {
			anthropicSaving = false;
		}
	}

	async function removeAnthropicKey() {
		anthropicSaving = true;
		anthropicError = '';
		anthropicSuccess = '';
		try {
			const res = await fetch('/api/v1/settings/integrations/anthropic', {
				method: 'DELETE',
				credentials: 'include',
			});
			if (!res.ok) {
				anthropicError = `Remove failed (${res.status})`;
				return;
			}
			anthropicKeySet = false;
			anthropicKeyMasked = '';
			// Clearing the key reverts Claude Code to the connected account.
			claudeCodeAuthMethod = 'account';
			anthropicSuccess = 'Removed — Claude Code reverted to the connected account';
			setTimeout(() => (anthropicSuccess = ''), 2500);
		} catch (e) {
			anthropicError = e instanceof Error ? e.message : 'Network error';
		} finally {
			anthropicSaving = false;
		}
	}

	async function saveFalKey() {
		if (!falKeyInput.trim()) return;
		falSaving = true;
		falError = '';
		falSuccess = '';
		try {
			const res = await fetch('/api/v1/settings/integrations/fal-ai', {
				method: 'POST',
				credentials: 'include',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ api_key: falKeyInput.trim() }),
			});
			if (!res.ok) {
				const body = await res.json().catch(() => ({}));
				falError = body.detail ?? `Save failed (${res.status})`;
				return;
			}
			const data = await res.json();
			falKeySet = true;
			falKeyMasked = data.masked_key ?? '';
			falKeyInput = '';
			falSuccess = data.valid ? 'Saved & verified' : 'Saved (could not verify)';
			setTimeout(() => (falSuccess = ''), 2500);
		} catch (e) {
			falError = e instanceof Error ? e.message : 'Network error';
		} finally {
			falSaving = false;
		}
	}

	async function removeFalKey() {
		falSaving = true;
		falError = '';
		falSuccess = '';
		try {
			const res = await fetch('/api/v1/settings/integrations/fal-ai', {
				method: 'DELETE',
				credentials: 'include',
			});
			if (!res.ok) {
				falError = `Remove failed (${res.status})`;
				return;
			}
			falKeySet = false;
			falKeyMasked = '';
			falSuccess = 'Removed';
			setTimeout(() => (falSuccess = ''), 2500);
		} catch (e) {
			falError = e instanceof Error ? e.message : 'Network error';
		} finally {
			falSaving = false;
		}
	}

	// Fetch on mount. No reactivity needed — key status only changes via this
	// component's own save/remove actions.
	onMount(() => {
		loadKeyStatus();
	});
</script>

<!-- One home for every credential -->
<div class="ed-note">
	<Info />
	<div><b>One home for every credential.</b> API keys and sign-ins live here together — each row shows its status and is masked. Model selection &amp; parameters stay on <b>AI Models</b>.</div>
</div>

<!-- Anthropic / Claude -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><KeyRound />Claude Code login</h3>
		<p>Choose how Claude Code authenticates: your Claude account, or an Anthropic API key. Pick one — an API key always overrides the account.</p>
	</div>

	<!-- Active-method banner -->
	<div class="ed-note authnote">
		<Info />
		<div>
			{#if usingApiKeyAuth}
				<b>Authenticating with the Anthropic API key.</b> While a key is set it overrides the connected Claude account — all Claude Code sessions run and bill through this key. Remove the key to switch back to your account.
			{:else}
				<b>Authenticating with your connected Claude account.</b> Set an Anthropic API key below to authenticate with the key instead (it will override the account until removed).
			{/if}
		</div>
	</div>

	<!-- Claude Code OAuth (account login method) -->
	<div class="keycard">
		<div class="kc-ic">
			<svg viewBox="0 0 24 24" fill="currentColor">
				<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
			</svg>
		</div>
		<div class="kc-body">
			<h4>Claude account
				{#if tokenExpired}<span class="sb warn">Token expired — reconnect</span>
				{:else if $claudeAuthenticated && !usingApiKeyAuth}<span class="sb ok">Active</span>
				{:else if $claudeAuthenticated}<span class="sb off">Connected · overridden by API key</span>
				{:else}<span class="sb off">Not connected</span>{/if}
			</h4>
			<p>Sign in with your Claude subscription. Used as the login method whenever no API key is set.</p>
		</div>
		<div class="kc-acts">
			{#if claudeOAuthUrl}
				<!-- OAuth in progress (connect OR re-auth): paste the code from the browser. -->
				<input
					type="text"
					value={claudeAuthCode}
					oninput={(e) => onClaudeAuthCodeChange(e.currentTarget.value)}
					placeholder="Paste code from browser"
					class="inp kc-in"
				/>
				<button type="button" class="ed-btn primary" onclick={onCompleteClaudeOAuth} disabled={claudeCompletingLogin || !claudeAuthCode}>
					{claudeCompletingLogin ? '…' : 'Complete'}
				</button>
			{:else if $claudeAuthenticated}
				<!-- Re-authenticate is ALWAYS available while connected — the primary
				     recovery path when generations fail with an auth error (e.g. an
				     expired token with no refresh token). When the token is still
				     expired it becomes the prominent (primary-styled) action. -->
				{#if !tokenExpired}<span class="kc-val authed">Authenticated</span>{/if}
				<button
					type="button"
					class="ed-btn kc-reauth"
					class:primary={tokenExpired}
					onclick={onStartClaudeReauth}
					disabled={claudeLoginLoading}
					title="Force a fresh login even if currently connected — use this if generations fail with an auth error."
				>
					<RefreshCw />
					{claudeLoginLoading ? '…' : 'Re-authenticate'}
				</button>
			{:else}
				<button type="button" class="ed-btn primary" onclick={onStartClaudeOAuth} disabled={claudeLoginLoading}>
					{claudeLoginLoading ? '…' : 'Connect'}
				</button>
			{/if}
		</div>
	</div>

	{#if tokenExpired}
		<div class="kc-foot warn" role="alert">
			<AlertTriangle />
			<span>Your Claude token has expired and can't be refreshed automatically. Generations will fail with an authentication error until you <b>Re-authenticate</b> above.</span>
		</div>
	{/if}

	<!-- Multiple Claude accounts: switch which sign-in the app authenticates with.
	     Rendered whenever accounts exist, the store is locked, or a flow is open. -->
	{#if showAccountSection}
		<div class="acct-block">
			<div class="acct-head">
				<h4>Saved accounts</h4>
				{#if accountsEncryptionReady && !addPanelOpen}
					<button type="button" class="ed-btn acct-add-btn" onclick={openAddPanel} disabled={accountsLoading}>
						<UserPlus />Add account
					</button>
				{/if}
			</div>

			{#if !accountsEncryptionReady}
				<!-- Locked store: the account list & switching are unavailable until the
				     admin re-derives the key by logging in again. -->
				<div class="kc-foot warn acct-locked" role="status">
					<Lock />
					<span>Accounts are encrypted with your admin password — log in again to unlock switching.</span>
				</div>
			{:else}
				{#if accountsError}
					<div class="kc-foot err" role="alert">{accountsError}</div>
				{/if}

				{#if accountsLoading && claudeAccounts.length === 0}
					<div class="acct-empty">Loading accounts…</div>
				{:else if claudeAccounts.length === 0 && !addPanelOpen}
					<div class="acct-empty">No saved accounts yet. Add one to switch between Claude sign-ins.</div>
				{/if}

				<ul class="acct-list">
					{#each claudeAccounts as acc (acc.id)}
						{@const expired = isExpired(acc)}
						{@const lastUsed = relativeLastUsed(acc.last_used_at)}
						<li class="acct-row" class:active={acc.is_active}>
							<div class="acct-ic" aria-hidden="true">
								{(acc.name || acc.email || '?').trim().charAt(0).toUpperCase()}
							</div>
							<div class="acct-body">
								{#if renamingId === acc.id}
									<!-- Inline rename: name field replaces the title until save/cancel. -->
									<div class="acct-rename">
										<input
											type="text"
											class="inp acct-rename-in"
											value={renameDraft}
											oninput={(e) => (renameDraft = e.currentTarget.value)}
											onkeydown={(e) => {
												if (e.key === 'Enter') saveRename(acc.id);
												if (e.key === 'Escape') cancelRename();
											}}
											placeholder="Account name"
											aria-label="Account name"
										/>
										<button type="button" class="acct-iconbtn ok" title="Save name" aria-label="Save name" onclick={() => saveRename(acc.id)}><Check /></button>
										<button type="button" class="acct-iconbtn" title="Cancel" aria-label="Cancel rename" onclick={cancelRename}><X /></button>
									</div>
								{:else}
									<div class="acct-name">
										<span class="acct-title">{acc.name || acc.email || 'Claude account'}</span>
										{#if acc.is_active}<span class="sb ok">Active</span>{/if}
										{#if expired}<span class="sb warn" title="This account's token has expired">Token expired</span>{/if}
										{#if !acc.has_refresh_token}<span class="sb warn" title="No refresh token — this sign-in can't renew automatically and will need re-authentication when it expires">No refresh token</span>{/if}
										{#if acc.sync_pending}<span class="sb warn acct-sync" title="Latest tokens not yet saved — unlock encryption by logging in again as admin"><CircleAlert />Sync pending</span>{/if}
									</div>
									<p class="acct-meta">
										<span>{acc.email || '—'}</span>
										{#if acc.subscription_type}<span class="acct-dot">·</span><span>{acc.subscription_type}</span>{/if}
										{#if lastUsed}<span class="acct-dot">·</span><span>used {lastUsed}</span>{/if}
									</p>
								{/if}
							</div>

							{#if renamingId !== acc.id}
								<div class="acct-acts">
									{#if confirmDeleteId === acc.id}
										<span class="acct-confirm">Delete?</span>
										<button type="button" class="ed-btn danger acct-sm" onclick={() => confirmDelete(acc.id)}>Delete</button>
										<button type="button" class="ed-btn acct-sm" onclick={() => (confirmDeleteId = null)}>Cancel</button>
									{:else}
										{#if !acc.is_active}
											<button
												type="button"
												class="ed-btn primary acct-sm"
												onclick={() => onActivateAccount(acc.id)}
												disabled={switchingAccountId !== null}
											>
												{#if switchingAccountId === acc.id}<Loader2 class="spin" />Switching…{:else}Switch{/if}
											</button>
										{/if}
										<button type="button" class="acct-iconbtn" title="Rename" aria-label="Rename account" onclick={() => beginRename(acc)}><Pencil /></button>
										<button type="button" class="acct-iconbtn danger" title="Remove" aria-label="Remove account" onclick={() => requestDelete(acc.id)}><Trash2 /></button>
									{/if}
								</div>
							{/if}
						</li>
					{/each}
				</ul>

				<!-- Auto-switch on usage limits — only meaningful once there are at
				     least two accounts to fail over between. -->
				{#if claudeAccounts.length >= 2}
					<div class="acct-autoswitch">
						<div class="acct-autoswitch-lab">
							<h5>Auto-switch on usage limits</h5>
							<p>When a chat hits a Claude usage limit, automatically switch to another saved account and retry.</p>
						</div>
						<button
							type="button"
							class="switch"
							class:on={claudeAutoSwitchOnLimit}
							role="switch"
							aria-checked={claudeAutoSwitchOnLimit}
							aria-label="Auto-switch on usage limits"
							onclick={onToggleClaudeAutoSwitch}
						><i></i></button>
					</div>
				{/if}

				<!-- Add-account panel: same code-paste UX as the primary connect flow. -->
				{#if addPanelOpen}
					<div class="acct-addpanel">
						{#if !addAccountOAuthUrl}
							<p class="acct-addhint">A browser window opens to sign in. Paste the code it gives you back here — your current account stays active meanwhile.</p>
							<div class="acct-addrow">
								<button type="button" class="ed-btn primary" onclick={onStartAddAccount} disabled={addAccountLoading}>
									{addAccountLoading ? '…' : 'Open sign-in'}
								</button>
								<button type="button" class="ed-btn" onclick={closeAddPanel}>Cancel</button>
							</div>
						{:else}
							<div class="acct-addfields">
								<input
									type="text"
									class="inp"
									value={addAccountCode}
									oninput={(e) => onAddAccountCodeChange(e.currentTarget.value)}
									placeholder="Paste code from browser"
									aria-label="Authorization code"
								/>
								<input
									type="text"
									class="inp"
									value={addAccountName}
									oninput={(e) => onAddAccountNameChange(e.currentTarget.value)}
									placeholder="Account name (optional)"
									aria-label="Account name"
								/>
							</div>
							<label class="acct-check">
								<input
									type="checkbox"
									checked={addAccountMakeActive}
									onchange={(e) => onAddAccountMakeActiveChange(e.currentTarget.checked)}
								/>
								<span>Make active — switch the app to this account now</span>
							</label>
							<div class="acct-addrow">
								<button type="button" class="ed-btn primary" onclick={onCompleteAddAccount} disabled={addAccountCompleting || !addAccountCode.trim()}>
									{addAccountCompleting ? '…' : 'Complete'}
								</button>
								<button type="button" class="ed-btn" onclick={closeAddPanel}>Cancel</button>
							</div>
						{/if}
					</div>
				{/if}
			{/if}
		</div>
	{/if}

	<!-- Anthropic API key (API-key login method) -->
	<div class="keycard">
		<div class="kc-ic">
			<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
				<path d="M6.5 4h3l5.5 16h-3l-1.2-3.6H6.2L5 20H2L6.5 4zm.4 9.6h3.2L8.5 8.3 6.9 13.6zM15.5 4h3L22 20h-3L15.5 4z"/>
			</svg>
		</div>
		<div class="kc-body">
			<h4>Anthropic API key
				{#if usingApiKeyAuth}<span class="sb ok">Active</span>{:else}<span class="sb off">Not set</span>{/if}
			</h4>
			{#if anthropicKeySet}
				<p>Claude Code authenticates with this key, overriding the connected account. <code class="kc-val">{anthropicKeyMasked}</code></p>
			{:else}
				<p>Authenticate Claude Code with an API key instead of your account. Setting it overrides the connected account; removing it reverts to the account.</p>
			{/if}
		</div>
		<div class="kc-acts">
			{#if anthropicKeySet}
				<button type="button" class="ed-btn danger" onclick={removeAnthropicKey} disabled={anthropicSaving}>Remove</button>
			{:else}
				<input
					type="password"
					autocomplete="off"
					value={anthropicKeyInput}
					oninput={(e) => (anthropicKeyInput = e.currentTarget.value)}
					placeholder="sk-ant-…"
					class="inp kc-in"
				/>
				<button type="button" class="ed-btn primary" onclick={saveAnthropicKey} disabled={anthropicSaving || !anthropicKeyInput.trim()}>
					{anthropicSaving ? '…' : 'Use this key'}
				</button>
			{/if}
		</div>
	</div>

	{#if anthropicError}<div class="kc-foot err" role="alert">{anthropicError}</div>{/if}
	{#if anthropicSuccess}<div class="kc-foot ok">{anthropicSuccess}</div>{/if}
</div>

<!-- Media & tools -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Image />Media &amp; tools</h3>
		<p>Keys for the image, video, audio and 3D generation tools.</p>
	</div>

	<!-- fal.ai key -->
	<div class="keycard">
		<div class="kc-ic">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
				<rect x="3" y="3" width="18" height="18" rx="2"/>
				<circle cx="9" cy="9" r="2"/>
				<path d="m21 15-3.6-3.6a2 2 0 0 0-2.8 0L6 20"/>
			</svg>
		</div>
		<div class="kc-body">
			<h4>fal.ai API key
				{#if falKeySet}<span class="sb ok">Set</span>{:else}<span class="sb off">Optional</span>{/if}
			</h4>
			{#if falKeySet}
				<p>One key powers every AI media tool. <code class="kc-val">{falKeyMasked}</code></p>
			{:else}
				<p>Image, video, audio &amp; 3D generation &amp; editing tools.</p>
			{/if}
		</div>
		<div class="kc-acts">
			{#if falKeySet}
				<button type="button" class="ed-btn danger" onclick={removeFalKey} disabled={falSaving}>Remove</button>
			{:else}
				<input
					type="password"
					autocomplete="off"
					value={falKeyInput}
					oninput={(e) => (falKeyInput = e.currentTarget.value)}
					placeholder="Enter your fal.ai API key"
					class="inp kc-in"
				/>
				<button type="button" class="ed-btn primary" onclick={saveFalKey} disabled={falSaving || !falKeyInput.trim()}>
					{falSaving ? '…' : 'Save'}
				</button>
			{/if}
		</div>
	</div>

	{#if falError}<div class="kc-foot err" role="alert">{falError}</div>{/if}
	{#if falSuccess}<div class="kc-foot ok">{falSuccess}</div>{/if}
</div>

<!-- Integrations -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Plug />Integrations</h3>
		<p>Connect external services used by sessions and projects.</p>
	</div>

	<!-- GitHub -->
	<div class="keycard">
		<div class="kc-ic">
			<svg viewBox="0 0 24 24" fill="currentColor">
				<path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
			</svg>
		</div>
		<div class="kc-body">
			<h4>GitHub
				{#if githubUser}<span class="sb ok">@{githubUser}</span>{:else}<span class="sb off">Not connected</span>{/if}
			</h4>
			<p>Clone, branch and push project repositories.</p>
		</div>
		<div class="kc-acts">
			{#if githubUser}
				<button type="button" class="ed-btn danger" onclick={onDisconnectGithub}>Disconnect</button>
			{:else}
				<input
					type="password"
					autocomplete="off"
					value={githubToken}
					oninput={(e) => onGithubTokenChange(e.currentTarget.value)}
					placeholder="Personal access token"
					class="inp kc-in"
				/>
				<button type="button" class="ed-btn primary" onclick={onConnectGithub} disabled={githubLoginLoading || !githubToken}>
					{githubLoginLoading ? '…' : 'Connect'}
				</button>
			{/if}
		</div>
	</div>
</div>

<style>
	.keycard {
		display: flex;
		gap: 14px;
		align-items: flex-start;
		padding: 14px 18px;
		border-bottom: 1px solid var(--border);
	}
	.keycard:last-child {
		border-bottom: none;
	}
	.kc-ic {
		flex-shrink: 0;
		width: 38px;
		height: 38px;
		border-radius: 10px;
		display: grid;
		place-items: center;
		background: var(--muted);
		color: var(--foreground);
	}
	.kc-ic :global(svg) {
		width: 20px;
		height: 20px;
	}
	.kc-body {
		flex: 1;
		min-width: 0;
	}
	.kc-body h4 {
		margin: 1px 0 3px;
		font-size: 13.5px;
		font-weight: 600;
		color: var(--foreground);
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.kc-body p {
		margin: 0;
		font-size: 12px;
		color: var(--muted-foreground);
		line-height: 1.5;
	}
	.kc-val {
		font-family: var(--font-mono, monospace);
		font-size: 11.5px;
		color: var(--muted-foreground);
	}
	/* The "Authenticated" status word reads green to match the "Active" badge
	   (same --success token). Masked key values keep the muted .kc-val color. */
	.kc-val.authed {
		color: var(--success, #16a34a);
	}
	.kc-acts {
		flex-shrink: 0;
		display: flex;
		gap: 8px;
		align-items: center;
		/* Vertically center the action column against the taller left body
		   (the card itself stays flex-start so the icon aligns with the title). */
		align-self: center;
		flex-wrap: wrap;
		justify-content: flex-end;
	}
	.kc-acts .kc-in {
		width: 200px;
		min-width: 0;
	}
	.sb {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 2px 7px;
		border-radius: 999px;
		line-height: 1.5;
	}
	.sb.ok {
		color: var(--success, #16a34a);
		background: color-mix(in oklch, var(--success, #16a34a) 14%, transparent);
	}
	.sb.off {
		color: var(--muted-foreground);
		background: var(--muted);
	}
	.sb.warn {
		color: var(--warning, #d97706);
		background: color-mix(in oklch, var(--warning, #d97706) 16%, transparent);
	}
	/* Re-authenticate keeps its icon compact and aligned with the label. */
	.kc-reauth :global(svg) {
		width: 14px;
		height: 14px;
	}
	.kc-foot {
		padding: 8px 18px 12px;
		font-size: 12.5px;
	}
	.kc-foot.ok {
		color: var(--success, #16a34a);
	}
	.kc-foot.err {
		color: var(--destructive);
	}
	/* Expired-token warning: icon + wrapped explanatory text. */
	.kc-foot.warn {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		color: var(--warning, #d97706);
		line-height: 1.5;
	}
	.kc-foot.warn :global(svg) {
		width: 16px;
		height: 16px;
		flex-shrink: 0;
		margin-top: 1px;
	}

	/* ===== Multiple Claude accounts ===== */
	.acct-block {
		padding: 4px 18px 14px;
		border-bottom: 1px solid var(--border);
	}
	.acct-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin: 6px 0 8px;
	}
	.acct-head h4 {
		margin: 0;
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: var(--muted-foreground);
	}
	.acct-add-btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.acct-add-btn :global(svg) {
		width: 14px;
		height: 14px;
	}
	.acct-locked {
		padding: 10px 0 2px;
	}
	.acct-empty {
		font-size: 12px;
		color: var(--muted-foreground);
		padding: 6px 0 4px;
	}
	.acct-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.acct-row {
		display: flex;
		gap: 12px;
		align-items: center;
		padding: 10px 12px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--muted);
	}
	.acct-row.active {
		border-color: color-mix(in oklch, var(--success, #16a34a) 45%, var(--border));
		background: color-mix(in oklch, var(--success, #16a34a) 7%, var(--muted));
	}
	.acct-ic {
		flex-shrink: 0;
		width: 32px;
		height: 32px;
		border-radius: 8px;
		display: grid;
		place-items: center;
		background: var(--background);
		color: var(--foreground);
		font-weight: 600;
		font-size: 14px;
	}
	.acct-body {
		flex: 1;
		min-width: 0;
	}
	.acct-name {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.acct-title {
		font-size: 13px;
		font-weight: 600;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
	}
	.acct-meta {
		margin: 2px 0 0;
		font-size: 11.5px;
		color: var(--muted-foreground);
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}
	.acct-dot {
		opacity: 0.5;
	}
	.sb.acct-sync {
		display: inline-flex;
		align-items: center;
		gap: 4px;
	}
	.sb.acct-sync :global(svg) {
		width: 11px;
		height: 11px;
	}
	.acct-acts {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.acct-sm {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 5px 10px;
		font-size: 12px;
	}
	.acct-sm :global(svg) {
		width: 13px;
		height: 13px;
	}
	.acct-confirm {
		font-size: 12px;
		color: var(--warning, #d97706);
		font-weight: 600;
	}
	.acct-iconbtn {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--background);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 0.15s, border-color 0.15s, background 0.15s;
	}
	.acct-iconbtn :global(svg) {
		width: 15px;
		height: 15px;
	}
	.acct-iconbtn:hover {
		color: var(--foreground);
		border-color: var(--foreground);
	}
	.acct-iconbtn.ok:hover {
		color: var(--success, #16a34a);
		border-color: var(--success, #16a34a);
	}
	.acct-iconbtn.danger:hover {
		color: var(--destructive);
		border-color: var(--destructive);
	}
	.acct-iconbtn:focus-visible {
		outline: 2px solid var(--ring, var(--foreground));
		outline-offset: 1px;
	}
	.acct-rename {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.acct-rename-in {
		flex: 1;
		min-width: 0;
		max-width: 280px;
	}
	.acct-addpanel {
		margin-top: 10px;
		padding: 12px;
		border: 1px dashed var(--border);
		border-radius: 10px;
		background: var(--muted);
	}
	.acct-addhint {
		margin: 0 0 10px;
		font-size: 12px;
		color: var(--muted-foreground);
		line-height: 1.5;
	}
	.acct-addfields {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.acct-addfields .inp {
		width: 100%;
	}
	.acct-check {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 10px 0;
		font-size: 12px;
		color: var(--foreground);
		cursor: pointer;
	}
	.acct-check input {
		width: 15px;
		height: 15px;
		accent-color: var(--success, #16a34a);
	}
	.acct-addrow {
		display: flex;
		gap: 8px;
	}
	/* Auto-switch toggle row: label + hint on the left, .switch control on the
	   right. Mirrors the .acct-row rhythm and reuses the shared .switch styling. */
	.acct-autoswitch {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		margin-top: 10px;
		padding: 10px 12px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--muted);
	}
	.acct-autoswitch-lab {
		min-width: 0;
	}
	.acct-autoswitch-lab h5 {
		margin: 0;
		font-size: 13px;
		font-weight: 600;
		color: var(--foreground);
	}
	.acct-autoswitch-lab p {
		margin: 2px 0 0;
		font-size: 11.5px;
		line-height: 1.5;
		color: var(--muted-foreground);
	}
	/* Switch-in-progress spinner (lucide Loader2). */
	:global(.acct-sm .spin) {
		animation: acct-spin 0.8s linear infinite;
	}
	@keyframes acct-spin {
		to { transform: rotate(360deg); }
	}
	@media (prefers-reduced-motion: reduce) {
		:global(.acct-sm .spin) {
			animation: none;
		}
	}

	@media (max-width: 560px) {
		.acct-row {
			flex-wrap: wrap;
		}
		.acct-acts {
			width: 100%;
			justify-content: flex-start;
			padding-left: 44px;
		}
		/* Let the card wrap so the action area drops onto its own full-width
		   row. Without this the flex-shrink:0 actions column demands 100% width
		   in the same row and crushes the body text to one word per line. */
		.keycard {
			flex-wrap: wrap;
		}
		.kc-acts {
			width: 100%;
			justify-content: flex-start;
		}
		.kc-acts .kc-in {
			width: 100%;
		}
	}
</style>

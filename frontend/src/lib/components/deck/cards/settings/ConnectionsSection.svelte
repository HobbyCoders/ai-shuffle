<script lang="ts">
	import { onMount } from 'svelte';
	import { KeyRound, Plug, Info, Image, AlertTriangle, RefreshCw } from 'lucide-svelte';
	import { claudeAuthenticated, claudeTokenValid } from '$lib/stores/auth.svelte';

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
		onConnectGithub,
		onDisconnectGithub,
	}: Props = $props();

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
				{#if !tokenExpired}<span class="kc-val">Authenticated</span>{/if}
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
	.kc-acts {
		flex-shrink: 0;
		display: flex;
		gap: 8px;
		align-items: center;
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

	@media (max-width: 560px) {
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

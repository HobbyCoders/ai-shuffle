<script lang="ts">
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { onMount } from 'svelte';
	import { claudeAuthenticated, githubAuthenticated } from '$lib/stores/auth';
	import './settings-shared.css';

	interface Props {
		expanded: boolean;
		onToggle: () => void;
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
		onCompleteClaudeOAuth: () => void;
		onConnectGithub: () => void;
		onDisconnectGithub: () => void;
	}

	let {
		expanded,
		onToggle,
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
		onCompleteClaudeOAuth,
		onConnectGithub,
		onDisconnectGithub,
	}: Props = $props();

	// ----- Anthropic API key (self-contained: this section owns its state) -----
	// Enables the direct-HTTP path for one-shot AI generation (commit msgs,
	// PR descriptions, branch names). Without it, those calls fall through
	// to the slower SDK subprocess path.
	let anthropicKeySet = $state(false);
	let anthropicKeyMasked = $state('');
	let anthropicKeyInput = $state('');
	let anthropicSaving = $state(false);
	let anthropicError = $state('');
	let anthropicSuccess = $state('');

	async function loadAnthropicKey() {
		try {
			const res = await fetch('/api/v1/settings/integrations', { credentials: 'include' });
			if (!res.ok) return;
			const data = await res.json();
			anthropicKeySet = !!data.anthropic_api_key_set;
			anthropicKeyMasked = data.anthropic_api_key_masked ?? '';
		} catch (e) {
			// Silent — non-critical, user can retry by reopening settings
			console.warn('Failed to load Anthropic key status', e);
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
			anthropicSuccess = 'Saved';
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
			anthropicSuccess = 'Removed';
			setTimeout(() => (anthropicSuccess = ''), 2500);
		} catch (e) {
			anthropicError = e instanceof Error ? e.message : 'Network error';
		} finally {
			anthropicSaving = false;
		}
	}

	// Fetch on mount. No reactivity needed — key status only changes via this
	// component's own save/remove actions.
	onMount(() => {
		loadAnthropicKey();
	});
</script>

<section class="section">
	<button class="section-header" onclick={onToggle}>
		<h3>Connections</h3>
		<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if expanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<!-- Claude Code -->
			<div class="connection-row">
				<div class="connection-info">
					<div class="connection-icon claude">
						<svg viewBox="0 0 24 24" fill="currentColor">
							<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
						</svg>
					</div>
					<div>
						<span class="connection-name">Claude Code</span>
						{#if $claudeAuthenticated}
							<span class="badge success">Connected</span>
						{/if}
					</div>
				</div>
				<div class="connection-actions">
					{#if $claudeAuthenticated}
						<span class="connected-text">Authenticated</span>
					{:else if claudeOAuthUrl}
						<div class="oauth-flow">
							<input
								type="text"
								value={claudeAuthCode}
								oninput={(e) => onClaudeAuthCodeChange(e.currentTarget.value)}
								placeholder="Paste code from browser"
								class="input compact"
							/>
							<button
								onclick={onCompleteClaudeOAuth}
								disabled={claudeCompletingLogin || !claudeAuthCode}
								class="btn primary compact"
							>
								{claudeCompletingLogin ? '...' : 'Complete'}
							</button>
						</div>
					{:else}
						<button
							onclick={onStartClaudeOAuth}
							disabled={claudeLoginLoading}
							class="btn primary compact"
						>
							{claudeLoginLoading ? '...' : 'Connect'}
						</button>
					{/if}
				</div>
			</div>

			<!-- Anthropic API key — drives fast HTTP path for AI generation -->
			<div class="connection-row">
				<div class="connection-info">
					<div class="connection-icon anthropic">
						<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
							<path d="M6.5 4h3l5.5 16h-3l-1.2-3.6H6.2L5 20H2L6.5 4zm.4 9.6h3.2L8.5 8.3 6.9 13.6zM15.5 4h3L22 20h-3L15.5 4z"/>
						</svg>
					</div>
					<div>
						<span class="connection-name">Anthropic API</span>
						{#if anthropicKeySet}
							<span class="badge success">Connected</span>
						{:else}
							<span class="badge muted">Optional — speeds up AI generation</span>
						{/if}
					</div>
				</div>
				<div class="connection-actions">
					{#if anthropicKeySet}
						<span class="connected-text"><code>{anthropicKeyMasked}</code></span>
						<button
							onclick={removeAnthropicKey}
							disabled={anthropicSaving}
							class="btn danger compact"
						>
							Remove
						</button>
					{:else}
						<div class="oauth-flow">
							<input
								type="password"
								autocomplete="off"
								value={anthropicKeyInput}
								oninput={(e) => (anthropicKeyInput = e.currentTarget.value)}
								placeholder="sk-ant-..."
								class="input compact"
							/>
							<button
								onclick={saveAnthropicKey}
								disabled={anthropicSaving || !anthropicKeyInput.trim()}
								class="btn primary compact"
							>
								{anthropicSaving ? '...' : 'Save'}
							</button>
						</div>
					{/if}
				</div>
			</div>
			{#if anthropicError}
				<div class="connection-row" role="alert">
					<span class="error-text">{anthropicError}</span>
				</div>
			{/if}
			{#if anthropicSuccess}
				<div class="connection-row">
					<span class="success-text">{anthropicSuccess}</span>
				</div>
			{/if}

			<!-- GitHub -->
			<div class="connection-row">
				<div class="connection-info">
					<div class="connection-icon github">
						<svg viewBox="0 0 24 24" fill="currentColor">
							<path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
						</svg>
					</div>
					<div>
						<span class="connection-name">GitHub</span>
						{#if githubUser}
							<span class="badge success">@{githubUser}</span>
						{/if}
					</div>
				</div>
				<div class="connection-actions">
					{#if githubUser}
						<button onclick={onDisconnectGithub} class="btn danger compact">Disconnect</button>
					{:else}
						<div class="oauth-flow">
							<input
								type="password"
								autocomplete="off"
								value={githubToken}
								oninput={(e) => onGithubTokenChange(e.currentTarget.value)}
								placeholder="Personal access token"
								class="input compact"
							/>
							<button
								onclick={onConnectGithub}
								disabled={githubLoginLoading || !githubToken}
								class="btn primary compact"
							>
								{githubLoginLoading ? '...' : 'Connect'}
							</button>
						</div>
					{/if}
				</div>
			</div>
		</div>
	{/if}
</section>

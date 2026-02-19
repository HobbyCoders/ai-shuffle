<script lang="ts">
	/**
	 * ConnectionSettings - Claude Code OAuth and GitHub PAT connections
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { auth, claudeAuthenticated, githubAuthenticated } from '$lib/stores/auth';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount } from 'svelte';

	let expanded = $state(false);

	// Auth state
	let githubToken = $state('');
	let githubLoginLoading = $state(false);
	let claudeLoginLoading = $state(false);
	let claudeOAuthUrl: string | null = $state(null);
	let claudeOAuthState: string | null = $state(null);
	let claudeAuthCode = $state('');
	let claudeCompletingLogin = $state(false);
	let githubUser: string | null = $state(null);
	let error = $state('');

	onMount(() => {
		loadAuthStatus();
	});

	async function loadAuthStatus() {
		try {
			const ghStatus = await api.get<{authenticated: boolean, user: string | null}>('/auth/github/status');
			githubUser = ghStatus.user;
		} catch (e) {
			console.error('Failed to load GitHub status:', e);
		}
	}

	async function startClaudeOAuth() {
		claudeLoginLoading = true;
		try {
			const result = await api.get<{authorization_url: string, state: string}>('/auth/claude/authorize');
			claudeOAuthUrl = result.authorization_url;
			claudeOAuthState = result.state;
			window.open(result.authorization_url, '_blank', 'width=600,height=700');
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to start OAuth');
		} finally {
			claudeLoginLoading = false;
		}
	}

	async function completeClaudeOAuth() {
		if (!claudeAuthCode.trim() || !claudeOAuthState) return;
		claudeCompletingLogin = true;
		try {
			await api.post('/auth/claude/callback', {
				code: claudeAuthCode,
				state: claudeOAuthState
			});
			claudeOAuthUrl = null;
			claudeOAuthState = null;
			claudeAuthCode = '';
			await auth.checkAuth();
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to complete OAuth');
		} finally {
			claudeCompletingLogin = false;
		}
	}

	async function connectGithub() {
		if (!githubToken.trim()) return;
		githubLoginLoading = true;
		try {
			await api.post('/auth/github/login', { token: githubToken });
			await loadAuthStatus();
			await auth.checkAuth();
			githubToken = '';
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to connect GitHub');
		} finally {
			githubLoginLoading = false;
		}
	}

	async function disconnectGithub() {
		try {
			await api.post('/auth/github/logout');
			githubUser = null;
			await auth.checkAuth();
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to disconnect');
		}
	}
</script>

<section class="section">
	<button class="section-header" onclick={() => expanded = !expanded}>
		<h3>Connections</h3>
		<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if expanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			{#if error}
				<div class="error-msg">{error}</div>
			{/if}

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
								bind:value={claudeAuthCode}
								placeholder="Paste code from browser"
								class="input compact"
							/>
							<button
								onclick={completeClaudeOAuth}
								disabled={claudeCompletingLogin || !claudeAuthCode}
								class="btn primary compact"
							>
								{claudeCompletingLogin ? '...' : 'Complete'}
							</button>
						</div>
					{:else}
						<button
							onclick={startClaudeOAuth}
							disabled={claudeLoginLoading}
							class="btn primary compact"
						>
							{claudeLoginLoading ? '...' : 'Connect'}
						</button>
					{/if}
				</div>
			</div>

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
						<button onclick={disconnectGithub} class="btn danger compact">Disconnect</button>
					{:else}
						<div class="oauth-flow">
							<input
								type="password"
								bind:value={githubToken}
								placeholder="Personal access token"
								class="input compact"
							/>
							<button
								onclick={connectGithub}
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

<style>
	.section {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 12px 16px;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: background 150ms ease;
	}

	.section-header:hover { background: var(--hover-overlay); }
	.section-header:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.section-header h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
	}

	@media (min-width: 640px) {
		.section-header {
			justify-content: center;
			position: relative;
		}
		.section-header .chevron {
			position: absolute;
			right: 16px;
		}
	}

	.chevron {
		width: 20px;
		height: 20px;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.chevron.expanded { transform: rotate(180deg); }

	.section-content {
		padding: 8px 16px 16px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.connection-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 12px;
		background: var(--muted);
		border-radius: var(--radius);
	}

	.connection-info {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.connection-icon {
		width: 36px;
		height: 36px;
		display: flex;
		align-items: center;
		justify-content: center;
		border-radius: var(--radius, 0.75rem);
		color: var(--primary-foreground);
	}

	.connection-icon.claude {
		background: linear-gradient(135deg, var(--warning), color-mix(in oklch, var(--warning) 80%, oklch(0.3 0 0)));
	}

	.connection-icon.github { background: var(--muted-foreground); }

	.connection-icon svg {
		width: 20px;
		height: 20px;
	}

	.connection-name {
		font-weight: 500;
		font-size: 0.875rem;
	}

	.connection-actions {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.oauth-flow {
		display: flex;
		gap: 6px;
		align-items: center;
	}

	.connected-text {
		font-size: 0.8125rem;
		color: var(--success);
	}

	.badge {
		display: inline-flex;
		align-items: center;
		padding: 2px 8px;
		font-size: 0.6875rem;
		font-weight: 600;
		border-radius: 9999px;
		text-transform: uppercase;
		letter-spacing: 0.025em;
	}

	.badge.success {
		background: color-mix(in oklch, var(--success) 15%, transparent);
		color: var(--success);
		border: 1px solid color-mix(in oklch, var(--success) 30%, transparent);
	}

	.input {
		padding: 8px 12px;
		background-color: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		color: var(--foreground);
		font-size: 0.875rem;
		transition: border-color 150ms ease, box-shadow 150ms ease;
	}

	.input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px var(--primary-20);
	}

	.input::placeholder { color: var(--muted-foreground); }

	.input.compact {
		padding: 6px 10px;
		font-size: 0.8125rem;
	}

	.btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 8px 16px;
		border: none;
		border-radius: calc(var(--radius) - 2px);
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.btn.primary {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.btn.primary:hover:not(:disabled) { filter: brightness(1.1); }

	.btn.danger {
		background: var(--destructive);
		color: var(--destructive-foreground);
	}

	.btn.compact {
		padding: 6px 12px;
		font-size: 0.8125rem;
	}

	.error-msg {
		font-size: 0.8125rem;
		color: var(--destructive);
	}
</style>

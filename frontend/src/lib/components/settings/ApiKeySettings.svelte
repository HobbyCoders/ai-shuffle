<script lang="ts">
	/**
	 * ApiKeySettings - Single fal.ai API key management
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount } from 'svelte';

	let expanded = $state(true);

	// fal.ai
	let falApiKey = $state('');
	let falApiKeyMasked = $state('');
	let falApiKeySet = $state(false);
	let savingFalKey = $state(false);
	let removingFalKey = $state(false);
	let falKeySuccess = $state('');
	let falKeyError = $state('');

	onMount(() => {
		loadIntegrationSettings();
	});

	async function loadIntegrationSettings() {
		try {
			const settings = await api.get<{
				fal_api_key_set: boolean;
				fal_api_key_masked: string;
			}>('/settings/integrations');

			falApiKeySet = settings.fal_api_key_set ?? false;
			falApiKeyMasked = settings.fal_api_key_masked || '';
		} catch (e) {
			console.error('Failed to load integration settings:', e);
		}
	}

	async function saveFalApiKey() {
		if (!falApiKey.trim()) {
			falKeyError = 'Please enter an API key';
			return;
		}
		savingFalKey = true;
		falKeyError = '';
		falKeySuccess = '';
		try {
			const result = await api.post<{success: boolean, masked_key: string, valid: boolean}>('/settings/integrations/fal-ai', {
				api_key: falApiKey
			});
			falApiKeyMasked = result.masked_key;
			falApiKeySet = true;
			falApiKey = '';
			falKeySuccess = result.valid ? 'Key saved & verified ✓' : 'Key saved (could not verify)';
			setTimeout(() => falKeySuccess = '', 4000);
		} catch (e: unknown) {
			falKeyError = getErrorMessage(e, 'Failed to save');
		} finally {
			savingFalKey = false;
		}
	}

	async function removeFalApiKey() {
		removingFalKey = true;
		falKeyError = '';
		falKeySuccess = '';
		try {
			await api.delete('/settings/integrations/fal-ai');
			falApiKeySet = false;
			falApiKeyMasked = '';
			falKeySuccess = 'Key removed';
			setTimeout(() => falKeySuccess = '', 3000);
		} catch (e: unknown) {
			falKeyError = getErrorMessage(e, 'Failed to remove');
		} finally {
			removingFalKey = false;
		}
	}
</script>

<section class="section">
	<button class="section-header" onclick={() => expanded = !expanded}>
		<h3>fal.ai API Key</h3>
		<div class="header-right">
			{#if falApiKeySet}
				<span class="status-badge connected">Connected</span>
			{:else}
				<span class="status-badge disconnected">Not Set</span>
			{/if}
			<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<polyline points="6 9 12 15 18 9"/>
			</svg>
		</div>
	</button>
	{#if expanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<p class="description">
				One API key powers all AI tools — image, video, 3D, and audio generation.
			</p>

			<div class="api-key-row">
				{#if falApiKeyMasked}
					<div class="api-key-info">
						<code class="masked-key">{falApiKeyMasked}</code>
					</div>
				{/if}
				<div class="api-key-input">
					<input
						type="password"
						bind:value={falApiKey}
						placeholder={falApiKeySet ? 'Enter new key to replace...' : 'Enter your fal.ai API key'}
						class="input"
						onkeydown={(e) => e.key === 'Enter' && saveFalApiKey()}
					/>
					<button
						onclick={saveFalApiKey}
						disabled={savingFalKey || !falApiKey.trim()}
						class="btn primary compact"
					>
						{savingFalKey ? '...' : 'Save'}
					</button>
					{#if falApiKeySet}
						<button
							onclick={removeFalApiKey}
							disabled={removingFalKey}
							class="btn destructive compact"
						>
							{removingFalKey ? '...' : 'Remove'}
						</button>
					{/if}
				</div>
			</div>

			{#if falKeySuccess}<div class="success-msg">{falKeySuccess}</div>{/if}
			{#if falKeyError}<div class="error-msg">{falKeyError}</div>{/if}
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

	.header-right {
		display: flex;
		align-items: center;
		gap: 8px;
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

	.description {
		margin: 0;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		line-height: 1.5;
	}

	.api-key-row {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.api-key-info {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.masked-key {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		background: var(--secondary);
		padding: 2px 6px;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
	}

	.api-key-input {
		display: flex;
		gap: 6px;
	}

	.api-key-input .input { flex: 1; }

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

	.btn.destructive {
		background: var(--destructive);
		color: var(--destructive-foreground, #fff);
	}

	.btn.destructive:hover:not(:disabled) { filter: brightness(1.1); }

	.btn.compact {
		padding: 6px 12px;
		font-size: 0.8125rem;
	}

	.status-badge {
		display: inline-flex;
		align-items: center;
		padding: 2px 8px;
		font-size: 0.6875rem;
		font-weight: 600;
		border-radius: 9999px;
		text-transform: uppercase;
		letter-spacing: 0.025em;
	}

	.status-badge.connected {
		background: color-mix(in oklch, var(--success, #22c55e) 15%, transparent);
		color: var(--success, #22c55e);
		border: 1px solid color-mix(in oklch, var(--success, #22c55e) 30%, transparent);
	}

	.status-badge.disconnected {
		background: color-mix(in oklch, var(--muted-foreground) 15%, transparent);
		color: var(--muted-foreground);
		border: 1px solid color-mix(in oklch, var(--muted-foreground) 30%, transparent);
	}

	.success-msg {
		font-size: 0.8125rem;
		color: var(--success);
	}

	.error-msg {
		font-size: 0.8125rem;
		color: var(--destructive);
	}
</style>

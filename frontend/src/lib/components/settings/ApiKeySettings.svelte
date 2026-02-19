<script lang="ts">
	/**
	 * ApiKeySettings - OpenAI, Meshy, and Gemini API key management
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount } from 'svelte';

	let expanded = $state(false);

	// OpenAI
	let openaiApiKey = $state('');
	let openaiApiKeyMasked = $state('');
	let savingOpenaiKey = $state(false);
	let openaiKeySuccess = $state('');
	let openaiKeyError = $state('');

	// Meshy
	let meshyApiKey = $state('');
	let meshyApiKeyMasked = $state('');
	let savingMeshyKey = $state(false);
	let meshyKeySuccess = $state('');
	let meshyKeyError = $state('');

	// Gemini
	let geminiApiKey = $state('');
	let geminiApiKeyMasked = $state('');
	let savingGeminiKey = $state(false);
	let geminiKeySuccess = $state('');
	let geminiKeyError = $state('');

	onMount(() => {
		loadIntegrationSettings();
	});

	async function loadIntegrationSettings() {
		try {
			const settings = await api.get<{
				openai_api_key_set: boolean;
				openai_api_key_masked: string;
				meshy_api_key_set: boolean;
				meshy_api_key_masked: string;
				image_api_key_set: boolean;
				image_api_key_masked: string;
			}>('/settings/integrations');

			openaiApiKeyMasked = settings.openai_api_key_masked;
			meshyApiKeyMasked = settings.meshy_api_key_masked || '';
			geminiApiKeyMasked = settings.image_api_key_masked || '';
		} catch (e) {
			console.error('Failed to load integration settings:', e);
		}
	}

	async function saveOpenaiApiKey() {
		if (!openaiApiKey.trim()) {
			openaiKeyError = 'Please enter an API key';
			return;
		}
		savingOpenaiKey = true;
		openaiKeyError = '';
		openaiKeySuccess = '';
		try {
			const result = await api.post<{success: boolean, masked_key: string}>('/settings/integrations/openai', {
				api_key: openaiApiKey
			});
			openaiApiKeyMasked = result.masked_key;
			openaiApiKey = '';
			openaiKeySuccess = 'Saved';
			setTimeout(() => openaiKeySuccess = '', 3000);
		} catch (e: unknown) {
			openaiKeyError = getErrorMessage(e, 'Failed to save');
		} finally {
			savingOpenaiKey = false;
		}
	}

	async function saveMeshyKey() {
		if (!meshyApiKey.trim()) return;
		savingMeshyKey = true;
		meshyKeyError = '';
		meshyKeySuccess = '';
		try {
			await api.post('/meshy/config/api-key', { api_key: meshyApiKey });
			meshyKeySuccess = 'Saved';
			meshyApiKey = '';
			await loadIntegrationSettings();
			setTimeout(() => meshyKeySuccess = '', 3000);
		} catch (e: unknown) {
			meshyKeyError = getErrorMessage(e, 'Failed to save');
		}
		savingMeshyKey = false;
	}

	async function saveGeminiApiKey() {
		if (!geminiApiKey.trim()) return;
		savingGeminiKey = true;
		geminiKeyError = '';
		geminiKeySuccess = '';
		try {
			const result = await api.post<{success: boolean, masked_key: string}>('/settings/integrations/gemini', {
				api_key: geminiApiKey
			});
			geminiApiKeyMasked = result.masked_key;
			geminiKeySuccess = 'Saved';
			geminiApiKey = '';
			await loadIntegrationSettings();
			setTimeout(() => geminiKeySuccess = '', 3000);
		} catch (e: unknown) {
			geminiKeyError = getErrorMessage(e, 'Failed to save');
		}
		savingGeminiKey = false;
	}
</script>

<section class="section">
	<button class="section-header" onclick={() => expanded = !expanded}>
		<h3>API Keys</h3>
		<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if expanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<!-- OpenAI -->
			<div class="api-key-row">
				<div class="api-key-info">
					<span class="api-key-name">OpenAI</span>
					{#if openaiApiKeyMasked}
						<code class="masked-key">{openaiApiKeyMasked}</code>
					{/if}
				</div>
				<div class="api-key-input">
					<input
						type="password"
						bind:value={openaiApiKey}
						placeholder={openaiApiKeyMasked ? 'Enter new key...' : 'sk-...'}
						class="input compact"
					/>
					<button
						onclick={saveOpenaiApiKey}
						disabled={savingOpenaiKey || !openaiApiKey.trim()}
						class="btn primary compact"
					>
						{savingOpenaiKey ? '...' : 'Save'}
					</button>
				</div>
				{#if openaiKeySuccess}<span class="inline-success">{openaiKeySuccess}</span>{/if}
				{#if openaiKeyError}<span class="inline-error">{openaiKeyError}</span>{/if}
			</div>

			<!-- Meshy -->
			<div class="api-key-row">
				<div class="api-key-info">
					<span class="api-key-name">Meshy (3D)</span>
					{#if meshyApiKeyMasked}
						<code class="masked-key">{meshyApiKeyMasked}</code>
					{/if}
				</div>
				<div class="api-key-input">
					<input
						type="password"
						bind:value={meshyApiKey}
						placeholder={meshyApiKeyMasked ? 'Enter new key...' : 'API key'}
						class="input compact"
					/>
					<button
						onclick={saveMeshyKey}
						disabled={savingMeshyKey || !meshyApiKey.trim()}
						class="btn primary compact"
					>
						{savingMeshyKey ? '...' : 'Save'}
					</button>
				</div>
				{#if meshyKeySuccess}<span class="inline-success">{meshyKeySuccess}</span>{/if}
				{#if meshyKeyError}<span class="inline-error">{meshyKeyError}</span>{/if}
			</div>

			<!-- Google Gemini -->
			<div class="api-key-row">
				<div class="api-key-info">
					<span class="api-key-name">Google Gemini</span>
					{#if geminiApiKeyMasked}
						<code class="masked-key">{geminiApiKeyMasked}</code>
					{/if}
				</div>
				<div class="api-key-input">
					<input
						type="password"
						bind:value={geminiApiKey}
						placeholder={geminiApiKeyMasked ? 'Enter new key...' : 'AIza...'}
						class="input compact"
					/>
					<button
						onclick={saveGeminiApiKey}
						disabled={savingGeminiKey || !geminiApiKey.trim()}
						class="btn primary compact"
					>
						{savingGeminiKey ? '...' : 'Save'}
					</button>
				</div>
				{#if geminiKeySuccess}<span class="inline-success">{geminiKeySuccess}</span>{/if}
				{#if geminiKeyError}<span class="inline-error">{geminiKeyError}</span>{/if}
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

	.api-key-row {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 12px;
		background: var(--muted);
		border-radius: var(--radius);
	}

	.api-key-info {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.api-key-name {
		font-weight: 500;
		font-size: 0.875rem;
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

	.btn.compact {
		padding: 6px 12px;
		font-size: 0.8125rem;
	}

	.inline-success {
		font-size: 0.75rem;
		color: var(--success);
		margin-left: 8px;
	}

	.inline-error {
		font-size: 0.75rem;
		color: var(--destructive);
		margin-left: 8px;
	}
</style>

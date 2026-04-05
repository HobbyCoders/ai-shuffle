<script lang="ts">
	/**
	 * AIToolsSettings — Complete AI tools configuration panel
	 *
	 * Orchestrates the full AI settings experience:
	 * 1. API key management (fal.ai)
	 * 2. Category-based tool cards (Image, Video, 3D, Audio)
	 * 3. Model catalog browser (600+ models)
	 *
	 * All state is managed here and passed down to child components.
	 * Replaces the monolithic inline AI tab in SettingsCard.
	 */
	import '../deck/cards/card-design-system.css';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount } from 'svelte';
	import { AI_TOOL_CATEGORIES, getDefaultModelSelections } from '$lib/types/ai-tool-registry';
	import { catalogStore } from '$lib/stores/catalog';
	import ToolCategoryCard from './ToolCategoryCard.svelte';
	import ModelBrowser from './ModelBrowser.svelte';
	import type { CatalogModel } from '$lib/types/ai-models';

	// ─── API Key State ───
	let falApiKey = $state('');
	let falApiKeyMasked = $state('');
	let falApiKeySet = $state(false);
	let savingFalKey = $state(false);
	let removingFalKey = $state(false);
	let falKeySuccess = $state('');
	let falKeyError = $state('');

	// ─── Model & Setting Selections ───
	// Keyed by backend setting names (e.g., 'image_model', 'video_model')
	let modelSelections = $state<Record<string, string>>(getDefaultModelSelections());
	let settingValues = $state<Record<string, string | number>>({});

	// ─── UI State ───
	let activeView = $state<'categories' | 'browser'>('categories');
	let loaded = $state(false);

	// ─── Catalog State ───
	// Reactive counter to trigger re-derivation when catalog loads
	let catalogVersion = $state(0);

	// Catalog models organized by tool type for each ToolCategoryCard
	let catalogModelsByTool = $derived.by(() => {
		// Access catalogVersion to establish reactive dependency
		void catalogVersion;
		const map: Record<string, CatalogModel[]> = {};
		for (const cat of AI_TOOL_CATEGORIES) {
			for (const tool of cat.tools) {
				map[tool.catalogToolType] = catalogStore.getModelsForTool(tool.catalogToolType);
			}
		}
		return map;
	});

	let catalogSize = $derived.by(() => {
		void catalogVersion;
		return catalogStore.catalogSize;
	});

	// Map from modelSettingKey to catalog tool_type for browser selection
	let selectedModelsForBrowser = $derived.by(() => {
		const map: Record<string, string> = {};
		for (const cat of AI_TOOL_CATEGORIES) {
			for (const tool of cat.tools) {
				map[tool.catalogToolType] = modelSelections[tool.modelSettingKey] || tool.defaultModel;
			}
		}
		return map;
	});

	onMount(() => {
		loadSettings();
		// Load catalog in parallel (non-blocking)
		catalogStore.load().then(() => {
			catalogVersion++;
		});
		// Subscribe to catalog state changes
		const unsubCatalog = catalogStore.subscribe(() => {
			catalogVersion++;
		});
		return () => unsubCatalog();
	});

	async function loadSettings() {
		try {
			const settings = await api.get<Record<string, unknown>>('/settings/integrations');

			// API key
			falApiKeySet = (settings.fal_api_key_set as boolean) ?? false;
			falApiKeyMasked = (settings.fal_api_key_masked as string) || '';

			// Model selections from backend
			const defaults = getDefaultModelSelections();
			for (const [key, defaultVal] of Object.entries(defaults)) {
				modelSelections[key] = (settings[key] as string) || defaultVal;
			}
			// Trigger reactivity
			modelSelections = { ...modelSelections };

			// Per-model setting values
			settingValues = {
				image_aspect_ratio: (settings.image_aspect_ratio as string) || '',
				image_resolution: (settings.image_resolution as string) || '',
				video_aspect_ratio: (settings.video_aspect_ratio as string) || '',
				video_duration: (settings.video_duration as number) || 0,
				video_resolution: (settings.video_resolution as string) || '',
				tts_voice: (settings.tts_voice as string) || 'af_heart',
				tts_speed: (settings.tts_speed as number) || 1.0,
				tts_output_format: (settings.tts_output_format as string) || 'wav',
			};

			loaded = true;
		} catch (e) {
			console.error('Failed to load AI settings:', e);
			loaded = true;
		}
	}

	// ─── API Key handlers ───
	async function saveFalApiKey() {
		if (!falApiKey.trim()) {
			falKeyError = 'Please enter an API key';
			return;
		}
		savingFalKey = true;
		falKeyError = '';
		falKeySuccess = '';
		try {
			const result = await api.post<{ success: boolean; masked_key: string; valid: boolean }>(
				'/settings/integrations/fal-ai',
				{ api_key: falApiKey }
			);
			falApiKeyMasked = result.masked_key;
			falApiKeySet = true;
			falApiKey = '';
			falKeySuccess = result.valid ? 'Key saved & verified' : 'Key saved (could not verify)';
			setTimeout(() => (falKeySuccess = ''), 4000);
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
			setTimeout(() => (falKeySuccess = ''), 3000);
		} catch (e: unknown) {
			falKeyError = getErrorMessage(e, 'Failed to remove');
		} finally {
			removingFalKey = false;
		}
	}

	// ─── Model/setting change handler (from ToolCategoryCards) ───
	function handleSettingChange(key: string, value: string | number) {
		// Check if it's a model key or a setting key
		if (key in modelSelections) {
			modelSelections[key] = String(value);
			modelSelections = { ...modelSelections };
		} else {
			settingValues[key] = value;
			settingValues = { ...settingValues };
		}
	}

	// ─── Browser model selection ───
	async function handleBrowserSelect(model: CatalogModel, toolType: string) {
		// Find the tool definition for this tool type
		for (const cat of AI_TOOL_CATEGORIES) {
			for (const tool of cat.tools) {
				if (tool.catalogToolType === toolType) {
					try {
						await api.patch<{ success: boolean }>(tool.modelEndpoint, {
							model: model.endpoint_id,
						});
						modelSelections[tool.modelSettingKey] = model.endpoint_id;
						modelSelections = { ...modelSelections };
					} catch (e) {
						console.error('Failed to set default from browser:', e);
					}
					return;
				}
			}
		}
	}
</script>

<div class="ai-tools-settings">
	<!-- View toggle: Categories vs Browser -->
	<div class="view-toggle">
		<button
			class="view-btn"
			class:active={activeView === 'categories'}
			onclick={() => (activeView = 'categories')}
			type="button"
		>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<rect x="3" y="3" width="7" height="7" rx="1"/>
				<rect x="14" y="3" width="7" height="7" rx="1"/>
				<rect x="3" y="14" width="7" height="7" rx="1"/>
				<rect x="14" y="14" width="7" height="7" rx="1"/>
			</svg>
			Tools
		</button>
		<button
			class="view-btn"
			class:active={activeView === 'browser'}
			onclick={() => (activeView = 'browser')}
			type="button"
		>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>
			</svg>
			Browse All{#if catalogSize > 0} ({catalogSize}){/if}
		</button>
	</div>

	{#if activeView === 'categories'}
		<!-- API Key Section -->
		<div class="api-key-card">
			<div class="api-key-header">
				<div class="api-key-title">
					<span class="api-key-label">fal.ai API Key</span>
					{#if falApiKeySet}
						<span class="status-dot connected" title="Connected"></span>
					{:else}
						<span class="status-dot disconnected" title="Not Set"></span>
					{/if}
				</div>
				<span class="api-key-hint">One key powers all AI tools</span>
			</div>

			<div class="api-key-body">
				{#if falApiKeyMasked}
					<code class="masked-key">{falApiKeyMasked}</code>
				{/if}
				<div class="api-key-input-row">
					<input
						type="password"
						autocomplete="off"
						bind:value={falApiKey}
						placeholder={falApiKeySet ? 'Enter new key to replace...' : 'Enter your fal.ai API key'}
						class="input"
						onkeydown={(e) => e.key === 'Enter' && saveFalApiKey()}
					/>
					<button
						onclick={saveFalApiKey}
						disabled={savingFalKey || !falApiKey.trim()}
						class="btn primary compact"
						type="button"
					>
						{savingFalKey ? '...' : 'Save'}
					</button>
					{#if falApiKeySet}
						<button
							onclick={removeFalApiKey}
							disabled={removingFalKey}
							class="btn destructive compact"
							type="button"
						>
							{removingFalKey ? '...' : 'Remove'}
						</button>
					{/if}
				</div>
				{#if falKeySuccess}<span class="feedback-success">{falKeySuccess}</span>{/if}
				{#if falKeyError}<span class="feedback-error">{falKeyError}</span>{/if}
			</div>
		</div>

		<!-- Tool Category Cards -->
		{#if loaded}
			<div class="categories-grid">
				{#each AI_TOOL_CATEGORIES as category (category.id)}
					<ToolCategoryCard
						{category}
						{modelSelections}
						{settingValues}
						{catalogModelsByTool}
						onchange={handleSettingChange}
					/>
				{/each}
			</div>
		{:else}
			<div class="loading-state">
				<div class="spinner"></div>
				<span>Loading AI tools...</span>
			</div>
		{/if}
	{:else}
		<!-- Model Browser view -->
		<div class="browser-wrapper">
			<ModelBrowser
				selectedModels={selectedModelsForBrowser}
				onselect={handleBrowserSelect}
			/>
		</div>
	{/if}
</div>

<style>
	.ai-tools-settings {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	/* === View Toggle === */
	.view-toggle {
		display: flex;
		gap: 2px;
		padding: 3px;
		background: var(--muted);
		border-radius: var(--radius);
		border: 1px solid var(--border);
	}

	.view-btn {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 7px 14px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius) - 3px);
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.view-btn:hover {
		color: var(--foreground);
	}

	.view-btn.active {
		background: var(--card);
		color: var(--foreground);
		box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
	}

	.view-btn svg {
		width: 15px;
		height: 15px;
	}

	/* === API Key Card === */
	.api-key-card {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.api-key-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 16px;
		border-bottom: 1px solid var(--border);
	}

	.api-key-title {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.api-key-label {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.status-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex-shrink: 0;
	}

	.status-dot.connected {
		background: var(--success, #22c55e);
		box-shadow: 0 0 4px var(--success, #22c55e);
	}

	.status-dot.disconnected {
		background: var(--muted-foreground);
	}

	.api-key-hint {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.api-key-body {
		padding: 12px 16px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.masked-key {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		background: var(--muted);
		padding: 2px 6px;
		border-radius: 4px;
		font-family: monospace;
		align-self: flex-start;
	}

	.api-key-input-row {
		display: flex;
		gap: 6px;
	}

	.api-key-input-row .input {
		flex: 1;
	}

	/* === Shared Styles === */
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

	.input::placeholder {
		color: var(--muted-foreground);
	}

	.btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 8px 16px;
		border: none;
		border-radius: calc(var(--radius) - 2px);
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
		white-space: nowrap;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.btn.primary {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.btn.primary:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.btn.destructive {
		background: var(--destructive);
		color: var(--destructive-foreground, #fff);
	}

	.btn.destructive:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.btn.compact {
		padding: 6px 12px;
		font-size: 0.8125rem;
	}

	.feedback-success {
		font-size: 0.8125rem;
		color: var(--success, #22c55e);
	}

	.feedback-error {
		font-size: 0.8125rem;
		color: var(--destructive);
	}

	/* === Categories Grid === */
	.categories-grid {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	/* === Browser Wrapper === */
	.browser-wrapper {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 16px;
	}

	/* === Loading === */
	.loading-state {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 10px;
		padding: 40px 16px;
		color: var(--muted-foreground);
		font-size: 0.875rem;
	}

	.spinner {
		width: 20px;
		height: 20px;
		border: 2px solid var(--border);
		border-top-color: var(--primary);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	/* Mobile */
	@media (max-width: 640px) {
		.api-key-header {
			flex-direction: column;
			align-items: flex-start;
			gap: 2px;
		}

		.api-key-input-row {
			flex-direction: column;
		}
	}
</style>

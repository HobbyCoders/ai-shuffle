<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
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
	import '../card-design-system.css';
	import { invalidateSttCapability } from '$lib/services/sttStream';
	import { api } from '$lib/api/client';
	import { onMount } from 'svelte';
	import { AI_TOOL_CATEGORIES, getDefaultModelSelections } from '$lib/types/ai-tool-registry';
	import { catalogStore } from '$lib/stores/catalog';
	import ToolCategoryCard from './ToolCategoryCard.svelte';
	import ModelBrowser from './ModelBrowser.svelte';
	import type { CatalogModel } from '$lib/types/ai-models';

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

	let savedSettings: Record<string, unknown> = {};
	async function loadSettings() {
		const before = { ...modelSelections, ...settingValues };
		const wasLoaded = loaded;
		try {
			const settings = await api.get<Record<string, unknown>>('/settings/integrations');

			// Model selections from backend
			const defaults = getDefaultModelSelections();
			for (const [key, defaultVal] of Object.entries(defaults)) {
				const fresh = (settings[key] as string) || defaultVal;
				if (!wasLoaded || (modelSelections[key] === before[key] && before[key] === savedSettings[key])) modelSelections[key] = fresh;
				savedSettings[key] = fresh;
			}
			// Trigger reactivity
			modelSelections = { ...modelSelections };

			// Per-model setting values
			const freshValues = {
				image_aspect_ratio: (settings.image_aspect_ratio as string) || '',
				image_resolution: (settings.image_resolution as string) || '',
				video_aspect_ratio: (settings.video_aspect_ratio as string) || '',
				video_duration: (settings.video_duration as number) || 0,
				video_resolution: (settings.video_resolution as string) || '',
				tts_voice: (settings.tts_voice as string) || 'af_heart',
				tts_speed: (settings.tts_speed as number) || 1.0,
				tts_output_format: (settings.tts_output_format as string) || 'wav',
			};

			for (const [key, fresh] of Object.entries(freshValues)) {
				if (!wasLoaded || (settingValues[key] === before[key] && before[key] === savedSettings[key])) settingValues[key] = fresh;
				savedSettings[key] = fresh;
			}
			loaded = true;
		} catch (e) {
			console.error('Failed to load AI settings:', e);
			loaded = true;
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
						if (tool.modelSettingKey === 'stt_model') {
							invalidateSttCapability();
						}
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
	watchResources(['settings'], () => loadSettings());
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
			{catalogSize > 0 ? `Browse All (${catalogSize})` : 'Browse All'}
		</button>
	</div>

	{#if activeView === 'categories'}
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
</style>

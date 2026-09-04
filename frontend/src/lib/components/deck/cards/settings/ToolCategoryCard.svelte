<script lang="ts">
	/**
	 * ToolCategoryCard — Self-contained settings card for one AI tool category
	 *
	 * Renders all sub-tools within a category (e.g., Image: text-to-image + editing)
	 * with model pickers, capability badges, and per-model settings panels.
	 * Auto-saves all changes to the backend.
	 */
	import '../card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { api, getErrorMessage } from '$lib/api/client';
	import type {
		ToolCategory,
		ToolDefinition,
		ToolSetting,
		AnyModel,
		CapabilityBadge,
	} from '$lib/types/ai-tool-registry';
	import { ALL_TTS_MODELS, type TTSVoice, type CatalogModel } from '$lib/types/ai-models';
	import { invalidateSttCapability } from '$lib/services/sttStream';
	import ModelPickerGrid from './ModelPickerGrid.svelte';
	import DynamicModelParams from './DynamicModelParams.svelte';

	interface Props {
		category: ToolCategory;
		/** Current model selections keyed by modelSettingKey */
		modelSelections: Record<string, string>;
		/** Current setting values keyed by settingKey */
		settingValues: Record<string, string | number>;
		/** Dynamic catalog models keyed by catalogToolType */
		catalogModelsByTool?: Record<string, CatalogModel[]>;
		/** Called when a model or setting changes */
		onchange: (key: string, value: string | number) => void;
	}

	let {
		category,
		modelSelections,
		settingValues,
		catalogModelsByTool = {},
		onchange,
	}: Props = $props();

	// Total models available (curated + unique catalog)
	let totalModels = $derived.by(() => {
		let count = 0;
		for (const tool of category.tools) {
			const curatedIds = new Set(tool.getModels().map(m => m.id));
			const catalogForTool = catalogModelsByTool[tool.catalogToolType] || [];
			const extraCatalog = catalogForTool.filter(m => !curatedIds.has(m.endpoint_id));
			count += curatedIds.size + extraCatalog.length;
		}
		return count;
	});

	// Which tool sub-section is expanded
	let expandedTool = $state<string | null>(null);

	// Per-tool saving state
	let savingTool = $state<string | null>(null);
	let feedbackMsg = $state<{ type: 'success' | 'error'; text: string } | null>(null);

	function showFeedback(type: 'success' | 'error', text: string) {
		feedbackMsg = { type, text };
		setTimeout(() => feedbackMsg = null, 2500);
	}

	function getSelectedModel(tool: ToolDefinition): AnyModel | undefined {
		const modelId = modelSelections[tool.modelSettingKey] || tool.defaultModel;
		return tool.getModels().find(m => m.id === modelId);
	}

	/** Get the selected catalog model when the selection isn't a curated model */
	function getSelectedCatalogModel(tool: ToolDefinition): CatalogModel | undefined {
		const modelId = modelSelections[tool.modelSettingKey] || tool.defaultModel;
		const catalogForTool = catalogModelsByTool[tool.catalogToolType] || [];
		return catalogForTool.find(m => m.endpoint_id === modelId);
	}

	/** Display name for the selected model (curated or catalog) */
	function getSelectedDisplayName(tool: ToolDefinition): string | undefined {
		const curated = getSelectedModel(tool);
		if (curated) return curated.displayName;
		const catalog = getSelectedCatalogModel(tool);
		return catalog?.display_name;
	}

	function getSelectedModelId(tool: ToolDefinition): string {
		return modelSelections[tool.modelSettingKey] || tool.defaultModel;
	}

	function getCapabilities(tool: ToolDefinition): CapabilityBadge[] {
		const model = getSelectedModel(tool);
		if (!model || !tool.getCapabilities) return [];
		return tool.getCapabilities(model);
	}

	function getSettingOptions(setting: ToolSetting, tool: ToolDefinition): string[] {
		const model = getSelectedModel(tool);
		if (!model || !setting.getOptions) return [];
		return setting.getOptions(model);
	}

	// For TTS: get voice display name
	function getVoiceDisplayName(voiceId: string, tool: ToolDefinition): string {
		const model = getSelectedModel(tool);
		if (!model) return voiceId;
		const ttsModel = ALL_TTS_MODELS.find(m => m.id === model.id);
		const voice = ttsModel?.voices.find((v: TTSVoice) => v.id === voiceId);
		return voice ? `${voice.name}${voice.gender ? ` (${voice.gender})` : ''}` : voiceId;
	}

	// Model change handler
	async function handleModelChange(tool: ToolDefinition, modelId: string) {
		savingTool = tool.id;
		try {
			await api.patch<{ success: boolean }>(tool.modelEndpoint, { model: modelId });

			// Clear stored schema-driven params so the new model starts from its
			// own defaults rather than inheriting the previous model's values.
			try {
				await api.patch('/settings/integrations/model-params', {
					key: tool.modelSettingKey,
					params: {},
				});
			} catch {
				// Non-critical
			}

			// The live-transcription probe caches whether the current STT model can
			// stream; a model swap invalidates that answer immediately.
			if (tool.modelSettingKey === 'stt_model') {
				invalidateSttCapability();
			}

			onchange(tool.modelSettingKey, modelId);

			// Reset per-model settings when model changes (keep sliders at current value)
			for (const setting of tool.settings) {
				if (setting.type !== 'slider') {
					onchange(setting.settingKey, '');
				}
			}
			if (tool.settingsEndpoint && tool.settings.length > 0) {
				const resetPayload: Record<string, string | number> = {};
				for (const setting of tool.settings) {
					if (setting.type === 'slider') continue; // Don't reset sliders on model change
					resetPayload[setting.key] = '';
				}
				if (Object.keys(resetPayload).length > 0) {
					try {
						await api.patch(tool.settingsEndpoint, resetPayload);
					} catch {
						// Non-critical: settings reset failed
					}
				}
			}

			// For TTS: auto-select first voice of new model
			if (tool.id === 'tts') {
				const newModel = ALL_TTS_MODELS.find(m => m.id === modelId);
				if (newModel?.voices.length) {
					const firstVoice = newModel.voices[0].id;
					onchange('tts_voice', firstVoice);
					try {
						await api.patch('/settings/integrations/tts', { voice: firstVoice });
					} catch {
						// Non-critical
					}
				}
			}

			showFeedback('success', 'Updated');
		} catch (e: unknown) {
			showFeedback('error', getErrorMessage(e, 'Failed to update'));
		} finally {
			savingTool = null;
		}
	}

	// Setting change handlers
	async function handlePillChange(tool: ToolDefinition, setting: ToolSetting, value: string) {
		const currentValue = settingValues[setting.settingKey];
		if (value === currentValue) return;
		onchange(setting.settingKey, value);
		if (tool.settingsEndpoint) {
			try {
				await api.patch(tool.settingsEndpoint, { [setting.key]: value });
			} catch (e: unknown) {
				showFeedback('error', getErrorMessage(e, 'Failed to save'));
			}
		}
	}

	async function handleSelectChange(tool: ToolDefinition, setting: ToolSetting, value: string) {
		const currentValue = settingValues[setting.settingKey];
		if (value === currentValue) return;
		onchange(setting.settingKey, value);
		if (tool.settingsEndpoint) {
			try {
				await api.patch(tool.settingsEndpoint, { [setting.key]: value });
			} catch (e: unknown) {
				showFeedback('error', getErrorMessage(e, 'Failed to save'));
			}
		}
	}

	let sliderDebounceTimer: ReturnType<typeof setTimeout> | null = null;

	function handleSliderInput(tool: ToolDefinition, setting: ToolSetting, value: number) {
		onchange(setting.settingKey, value);
		if (sliderDebounceTimer) clearTimeout(sliderDebounceTimer);
		sliderDebounceTimer = setTimeout(async () => {
			if (tool.settingsEndpoint) {
				try {
					await api.patch(tool.settingsEndpoint, { [setting.key]: value });
				} catch (e: unknown) {
					showFeedback('error', getErrorMessage(e, 'Failed to save'));
				}
			}
		}, 300);
	}

	// Category icon SVGs
	const CATEGORY_ICONS: Record<string, string> = {
		image: 'M4 5a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5zm7 8l-2-2-3 4h12l-4-5-3 3z',
		video: 'M15 10l4.553-2.276A1 1 0 0 1 21 8.618v6.764a1 1 0 0 1-1.447.894L15 14M5 18h8a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2z',
		'3d': 'M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z M3.27 6.96L12 12.01l8.73-5.05 M12 22.08V12',
		audio: 'M9 18V5l12-2v13 M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm12-2a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
	};

	// All categories use the same theme-consistent icon color
	const ICON_COLOR = 'var(--foreground)';
</script>

<div class="category-card accent-{category.accent}">
	<!-- Category header -->
	<div class="category-header">
		<div class="category-icon" style="color: {ICON_COLOR}">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
				<path d={CATEGORY_ICONS[category.icon] || CATEGORY_ICONS.image} />
			</svg>
		</div>
		<div class="category-meta">
			<h3 class="category-title">{category.label}</h3>
			<p class="category-desc">{category.description}</p>
		</div>
		<div class="category-count">
			{totalModels} model{totalModels !== 1 ? 's' : ''}
		</div>
	</div>

	<!-- Feedback message -->
	{#if feedbackMsg}
		<div class="feedback feedback-{feedbackMsg.type}">{feedbackMsg.text}</div>
	{/if}

	<!-- Tool sections -->
	<div class="tools-list">
		{#each category.tools as tool (tool.id)}
			{@const isExpanded = expandedTool === tool.id}
			{@const isSaving = savingTool === tool.id}
			{@const selectedModel = getSelectedModel(tool)}
			{@const selectedName = getSelectedDisplayName(tool)}
			{@const caps = getCapabilities(tool)}

			<div class="tool-section" class:expanded={isExpanded}>
				<button
					class="tool-header"
					onclick={() => expandedTool = isExpanded ? null : tool.id}
					type="button"
				>
					<div class="tool-header-left">
						<span class="tool-label">{tool.label}</span>
						{#if selectedName}
							<span class="tool-current">{selectedName}</span>
						{/if}
					</div>
					<div class="tool-header-right">
						{#if caps.length > 0}
							<div class="header-caps">
								{#each caps.slice(0, 2) as cap}
									<span class="header-cap cap-{cap.color}">{cap.label}</span>
								{/each}
							</div>
						{/if}
						<svg class="tool-chevron" class:expanded={isExpanded} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<polyline points="6 9 12 15 18 9"/>
						</svg>
					</div>
				</button>

				{#if isExpanded}
					<div class="tool-content" transition:slide={{ duration: 200, easing: quintOut }}>
						<p class="tool-desc">{tool.description}</p>

						<!-- Model picker -->
						<ModelPickerGrid
							{tool}
							selectedModelId={getSelectedModelId(tool)}
							onselect={(modelId) => handleModelChange(tool, modelId)}
							catalogModels={catalogModelsByTool[tool.catalogToolType] || []}
							disabled={isSaving}
						/>

						<!-- Capability badges (full list) -->
						{#if caps.length > 0}
							<div class="capabilities">
								{#each caps as cap}
									<span class="cap-badge cap-{cap.color}">{cap.label}</span>
								{/each}
							</div>
						{/if}

						<!-- Schema-driven params for uncurated (catalog) models -->
						{#if !selectedModel}
							<DynamicModelParams
								endpointId={getSelectedModelId(tool)}
								persistKey={tool.modelSettingKey}
							/>
						{/if}

						<!-- Per-model settings -->
						{#if tool.settings.length > 0 && selectedModel}
							<div class="settings-panel">
								{#each tool.settings as setting (setting.key)}
									{@const options = getSettingOptions(setting, tool)}
									{#if setting.type === 'pills' && options.length > 0}
										<div class="setting-group">
											<span class="setting-label">{setting.label}</span>
											<div class="pill-group">
												{#each options as opt}
													{@const displayValue = setting.key === 'duration' ? `${opt}s` : opt}
													<button
														class="pill"
														class:active={String(settingValues[setting.settingKey]) === opt}
														onclick={() => handlePillChange(tool, setting, opt)}
														type="button"
													>{displayValue}</button>
												{/each}
											</div>
										</div>
									{:else if setting.type === 'select' && options.length > 0}
										<div class="setting-group">
											<label class="setting-label" for="setting-{tool.id}-{setting.key}">{setting.label}</label>
											<select
												id="setting-{tool.id}-{setting.key}"
												value={String(settingValues[setting.settingKey] || '')}
												onchange={(e) => handleSelectChange(tool, setting, (e.target as HTMLSelectElement).value)}
												class="input select"
											>
												{#each options as opt}
													<option value={opt}>
														{setting.key === 'voice' ? getVoiceDisplayName(opt, tool) : opt.toUpperCase()}
													</option>
												{/each}
											</select>
										</div>
									{:else if setting.type === 'slider' && setting.sliderConfig}
										{@const config = setting.sliderConfig}
										{@const currentValue = Number(settingValues[setting.settingKey]) || config.min}
										<div class="setting-group">
											<label class="setting-label" for="setting-{tool.id}-{setting.key}">
												{setting.label} ({config.format(currentValue)})
											</label>
											<input
												id="setting-{tool.id}-{setting.key}"
												type="range"
												min={config.min}
												max={config.max}
												step={config.step}
												value={currentValue}
												oninput={(e) => handleSliderInput(tool, setting, Number((e.target as HTMLInputElement).value))}
												class="slider"
											/>
										</div>
									{/if}
								{/each}
							</div>
						{/if}
					</div>
				{/if}
			</div>
		{/each}
	</div>
</div>

<style>
	/* === Category Card === */
	.category-card {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
	}

	/* Accent top border — uniform for all categories */
	.category-card[class*='accent-'] { border-top: 2px solid var(--border); }

	/* === Category Header === */
	.category-header {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 14px 16px;
		border-bottom: 1px solid var(--border);
	}

	.category-icon {
		flex-shrink: 0;
	}

	.category-icon svg {
		width: 22px;
		height: 22px;
	}

	.category-meta {
		flex: 1;
		min-width: 0;
	}

	.category-title {
		margin: 0;
		font-size: 1rem;
		font-weight: 700;
		color: var(--foreground);
		line-height: 1.3;
	}

	.category-desc {
		margin: 0;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.4;
	}

	.category-count {
		padding: 2px 8px;
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--muted-foreground);
		background: var(--muted);
		border-radius: 9999px;
		white-space: nowrap;
		flex-shrink: 0;
	}

	/* === Feedback === */
	.feedback {
		padding: 6px 16px;
		font-size: 0.8125rem;
		font-weight: 500;
	}

	.feedback-success { color: var(--success, #22c55e); }
	.feedback-error { color: var(--destructive); }

	/* === Tools List === */
	.tools-list {
		display: flex;
		flex-direction: column;
	}

	.tool-section {
		border-bottom: 1px solid var(--border);
	}

	.tool-section:last-child {
		border-bottom: none;
	}

	/* === Tool Header === */
	.tool-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 10px 16px;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: background 150ms ease;
		gap: 8px;
	}

	.tool-header:hover { background: var(--hover-overlay); }
	.tool-header:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	.tool-header-left {
		display: flex;
		align-items: baseline;
		gap: 8px;
		min-width: 0;
		overflow: hidden;
	}

	.tool-label {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--foreground);
		white-space: nowrap;
	}

	.tool-current {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.tool-header-right {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
	}

	.header-caps {
		display: flex;
		gap: 4px;
	}

	.header-cap {
		padding: 1px 6px;
		font-size: 0.625rem;
		font-weight: 600;
		border-radius: 9999px;
		white-space: nowrap;
	}

	.header-cap[class*='cap-'] {
		background: var(--muted);
		color: var(--muted-foreground);
	}

	.tool-chevron {
		width: 16px;
		height: 16px;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
		flex-shrink: 0;
	}

	.tool-chevron.expanded { transform: rotate(180deg); }

	/* === Tool Content === */
	.tool-content {
		padding: 0 16px 14px;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.tool-desc {
		margin: 0;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		line-height: 1.4;
	}

	/* === Capabilities === */
	.capabilities {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}

	.cap-badge {
		padding: 2px 8px;
		font-size: 0.6875rem;
		font-weight: 600;
		border-radius: 9999px;
		line-height: 1.4;
	}

	.cap-badge[class*='cap-'] {
		background: var(--muted);
		color: var(--muted-foreground);
	}

	/* === Settings Panel === */
	.settings-panel {
		background: var(--muted);
		border-radius: calc(var(--radius) - 2px);
		padding: 12px;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.setting-group {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.setting-label {
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.pill-group {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}

	.pill {
		padding: 4px 10px;
		font-size: 0.75rem;
		font-weight: 500;
		border: 1px solid var(--border);
		border-radius: 9999px;
		background: var(--card);
		color: var(--foreground);
		cursor: pointer;
		transition: all 150ms ease;
		min-height: 28px;
	}

	.pill:hover {
		border-color: var(--ring);
		background: var(--hover-overlay);
	}

	.pill.active {
		background: var(--primary);
		color: var(--primary-foreground);
		border-color: var(--primary);
	}

	.input {
		padding: 8px 12px;
		background-color: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		color: var(--foreground);
		font-size: 0.875rem;
		transition: border-color 150ms ease, box-shadow 150ms ease;
		width: 100%;
	}

	.input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px var(--primary-20);
	}

	.slider {
		width: 100%;
		height: 6px;
		border-radius: 3px;
		background: var(--card);
		appearance: none;
		cursor: pointer;
	}

	.slider::-webkit-slider-thumb {
		appearance: none;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--primary);
		cursor: pointer;
	}

	.slider::-moz-range-thumb {
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--primary);
		cursor: pointer;
		border: none;
	}

	/* Mobile adjustments */
	@media (max-width: 640px) {
		.category-header {
			padding: 12px 14px;
		}

		.tool-content {
			padding: 0 14px 12px;
		}

		.header-caps {
			display: none;
		}
	}
</style>

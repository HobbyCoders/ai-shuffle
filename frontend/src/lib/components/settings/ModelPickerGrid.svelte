<script lang="ts">
	/**
	 * ModelPickerGrid — Visual two-tier model selector
	 *
	 * Shows models in two tiers:
	 * 1. "Recommended" — curated models from ai-models.ts with rich metadata
	 *    (prices, capabilities, aspect ratios, etc.)
	 * 2. "More from fal.ai" — auto-imported catalog models that appear
	 *    automatically when fal.ai adds new models, no code changes needed.
	 *
	 * The selected model summary is always visible. Clicking expands the
	 * full picker with search and both tiers.
	 */
	import type { ToolDefinition, AnyModel, CapabilityBadge } from '$lib/types/ai-tool-registry';
	import type { CatalogModel } from '$lib/types/ai-models';
	import { CATEGORY_LABELS } from '$lib/types/ai-models';

	interface Props {
		tool: ToolDefinition;
		selectedModelId: string;
		onselect: (modelId: string) => void;
		/** Dynamic catalog models for this tool's category (auto-imported from fal.ai) */
		catalogModels?: CatalogModel[];
		disabled?: boolean;
	}

	let {
		tool,
		selectedModelId,
		onselect,
		catalogModels = [],
		disabled = false,
	}: Props = $props();

	let expanded = $state(false);
	let searchQuery = $state('');

	// Curated models from the registry (with rich metadata)
	let curatedModels = $derived(tool.getModels());
	let curatedIds = $derived(new Set(curatedModels.map(m => m.id)));

	// Catalog-only models (not in curated list, auto-imported from fal.ai)
	let extraCatalogModels = $derived(
		catalogModels.filter(m => !curatedIds.has(m.endpoint_id))
	);

	// Search filtering
	let filteredCurated = $derived.by(() => {
		if (!searchQuery.trim()) return curatedModels;
		const q = searchQuery.toLowerCase();
		return curatedModels.filter(m =>
			m.displayName.toLowerCase().includes(q) ||
			m.description.toLowerCase().includes(q) ||
			m.id.toLowerCase().includes(q)
		);
	});

	let filteredCatalog = $derived.by(() => {
		if (!searchQuery.trim()) return extraCatalogModels;
		const q = searchQuery.toLowerCase();
		return extraCatalogModels.filter(m =>
			m.display_name.toLowerCase().includes(q) ||
			m.description.toLowerCase().includes(q) ||
			m.endpoint_id.toLowerCase().includes(q) ||
			m.tags.some(t => t.toLowerCase().includes(q))
		);
	});

	// Find the selected model (could be curated or catalog)
	let selectedCurated = $derived(curatedModels.find(m => m.id === selectedModelId));
	let selectedCatalog = $derived(
		!selectedCurated ? catalogModels.find(m => m.endpoint_id === selectedModelId) : undefined
	);
	let selectedDisplayName = $derived(
		selectedCurated?.displayName ?? selectedCatalog?.display_name ?? 'Select a model'
	);
	let selectedDescription = $derived(
		selectedCurated?.description ?? selectedCatalog?.description ?? ''
	);
	let selectedPrice = $derived(
		selectedCurated ? tool.formatPrice(selectedCurated) : ''
	);

	function selectCuratedModel(model: AnyModel) {
		if (disabled || model.id === selectedModelId) return;
		onselect(model.id);
		expanded = false;
		searchQuery = '';
	}

	function selectCatalogModel(model: CatalogModel) {
		if (disabled || model.endpoint_id === selectedModelId) return;
		onselect(model.endpoint_id);
		expanded = false;
		searchQuery = '';
	}

	function getCapabilities(model: AnyModel): CapabilityBadge[] {
		return tool.getCapabilities?.(model) ?? [];
	}
</script>

<div class="model-picker">
	<!-- Selected model summary (always visible) -->
	<button
		class="selected-model"
		class:disabled
		onclick={() => { if (!disabled) expanded = !expanded; }}
		type="button"
	>
		<div class="selected-info">
			<span class="selected-name">{selectedDisplayName}</span>
			{#if selectedPrice}
				<span class="selected-price">{selectedPrice}</span>
			{/if}
		</div>
		<div class="selected-right">
			{#if extraCatalogModels.length > 0}
				<span class="catalog-count" title="Auto-imported models from fal.ai">
					+{extraCatalogModels.length}
				</span>
			{/if}
			<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
				<polyline points="6 9 12 15 18 9"/>
			</svg>
		</div>
	</button>

	<!-- Description of selected model -->
	{#if selectedDescription && !expanded}
		<p class="selected-desc">{selectedDescription}</p>
	{/if}

	<!-- Expanded picker -->
	{#if expanded}
		<!-- Search bar -->
		{#if curatedModels.length + extraCatalogModels.length > 5}
			<div class="search-wrapper">
				<svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>
				</svg>
				<input
					type="text"
					class="search-input"
					placeholder="Search models..."
					bind:value={searchQuery}
				/>
				{#if searchQuery}
					<button class="clear-btn" onclick={() => searchQuery = ''} type="button">x</button>
				{/if}
			</div>
		{/if}

		<!-- Tier 1: Recommended (curated) models -->
		{#if filteredCurated.length > 0}
			<div class="tier">
				<div class="tier-header">
					<span class="tier-label">Recommended</span>
					<span class="tier-count">{filteredCurated.length}</span>
				</div>
				<div class="model-grid">
					{#each filteredCurated as model (model.id)}
						{@const isSelected = model.id === selectedModelId}
						{@const caps = getCapabilities(model)}
						<button
							class="model-card"
							class:selected={isSelected}
							class:deprecated={'deprecated' in model && model.deprecated}
							onclick={() => selectCuratedModel(model)}
							type="button"
							{disabled}
						>
							<div class="card-header">
								<span class="card-name">{model.displayName}</span>
								{#if isSelected}
									<span class="check-mark">
										<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
											<polyline points="20 6 9 17 4 12"/>
										</svg>
									</span>
								{/if}
							</div>
							<p class="card-desc">{model.description}</p>
							<div class="card-footer">
								<span class="card-price">{tool.formatPrice(model)}</span>
								{#if caps.length > 0}
									<div class="card-caps">
										{#each caps.slice(0, 2) as cap}
											<span class="cap-dot cap-{cap.color}"></span>
										{/each}
									</div>
								{/if}
							</div>
						</button>
					{/each}
				</div>
			</div>
		{/if}

		<!-- Tier 2: Auto-imported catalog models -->
		{#if filteredCatalog.length > 0}
			<div class="tier tier-catalog">
				<div class="tier-header">
					<span class="tier-label">More from fal.ai</span>
					<span class="tier-count">{filteredCatalog.length}</span>
					<span class="tier-auto">auto-imported</span>
				</div>
				<div class="model-grid catalog-grid">
					{#each filteredCatalog as model (model.endpoint_id)}
						{@const isSelected = model.endpoint_id === selectedModelId}
						<button
							class="model-card catalog-card"
							class:selected={isSelected}
							onclick={() => selectCatalogModel(model)}
							type="button"
							title={model.endpoint_id}
							{disabled}
						>
							<div class="card-header">
								<span class="card-name">{model.display_name}</span>
								{#if isSelected}
									<span class="check-mark">
										<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
											<polyline points="20 6 9 17 4 12"/>
										</svg>
									</span>
								{/if}
							</div>
							{#if model.description}
								<p class="card-desc">{model.description}</p>
							{/if}
							<div class="card-footer">
								<span class="card-id">{model.endpoint_id}</span>
							</div>
						</button>
					{/each}
				</div>
			</div>
		{/if}

		<!-- No results -->
		{#if filteredCurated.length === 0 && filteredCatalog.length === 0}
			<div class="no-results">
				No models match "{searchQuery}"
			</div>
		{/if}
	{/if}
</div>

<style>
	.model-picker {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	/* --- Selected model button --- */
	.selected-model {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 14px;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		cursor: pointer;
		transition: all 150ms ease;
		width: 100%;
	}

	.selected-model:hover:not(.disabled) {
		border-color: var(--ring);
	}

	.selected-model:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.selected-model.disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.selected-info {
		display: flex;
		align-items: baseline;
		gap: 10px;
		overflow: hidden;
	}

	.selected-name {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.selected-price {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		white-space: nowrap;
		font-family: monospace;
	}

	.selected-right {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-shrink: 0;
	}

	.catalog-count {
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--muted-foreground);
		background: var(--muted);
		padding: 1px 6px;
		border-radius: 9999px;
	}

	.chevron {
		width: 18px;
		height: 18px;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.chevron.expanded {
		transform: rotate(180deg);
	}

	.selected-desc {
		margin: 0;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		line-height: 1.4;
		padding: 0 2px;
	}

	/* --- Search --- */
	.search-wrapper {
		position: relative;
		display: flex;
		align-items: center;
	}

	.search-icon {
		position: absolute;
		left: 10px;
		width: 14px;
		height: 14px;
		color: var(--muted-foreground);
		pointer-events: none;
	}

	.search-input {
		width: 100%;
		padding: 7px 28px 7px 30px;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		color: var(--foreground);
		font-size: 0.8125rem;
	}

	.search-input:focus {
		outline: none;
		border-color: var(--ring);
	}

	.search-input::placeholder {
		color: var(--muted-foreground);
	}

	.clear-btn {
		position: absolute;
		right: 6px;
		background: none;
		border: none;
		color: var(--muted-foreground);
		cursor: pointer;
		font-size: 0.75rem;
		padding: 2px 4px;
	}

	/* --- Tier headers --- */
	.tier {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.tier-header {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 0 2px;
	}

	.tier-label {
		font-size: 0.6875rem;
		font-weight: 700;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.tier-count {
		font-size: 0.625rem;
		font-weight: 600;
		color: var(--muted-foreground);
		background: var(--muted);
		padding: 0 5px;
		border-radius: 9999px;
		line-height: 1.6;
	}

	.tier-auto {
		font-size: 0.625rem;
		color: var(--muted-foreground);
		opacity: 0.6;
		font-style: italic;
	}

	.tier-catalog {
		border-top: 1px dashed var(--border);
		padding-top: 8px;
	}

	/* --- Model grid --- */
	.model-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
		gap: 6px;
	}

	.catalog-grid {
		max-height: 280px;
		overflow-y: auto;
		padding-right: 2px;
	}

	/* --- Model card --- */
	.model-card {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 10px 12px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		cursor: pointer;
		transition: all 150ms ease;
		text-align: left;
		min-height: 72px;
	}

	.model-card:hover:not(:disabled) {
		border-color: var(--ring);
		background: var(--hover-overlay);
	}

	.model-card:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.model-card.selected {
		border-color: var(--primary);
		background: color-mix(in srgb, var(--primary) 6%, var(--card));
		box-shadow: 0 0 0 1px var(--primary);
	}

	.model-card.deprecated {
		opacity: 0.5;
	}

	.model-card:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Catalog cards are slightly more compact */
	.catalog-card {
		min-height: 56px;
		padding: 8px 10px;
		border-style: dashed;
	}

	.card-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 6px;
	}

	.card-name {
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--foreground);
		line-height: 1.3;
	}

	.check-mark {
		flex-shrink: 0;
	}

	.check-mark svg {
		width: 14px;
		height: 14px;
		color: var(--primary);
	}

	.card-desc {
		margin: 0;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.3;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.card-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-top: auto;
		padding-top: 4px;
	}

	.card-price {
		font-size: 0.6875rem;
		font-family: monospace;
		color: var(--muted-foreground);
	}

	.card-id {
		font-size: 0.625rem;
		font-family: monospace;
		color: var(--muted-foreground);
		opacity: 0.6;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
	}

	.card-caps {
		display: flex;
		gap: 3px;
	}

	.cap-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
	}

	.cap-dot[class*='cap-'] { background: var(--muted-foreground); }

	.no-results {
		padding: 16px;
		text-align: center;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
	}

	/* Mobile */
	@media (max-width: 640px) {
		.model-grid {
			grid-template-columns: 1fr;
		}
	}
</style>

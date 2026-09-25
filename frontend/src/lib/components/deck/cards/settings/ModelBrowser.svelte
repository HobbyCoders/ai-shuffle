<script lang="ts">
	/**
	 * ModelBrowser — Dynamic fal.ai model catalog browser
	 *
	 * Fetches and displays the full fal.ai model catalog from our backend,
	 * which proxies the fal.ai platform API. Supports search, category filtering,
	 * and model selection for each tool type.
	 */
	import '../card-design-system.css';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount } from 'svelte';
	import type { CatalogModel, CatalogCategory } from '$lib/types/ai-models';
	import { CATEGORY_LABELS, OUTPUT_TYPE_ICONS } from '$lib/types/ai-models';

	interface Props {
		/** Currently selected model endpoint_id per tool type */
		selectedModels?: Record<string, string>;
		/** Callback when a model is selected as default */
		onselect?: (model: CatalogModel, toolType: string) => void;
		/**
		 * Tool types a pick can be saved to. When given, a model whose tool type
		 * is not in it (e.g. the catalog's `generic` fallback for unmapped fal.ai
		 * categories) is shown greyed out and cannot be picked.
		 */
		selectableToolTypes?: readonly string[];
	}

	let { selectedModels = {}, onselect, selectableToolTypes }: Props = $props();

	const NO_TOOL_REASON = 'No tool uses this model';

	let models = $state<CatalogModel[]>([]);
	let categories = $state<CatalogCategory[]>([]);
	let loading = $state(true);
	let error = $state('');
	let searchQuery = $state('');
	let activeCategory = $state<string | null>(null);
	let catalogSize = $state(0);

	// Debounced search
	let searchTimeout: ReturnType<typeof setTimeout> | null = null;
	let filteredModels = $derived.by(() => {
		let result = models;
		if (activeCategory) {
			result = result.filter(m => m.category === activeCategory);
		}
		if (searchQuery.trim()) {
			const q = searchQuery.toLowerCase();
			result = result.filter(m =>
				m.display_name.toLowerCase().includes(q) ||
				m.description.toLowerCase().includes(q) ||
				m.endpoint_id.toLowerCase().includes(q) ||
				m.tags.some(t => t.toLowerCase().includes(q))
			);
		}
		return result;
	});

	// Group filtered models by group_key for visual grouping
	let groupedModels = $derived.by(() => {
		const groups = new Map<string, CatalogModel[]>();
		const ungrouped: CatalogModel[] = [];

		for (const model of filteredModels) {
			if (model.group_key) {
				const existing = groups.get(model.group_key) || [];
				existing.push(model);
				groups.set(model.group_key, existing);
			} else {
				ungrouped.push(model);
			}
		}

		return { groups, ungrouped };
	});

	onMount(() => {
		loadCatalog();
	});

	async function loadCatalog() {
		loading = true;
		error = '';
		try {
			const [modelsRes, catsRes] = await Promise.all([
				api.get<{ models: CatalogModel[]; total: number; catalog_size: number }>('/catalog/models'),
				api.get<{ categories: CatalogCategory[]; total_models: number }>('/catalog/categories'),
			]);
			models = modelsRes.models;
			categories = catsRes.categories;
			catalogSize = modelsRes.catalog_size;
		} catch (e) {
			error = getErrorMessage(e, 'Failed to load model catalog');
		} finally {
			loading = false;
		}
	}

	async function refreshCatalog() {
		loading = true;
		error = '';
		try {
			const result = await api.post<{ success: boolean; models_fetched: number; categories: CatalogCategory[] }>('/catalog/refresh', {});
			categories = result.categories;
			catalogSize = result.models_fetched;
			// Reload models with current filters
			await loadModels();
		} catch (e) {
			error = getErrorMessage(e, 'Failed to refresh catalog');
		} finally {
			loading = false;
		}
	}

	async function loadModels() {
		try {
			const params = new URLSearchParams();
			if (activeCategory) params.set('category', activeCategory);
			if (searchQuery.trim()) params.set('q', searchQuery.trim());
			const query = params.toString();
			const res = await api.get<{ models: CatalogModel[]; total: number }>(`/catalog/models${query ? '?' + query : ''}`);
			models = res.models;
		} catch (e) {
			// Keep existing models on filter failure
		}
	}

	function isUnusable(model: CatalogModel): boolean {
		return !!selectableToolTypes && !(model.tool_type && selectableToolTypes.includes(model.tool_type));
	}

	function selectModel(model: CatalogModel) {
		if (isUnusable(model)) return;
		const toolType = model.tool_type || model.output_type || 'image_generation';
		onselect?.(model, toolType);
	}

	function isSelected(model: CatalogModel): boolean {
		// Check all selectedModels values
		return Object.values(selectedModels).includes(model.endpoint_id);
	}

	function getCategoryLabel(category: string): string {
		return CATEGORY_LABELS[category] || category;
	}

	function getOutputIcon(model: CatalogModel): string {
		return OUTPUT_TYPE_ICONS[model.output_type || ''] || '✨';
	}
</script>

<div class="model-browser">
	<!-- Header with search and refresh -->
	<div class="browser-header">
		<div class="search-row">
			<div class="search-input-wrapper">
				<svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>
				</svg>
				<input
					type="text"
					class="search-input"
					placeholder="Search {catalogSize} models..."
					bind:value={searchQuery}
				/>
				{#if searchQuery}
					<button class="clear-search" onclick={() => searchQuery = ''}>✕</button>
				{/if}
			</div>
			<button class="refresh-btn" onclick={refreshCatalog} disabled={loading} title="Refresh catalog from fal.ai">
				<svg class="refresh-icon" class:spinning={loading} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
					<path d="M3 3v5h5"/>
					<path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/>
					<path d="M21 21v-5h-5"/>
				</svg>
			</button>
		</div>

		<!-- Category filters -->
		<div class="category-filters">
			<button
				class="category-chip"
				class:active={activeCategory === null}
				onclick={() => activeCategory = null}
			>
				All ({catalogSize})
			</button>
			{#each categories as cat}
				<button
					class="category-chip"
					class:active={activeCategory === cat.category}
					onclick={() => activeCategory = activeCategory === cat.category ? null : cat.category}
				>
					{getCategoryLabel(cat.category)} ({cat.count})
				</button>
			{/each}
		</div>
	</div>

	<!-- Error state -->
	{#if error}
		<div class="error-state">
			<p>{error}</p>
			<button class="retry-btn" onclick={loadCatalog}>Retry</button>
		</div>
	{/if}

	<!-- Loading state -->
	{#if loading && models.length === 0}
		<div class="loading-state">
			<div class="spinner"></div>
			<p>Loading model catalog...</p>
		</div>
	{/if}

	<!-- Model grid -->
	{#if filteredModels.length > 0}
		<div class="results-info">
			{filteredModels.length} model{filteredModels.length !== 1 ? 's' : ''}
			{#if searchQuery || activeCategory}
				<span class="filter-note">(filtered)</span>
			{/if}
		</div>
		<div class="model-grid">
			{#each filteredModels as model (model.endpoint_id)}
				{@const unusable = isUnusable(model)}
				<button
					class="model-card"
					class:selected={isSelected(model)}
					class:unusable
					aria-disabled={unusable ? 'true' : undefined}
					onclick={() => selectModel(model)}
					title={unusable ? `${model.endpoint_id} — ${NO_TOOL_REASON}` : model.endpoint_id}
				>
					<div class="model-thumb" class:placeholder={!model.thumbnail_url}>
						{#if model.thumbnail_url}
							<img
								src={model.thumbnail_url}
								alt={model.display_name}
								loading="lazy"
								referrerpolicy="no-referrer"
								onerror={(e) => {
									const img = e.target as HTMLImageElement;
									img.style.display = 'none';
									img.parentElement?.classList.add('placeholder');
								}}
							/>
						{/if}
						<div class="thumb-gradient"></div>
						{#if !model.thumbnail_url}
							<span class="thumb-icon">{getOutputIcon(model)}</span>
						{/if}
					</div>
					<div class="model-info">
						<div class="model-name">{model.display_name}</div>
						<div class="model-category">{getCategoryLabel(model.category)}</div>
						{#if model.description}
							<div class="model-desc">{model.description}</div>
						{/if}
						<div class="model-id">{model.endpoint_id}</div>
					</div>
					{#if isSelected(model)}
						<div class="selected-badge">✓ Default</div>
					{:else if unusable}
						<div class="unusable-badge">{NO_TOOL_REASON}</div>
					{/if}
				</button>
			{/each}
		</div>
	{:else if !loading}
		<div class="empty-state">
			{#if searchQuery || activeCategory}
				<p>No models match your search. Try different keywords or clear filters.</p>
			{:else}
				<p>No models in catalog. Click refresh to fetch from fal.ai.</p>
			{/if}
		</div>
	{/if}
</div>

<style>
	.model-browser {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.browser-header {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.search-row {
		display: flex;
		gap: 8px;
		align-items: center;
	}

	.search-input-wrapper {
		flex: 1;
		position: relative;
		display: flex;
		align-items: center;
	}

	.search-icon {
		position: absolute;
		left: 10px;
		width: 16px;
		height: 16px;
		color: var(--muted-foreground);
		pointer-events: none;
	}

	.search-input {
		width: 100%;
		padding: 8px 32px 8px 34px;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		color: var(--foreground);
		font-size: 0.875rem;
		transition: border-color 150ms ease;
	}

	.search-input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px var(--primary-20);
	}

	.search-input::placeholder {
		color: var(--muted-foreground);
	}

	.clear-search {
		position: absolute;
		right: 8px;
		background: none;
		border: none;
		color: var(--muted-foreground);
		cursor: pointer;
		font-size: 0.75rem;
		padding: 2px 4px;
		border-radius: 2px;
	}

	.clear-search:hover {
		color: var(--foreground);
	}

	.refresh-btn {
		padding: 8px;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 150ms ease;
		flex-shrink: 0;
	}

	.refresh-btn:hover:not(:disabled) {
		color: var(--foreground);
		border-color: var(--ring);
	}

	.refresh-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.refresh-icon {
		width: 16px;
		height: 16px;
		display: block;
	}

	.refresh-icon.spinning {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	.category-filters {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}

	.category-chip {
		padding: 4px 10px;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: 9999px;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		cursor: pointer;
		white-space: nowrap;
		transition: all 150ms ease;
	}

	.category-chip:hover {
		border-color: var(--ring);
		color: var(--foreground);
	}

	.category-chip.active {
		background: var(--primary);
		color: var(--primary-foreground);
		border-color: var(--primary);
	}

	.results-info {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		padding: 0 2px;
	}

	.filter-note {
		opacity: 0.7;
	}

	.model-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
		gap: 8px;
		max-height: 420px;
		overflow-y: auto;
		padding-right: 4px;
	}

	.model-card {
		display: flex;
		flex-direction: column;
		padding: 0;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		cursor: pointer;
		transition: all 150ms ease;
		text-align: left;
		overflow: hidden;
		position: relative;
	}

	.model-card:hover {
		border-color: var(--ring);
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
		transform: translateY(-1px);
	}

	.model-card.selected {
		border-color: var(--primary);
		box-shadow: 0 0 0 2px var(--primary-20);
	}

	.model-card.unusable {
		opacity: 0.55;
		cursor: not-allowed;
	}

	.model-card.unusable:hover {
		border-color: var(--border);
		box-shadow: none;
		transform: none;
	}

	.model-card.unusable:hover .model-thumb img {
		transform: none;
	}

	.model-thumb {
		width: 100%;
		height: 100px;
		overflow: hidden;
		display: flex;
		align-items: center;
		justify-content: center;
		position: relative;
		background: var(--muted);
	}

	.model-thumb img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		transition: transform 300ms ease;
	}

	.model-card:hover .model-thumb img {
		transform: scale(1.05);
	}

	.thumb-gradient {
		position: absolute;
		inset: 0;
		background: linear-gradient(to top, rgba(0, 0, 0, 0.45) 0%, transparent 50%);
		pointer-events: none;
		z-index: 1;
	}

	.model-thumb.placeholder {
		background: linear-gradient(
			135deg,
			var(--muted) 0%,
			color-mix(in srgb, var(--primary) 15%, var(--muted)) 25%,
			var(--muted) 50%,
			color-mix(in srgb, var(--primary) 10%, var(--muted)) 75%,
			var(--muted) 100%
		);
		background-size: 400% 400%;
		animation: shimmer-banner 8s ease infinite;
	}

	.model-thumb.placeholder .thumb-gradient {
		display: none;
	}

	@keyframes shimmer-banner {
		0% { background-position: 0% 50%; }
		50% { background-position: 100% 50%; }
		100% { background-position: 0% 50%; }
	}

	.thumb-icon {
		font-size: 2rem;
		opacity: 0.4;
		z-index: 2;
	}

	.model-info {
		padding: 8px 10px;
		display: flex;
		flex-direction: column;
		gap: 2px;
		flex: 1;
	}

	.model-name {
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--foreground);
		line-height: 1.3;
	}

	.model-category {
		font-size: 0.6875rem;
		color: var(--primary);
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.model-desc {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.3;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.model-id {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		font-family: monospace;
		opacity: 0.6;
		word-break: break-all;
	}

	.selected-badge {
		position: absolute;
		top: 6px;
		right: 6px;
		padding: 2px 8px;
		background: var(--primary);
		color: var(--primary-foreground);
		font-size: 0.6875rem;
		font-weight: 600;
		border-radius: 9999px;
	}

	.unusable-badge {
		position: absolute;
		top: 6px;
		right: 6px;
		padding: 2px 8px;
		background: var(--muted);
		color: var(--muted-foreground);
		font-size: 0.6875rem;
		font-weight: 600;
		border-radius: 9999px;
	}

	/* States */
	.loading-state, .empty-state, .error-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 8px;
		padding: 32px 16px;
		color: var(--muted-foreground);
		font-size: 0.875rem;
	}

	.spinner {
		width: 24px;
		height: 24px;
		border: 2px solid var(--border);
		border-top-color: var(--primary);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	.retry-btn {
		padding: 6px 16px;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		border-radius: var(--radius);
		cursor: pointer;
		font-size: 0.8125rem;
	}

	.error-state p {
		color: var(--destructive);
	}

	/* Reduced motion */
	@media (prefers-reduced-motion: reduce) {
		.model-thumb.placeholder {
			animation: none;
		}

		.model-card {
			transition: none;
		}

		.model-card:hover {
			transform: none;
		}

		.model-thumb img {
			transition: none;
		}

		.model-card:hover .model-thumb img {
			transform: none;
		}
	}

	/* Mobile */
	@media (max-width: 640px) {
		.model-grid {
			grid-template-columns: 1fr;
			max-height: 360px;
		}

		.model-thumb {
			height: 80px;
		}
	}
</style>

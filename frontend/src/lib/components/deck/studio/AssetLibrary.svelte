<script lang="ts">
	import {
		X,
		Search,
		Image as ImageIcon,
		Film,
		Eye,
		Edit,
		Trash2,
		CheckSquare,
		Square,
		Layers,
		Volume2,
		Plus,
		FolderPlus,
		Heart,
		Download,
		Grid3x3,
		LayoutGrid,
		XCircle
	} from 'lucide-svelte';
	import type { DeckGeneration } from '../types';
	import { savedAssets, studio } from '$lib/stores/studio';
	import type { Asset } from '$lib/stores/studio';

	// Props
	interface Props {
		onSelect?: (asset: DeckGeneration) => void;
		onClose?: () => void;
	}

	let { onSelect, onClose }: Props = $props();

	// Types
	type FilterType = 'all' | 'images' | 'videos' | 'audio';
	type ViewMode = 'grid' | 'large';

	// Map store asset to component's expected format
	function mapStoreAsset(asset: Asset): DeckGeneration {
		return {
			id: asset.id,
			type: asset.type,
			prompt: asset.prompt,
			status: 'complete',
			thumbnailUrl: asset.url,
			resultUrl: asset.url
		};
	}

	// Derived assets from store
	let assets = $derived($savedAssets.map(mapStoreAsset));

	// State
	let activeFilter: FilterType = $state('all');
	let searchQuery = $state('');
	let isMultiSelectMode = $state(false);
	let selectedIds: Set<string> = $state(new Set());
	let viewMode: ViewMode = $state('grid');
	let hoveredId: string | null = $state(null);

	// Derived
	let filteredAssets = $derived.by(() => {
		let filtered = assets;

		// Filter by type
		if (activeFilter === 'images') {
			filtered = filtered.filter((a) => a.type === 'image');
		} else if (activeFilter === 'videos') {
			filtered = filtered.filter((a) => a.type === 'video');
		} else if (activeFilter === 'audio') {
			filtered = filtered.filter((a) => a.type === 'audio');
		}

		// Filter by search query
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			filtered = filtered.filter((a) => a.prompt.toLowerCase().includes(query));
		}

		return filtered;
	});

	let allSelected = $derived(
		filteredAssets.length > 0 && filteredAssets.every((a) => selectedIds.has(a.id))
	);

	let imageCount = $derived(assets.filter((a) => a.type === 'image').length);
	let videoCount = $derived(assets.filter((a) => a.type === 'video').length);
	let audioCount = $derived(assets.filter((a) => a.type === 'audio').length);

	// Handlers
	function handleFilterChange(filter: FilterType) {
		activeFilter = filter;
	}

	function handleSearchInput(event: Event) {
		const input = event.target as HTMLInputElement;
		searchQuery = input.value;
	}

	function handleAssetClick(asset: DeckGeneration) {
		if (isMultiSelectMode) {
			toggleSelection(asset.id);
		} else {
			onSelect?.(asset);
		}
	}

	function toggleMultiSelect() {
		isMultiSelectMode = !isMultiSelectMode;
		if (!isMultiSelectMode) {
			selectedIds = new Set();
		}
	}

	function toggleSelection(id: string) {
		const newSet = new Set(selectedIds);
		if (newSet.has(id)) {
			newSet.delete(id);
		} else {
			newSet.add(id);
		}
		selectedIds = newSet;
	}

	function selectAll() {
		if (allSelected) {
			selectedIds = new Set();
		} else {
			selectedIds = new Set(filteredAssets.map((a) => a.id));
		}
	}

	function handleBulkDelete() {
		// Delete selected assets from the store
		for (const id of selectedIds) {
			studio.deleteAsset(id);
		}
		selectedIds = new Set();
		isMultiSelectMode = false;
	}

	function handleBulkDownload() {
		// Placeholder for bulk download
		console.log('Bulk download:', selectedIds);
	}

	function handleView(asset: DeckGeneration, event: MouseEvent) {
		event.stopPropagation();
		onSelect?.(asset);
	}

	function handleEdit(asset: DeckGeneration, event: MouseEvent) {
		event.stopPropagation();
		console.log('Edit asset:', asset.id);
	}

	function handleDelete(asset: DeckGeneration, event: MouseEvent) {
		event.stopPropagation();
		studio.deleteAsset(asset.id);
	}

	function handleDownload(asset: DeckGeneration, event: MouseEvent) {
		event.stopPropagation();
		console.log('Download asset:', asset.id);
	}

	function truncatePrompt(prompt: string, maxLength: number = 50): string {
		if (prompt.length <= maxLength) return prompt;
		return prompt.substring(0, maxLength) + '...';
	}

	function getTypeIcon(type: DeckGeneration['type']) {
		switch (type) {
			case 'image':
				return ImageIcon;
			case 'video':
				return Film;
			case 'audio':
				return Volume2;
			default:
				return ImageIcon;
		}
	}
</script>

<div class="cosmic-library h-full flex flex-col">
	<!-- Header -->
	<div class="library-header">
		<div class="flex items-center gap-3">
			<div class="header-icon">
				<Layers class="w-5 h-5 text-primary" />
			</div>
			<div>
				<h2 class="text-base font-semibold text-foreground">Asset Library</h2>
				<p class="text-xs text-muted-foreground">
					{assets.length} {assets.length === 1 ? 'asset' : 'assets'}
				</p>
			</div>
		</div>
		<button type="button" onclick={() => onClose?.()} class="header-close" aria-label="Close">
			<X class="w-5 h-5" />
		</button>
	</div>

	<!-- Toolbar -->
	<div class="toolbar">
		<!-- Search -->
		<div class="search-container">
			<Search class="search-icon" />
			<input
				type="text"
				value={searchQuery}
				oninput={handleSearchInput}
				placeholder="Search assets..."
				class="search-input"
			/>
			{#if searchQuery}
				<button
					type="button"
					onclick={() => (searchQuery = '')}
					class="search-clear"
					aria-label="Clear search"
				>
					<XCircle class="w-4 h-4" />
				</button>
			{/if}
		</div>

		<!-- View mode toggle -->
		<div class="view-toggle">
			<button
				type="button"
				onclick={() => (viewMode = 'grid')}
				class="view-btn {viewMode === 'grid' ? 'view-btn-active' : ''}"
				aria-label="Grid view"
				title="Grid view"
			>
				<Grid3x3 class="w-4 h-4" />
			</button>
			<button
				type="button"
				onclick={() => (viewMode = 'large')}
				class="view-btn {viewMode === 'large' ? 'view-btn-active' : ''}"
				aria-label="Large view"
				title="Large view"
			>
				<LayoutGrid class="w-4 h-4" />
			</button>
		</div>
	</div>

	<!-- Filter tabs -->
	<div class="filter-tabs">
		<button
			type="button"
			onclick={() => handleFilterChange('all')}
			class="filter-tab {activeFilter === 'all' ? 'filter-tab-active' : ''}"
		>
			All
			<span class="filter-count">{assets.length}</span>
		</button>
		<button
			type="button"
			onclick={() => handleFilterChange('images')}
			class="filter-tab {activeFilter === 'images' ? 'filter-tab-active' : ''}"
		>
			<ImageIcon class="w-4 h-4" />
			Images
			<span class="filter-count">{imageCount}</span>
		</button>
		<button
			type="button"
			onclick={() => handleFilterChange('videos')}
			class="filter-tab {activeFilter === 'videos' ? 'filter-tab-active' : ''}"
		>
			<Film class="w-4 h-4" />
			Videos
			<span class="filter-count">{videoCount}</span>
		</button>
		<button
			type="button"
			onclick={() => handleFilterChange('audio')}
			class="filter-tab {activeFilter === 'audio' ? 'filter-tab-active' : ''}"
		>
			<Volume2 class="w-4 h-4" />
			Audio
			<span class="filter-count">{audioCount}</span>
		</button>
	</div>

	<!-- Multi-select toolbar -->
	{#if isMultiSelectMode}
		<div class="multiselect-toolbar">
			<div class="flex items-center gap-3">
				<button type="button" onclick={selectAll} class="select-all-btn">
					{#if allSelected}
						<CheckSquare class="w-4 h-4" />
					{:else}
						<Square class="w-4 h-4" />
					{/if}
					{allSelected ? 'Deselect all' : 'Select all'}
				</button>
				<span class="selection-count">{selectedIds.size} selected</span>
			</div>
			<div class="flex items-center gap-2">
				<button
					type="button"
					onclick={handleBulkDownload}
					disabled={selectedIds.size === 0}
					class="bulk-action-btn"
				>
					<Download class="w-4 h-4" />
					Download
				</button>
				<button
					type="button"
					onclick={handleBulkDelete}
					disabled={selectedIds.size === 0}
					class="bulk-action-btn bulk-action-danger"
				>
					<Trash2 class="w-4 h-4" />
					Delete
				</button>
				<button type="button" onclick={toggleMultiSelect} class="cancel-btn"> Cancel </button>
			</div>
		</div>
	{:else}
		<div class="actions-bar">
			<button type="button" onclick={toggleMultiSelect} class="action-bar-btn">
				<CheckSquare class="w-4 h-4" />
				Select
			</button>
		</div>
	{/if}

	<!-- Asset grid -->
	<div class="flex-1 overflow-y-auto custom-scrollbar p-4">
		{#if filteredAssets.length === 0}
			<!-- Empty state -->
			<div class="h-full flex flex-col items-center justify-center text-center">
				<div class="empty-icon mb-4">
					{#if activeFilter === 'images'}
						<ImageIcon class="w-10 h-10 text-primary" />
					{:else if activeFilter === 'videos'}
						<Film class="w-10 h-10 text-primary" />
					{:else if activeFilter === 'audio'}
						<Volume2 class="w-10 h-10 text-primary" />
					{:else}
						<Layers class="w-10 h-10 text-primary" />
					{/if}
				</div>
				<h3 class="text-sm font-semibold text-foreground mb-2">
					{searchQuery ? 'No assets found' : 'No assets yet'}
				</h3>
				<p class="text-xs text-muted-foreground max-w-xs">
					{searchQuery
						? 'Try adjusting your search or filters'
						: 'Save your favorite generations to see them here'}
				</p>
			</div>
		{:else}
			<div
				class="asset-grid {viewMode === 'large' ? 'asset-grid-large' : 'asset-grid-compact'}"
			>
				{#each filteredAssets as asset (asset.id)}
					{@const TypeIcon = getTypeIcon(asset.type)}
					<div
						role="button"
						tabindex="0"
						onclick={() => handleAssetClick(asset)}
						onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && handleAssetClick(asset)}
						onmouseenter={() => (hoveredId = asset.id)}
						onmouseleave={() => (hoveredId = null)}
						class="asset-card {hoveredId === asset.id ? 'asset-card-hover' : ''}"
					>
						<!-- Thumbnail -->
						<div class="asset-thumb">
							{#if asset.thumbnailUrl}
								<img
									src={asset.thumbnailUrl}
									alt={asset.prompt}
									class="asset-image"
								/>
							{:else}
								<div class="asset-placeholder">
									<TypeIcon class="w-10 h-10 text-muted-foreground/50" />
								</div>
							{/if}

							<!-- Type badge -->
							<div class="type-badge">
								<TypeIcon class="w-3.5 h-3.5" />
							</div>

							<!-- Multi-select checkbox -->
							{#if isMultiSelectMode}
								<div class="select-checkbox">
									{#if selectedIds.has(asset.id)}
										<div class="checkbox-checked">
											<svg
												class="w-3.5 h-3.5"
												fill="none"
												stroke="currentColor"
												viewBox="0 0 24 24"
											>
												<path
													stroke-linecap="round"
													stroke-linejoin="round"
													stroke-width="3"
													d="M5 13l4 4L19 7"
												/>
											</svg>
										</div>
									{:else}
										<div class="checkbox-unchecked"></div>
									{/if}
								</div>
							{/if}

							<!-- Hover overlay -->
							{#if !isMultiSelectMode && hoveredId === asset.id}
								<div class="asset-overlay">
									<div class="overlay-actions">
										<button
											type="button"
											onclick={(e) => handleView(asset, e)}
											class="overlay-btn"
											aria-label="View"
											title="View"
										>
											<Eye class="w-4 h-4" />
										</button>
										<button
											type="button"
											onclick={(e) => handleDownload(asset, e)}
											class="overlay-btn"
											aria-label="Download"
											title="Download"
										>
											<Download class="w-4 h-4" />
										</button>
										<button
											type="button"
											onclick={(e) => handleEdit(asset, e)}
											class="overlay-btn"
											aria-label="Edit"
											title="Edit"
										>
											<Edit class="w-4 h-4" />
										</button>
										<button
											type="button"
											onclick={(e) => handleDelete(asset, e)}
											class="overlay-btn overlay-btn-danger"
											aria-label="Delete"
											title="Delete"
										>
											<Trash2 class="w-4 h-4" />
										</button>
									</div>
								</div>
							{/if}
						</div>

						<!-- Asset info (visible in large view) -->
						{#if viewMode === 'large'}
							<div class="asset-info">
								<p class="asset-prompt">{truncatePrompt(asset.prompt, 80)}</p>
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	</div>
</div>

<style>
	/* Base cosmic library container */
	.cosmic-library {
		background: var(--card, oklch(0.18 0.008 260));
		position: relative;
	}

	/* Header */
	.library-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 1.25rem 1.5rem;
		border-bottom: 1px solid oklch(0.72 0.14 180 / 0.15);
		background: oklch(0.16 0.008 260 / 0.5);
	}

	.header-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 2.5rem;
		height: 2.5rem;
		background: oklch(0.72 0.14 180 / 0.15);
		border: 1px solid oklch(0.72 0.14 180 / 0.3);
		border-radius: 0.75rem;
	}

	.header-close {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 2.25rem;
		height: 2.25rem;
		color: var(--muted-foreground);
		border-radius: 0.5rem;
		transition: all 0.2s;
	}

	.header-close:hover {
		background: oklch(0.72 0.14 180 / 0.1);
		color: var(--foreground);
	}

	/* Toolbar */
	.toolbar {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		padding: 1rem 1.5rem;
		border-bottom: 1px solid oklch(0.72 0.14 180 / 0.1);
	}

	/* Search */
	.search-container {
		position: relative;
		flex: 1;
		display: flex;
		align-items: center;
	}

	.search-icon {
		position: absolute;
		left: 0.875rem;
		width: 1rem;
		height: 1rem;
		color: var(--muted-foreground);
		pointer-events: none;
		z-index: 1;
	}

	.search-input {
		width: 100%;
		height: 2.5rem;
		padding: 0 2.5rem 0 2.5rem;
		background: oklch(0.14 0.008 260 / 0.5);
		border: 1px solid oklch(0.72 0.14 180 / 0.15);
		border-radius: 0.625rem;
		color: var(--foreground);
		font-size: 0.875rem;
		transition: all 0.2s;
		backdrop-filter: blur(var(--glass-blur));
		-webkit-backdrop-filter: blur(var(--glass-blur));
	}

	.search-input::placeholder {
		color: var(--muted-foreground);
	}

	.search-input:focus {
		outline: none;
		border-color: oklch(0.72 0.14 180 / 0.4);
		background: oklch(0.16 0.008 260 / 0.7);
		box-shadow: 0 0 0 3px oklch(0.72 0.14 180 / 0.1);
	}

	.search-clear {
		position: absolute;
		right: 0.75rem;
		color: var(--muted-foreground);
		transition: color 0.2s;
	}

	.search-clear:hover {
		color: var(--foreground);
	}

	/* View toggle */
	.view-toggle {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		background: oklch(0.14 0.008 260 / 0.5);
		border: 1px solid oklch(0.72 0.14 180 / 0.1);
		border-radius: 0.5rem;
		padding: 0.25rem;
	}

	.view-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 2rem;
		height: 2rem;
		color: var(--muted-foreground);
		border-radius: 0.375rem;
		transition: all 0.2s;
	}

	.view-btn:hover {
		background: oklch(0.72 0.14 180 / 0.1);
		color: var(--foreground);
	}

	.view-btn-active {
		background: oklch(0.72 0.14 180 / 0.2);
		color: var(--primary);
	}

	/* Filter tabs */
	.filter-tabs {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.75rem 1.5rem;
		border-bottom: 1px solid oklch(0.72 0.14 180 / 0.1);
		overflow-x: auto;
		scrollbar-width: none;
	}

	.filter-tabs::-webkit-scrollbar {
		display: none;
	}

	.filter-tab {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.5rem 1rem;
		background: oklch(0.14 0.008 260 / 0.5);
		border: 1px solid oklch(0.72 0.14 180 / 0.1);
		border-radius: 0.625rem;
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--muted-foreground);
		white-space: nowrap;
		transition: all 0.2s;
		backdrop-filter: blur(var(--glass-blur));
		-webkit-backdrop-filter: blur(var(--glass-blur));
	}

	.filter-tab:hover {
		background: oklch(0.16 0.008 260 / 0.7);
		border-color: oklch(0.72 0.14 180 / 0.2);
		color: var(--foreground);
	}

	.filter-tab-active {
		background: oklch(0.72 0.14 180 / 0.15);
		border-color: oklch(0.72 0.14 180 / 0.4);
		color: var(--primary);
		box-shadow: 0 0 12px oklch(0.72 0.14 180 / 0.2);
	}

	.filter-count {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 1.25rem;
		height: 1.25rem;
		padding: 0 0.375rem;
		background: oklch(0.72 0.14 180 / 0.15);
		border-radius: 0.375rem;
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--primary);
	}

	/* Multi-select toolbar */
	.multiselect-toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.75rem 1.5rem;
		background: oklch(0.72 0.14 180 / 0.1);
		border-bottom: 1px solid oklch(0.72 0.14 180 / 0.2);
	}

	.select-all-btn {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.8125rem;
		color: var(--foreground);
		transition: color 0.2s;
	}

	.select-all-btn:hover {
		color: var(--primary);
	}

	.selection-count {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
	}

	.bulk-action-btn {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.5rem 0.875rem;
		background: oklch(0.72 0.14 180 / 0.15);
		border: 1px solid oklch(0.72 0.14 180 / 0.3);
		border-radius: 0.5rem;
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--primary);
		transition: all 0.2s;
	}

	.bulk-action-btn:hover:not(:disabled) {
		background: oklch(0.72 0.14 180 / 0.25);
		border-color: oklch(0.72 0.14 180 / 0.5);
	}

	.bulk-action-btn:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.bulk-action-danger:hover:not(:disabled) {
		background: var(--destructive);
		border-color: var(--destructive);
		color: white;
	}

	.cancel-btn {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		transition: color 0.2s;
	}

	.cancel-btn:hover {
		color: var(--foreground);
	}

	/* Actions bar */
	.actions-bar {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		padding: 0.75rem 1.5rem;
		border-bottom: 1px solid oklch(0.72 0.14 180 / 0.1);
	}

	.action-bar-btn {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		transition: color 0.2s;
	}

	.action-bar-btn:hover {
		color: var(--foreground);
	}

	/* Asset grid */
	.asset-grid {
		display: grid;
		gap: 1rem;
	}

	.asset-grid-compact {
		grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
	}

	.asset-grid-large {
		grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
	}

	/* Asset card */
	.asset-card {
		display: flex;
		flex-direction: column;
		background: oklch(0.14 0.008 260 / 0.5);
		border: 1px solid oklch(0.72 0.14 180 / 0.1);
		border-radius: 0.875rem;
		overflow: hidden;
		transition: all 0.2s;
		cursor: pointer;
		backdrop-filter: blur(var(--glass-blur));
		-webkit-backdrop-filter: blur(var(--glass-blur));
	}

	.asset-card:hover {
		border-color: oklch(0.72 0.14 180 / 0.3);
		box-shadow: 0 4px 20px oklch(0.72 0.14 180 / 0.15);
		transform: translateY(-2px);
	}

	.asset-card-hover {
		border-color: oklch(0.72 0.14 180 / 0.4);
	}

	/* Asset thumbnail */
	.asset-thumb {
		position: relative;
		aspect-ratio: 1;
		background: oklch(0.12 0.008 260);
		overflow: hidden;
	}

	.asset-image {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	.asset-placeholder {
		width: 100%;
		height: 100%;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	/* Type badge */
	.type-badge {
		position: absolute;
		top: 0.625rem;
		left: 0.625rem;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 2rem;
		height: 2rem;
		background: oklch(0.18 0.01 260 / 0.9);
		backdrop-filter: blur(var(--glass-blur));
		-webkit-backdrop-filter: blur(var(--glass-blur));
		border: 1px solid oklch(0.72 0.14 180 / 0.25);
		border-radius: 0.5rem;
		color: var(--primary);
	}

	/* Select checkbox */
	.select-checkbox {
		position: absolute;
		top: 0.625rem;
		right: 0.625rem;
		z-index: 10;
	}

	.checkbox-checked {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.75rem;
		height: 1.75rem;
		background: var(--primary);
		border-radius: 0.375rem;
		color: var(--primary-foreground);
		box-shadow: 0 0 12px var(--primary);
	}

	.checkbox-unchecked {
		width: 1.75rem;
		height: 1.75rem;
		background: oklch(0.18 0.01 260 / 0.9);
		backdrop-filter: blur(var(--glass-blur));
		-webkit-backdrop-filter: blur(var(--glass-blur));
		border: 2px solid oklch(0.72 0.14 180 / 0.4);
		border-radius: 0.375rem;
	}

	/* Asset overlay */
	.asset-overlay {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		background: oklch(0.1 0.008 260 / 0.92);
		backdrop-filter: blur(4px);
		-webkit-backdrop-filter: blur(4px);
		animation: fade-in 0.15s ease;
	}

	@keyframes fade-in {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}

	.overlay-actions {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: center;
		gap: 0.5rem;
		padding: 1rem;
	}

	.overlay-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 2.25rem;
		height: 2.25rem;
		background: oklch(0.72 0.14 180 / 0.15);
		border: 1px solid oklch(0.72 0.14 180 / 0.3);
		border-radius: 0.5rem;
		color: var(--primary);
		transition: all 0.2s;
	}

	.overlay-btn:hover {
		background: oklch(0.72 0.14 180 / 0.3);
		border-color: oklch(0.72 0.14 180 / 0.5);
		transform: scale(1.1);
		box-shadow: 0 0 12px oklch(0.72 0.14 180 / 0.3);
	}

	.overlay-btn-danger:hover {
		background: var(--destructive);
		border-color: var(--destructive);
		color: white;
		box-shadow: 0 0 12px var(--destructive);
	}

	/* Asset info */
	.asset-info {
		padding: 0.875rem;
	}

	.asset-prompt {
		font-size: 0.8125rem;
		line-height: 1.4;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
	}

	/* Empty state */
	.empty-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 5rem;
		height: 5rem;
		background: oklch(0.72 0.14 180 / 0.1);
		border: 1px solid oklch(0.72 0.14 180 / 0.25);
		border-radius: 1.25rem;
	}

	/* Custom scrollbar */
	.custom-scrollbar {
		scrollbar-width: thin;
		scrollbar-color: oklch(0.72 0.14 180 / 0.3) transparent;
	}

	.custom-scrollbar::-webkit-scrollbar {
		width: 8px;
	}

	.custom-scrollbar::-webkit-scrollbar-track {
		background: transparent;
	}

	.custom-scrollbar::-webkit-scrollbar-thumb {
		background: oklch(0.72 0.14 180 / 0.3);
		border-radius: 4px;
	}

	.custom-scrollbar::-webkit-scrollbar-thumb:hover {
		background: oklch(0.72 0.14 180 / 0.5);
	}
</style>

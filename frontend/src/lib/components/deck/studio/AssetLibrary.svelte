<script lang="ts">
	/**
	 * AssetLibrary - Saved assets grid component
	 *
	 * Provides:
	 * - Filter tabs: All, Images, Videos
	 * - Search input
	 * - Grid of thumbnails
	 * - Click to preview/use
	 * - Multi-select for bulk delete
	 * - Lazy loading for large libraries
	 */
	import { createEventDispatcher } from 'svelte';
	import { studio, savedAssets, imageAssets, videoAssets } from '$lib/stores/studio';
	import type { Asset } from '$lib/stores/studio';

	const dispatch = createEventDispatcher<{
		select: { asset: Asset };
		edit: { asset: Asset };
		delete: { assets: Asset[] };
	}>();

	// Filter state
	type FilterType = 'all' | 'images' | 'videos';
	let activeFilter = $state<FilterType>('all');
	let searchQuery = $state('');
	let selectedIds = $state<Set<string>>(new Set());
	let isSelecting = $state(false);

	// Filtered assets
	const filteredAssets = $derived(() => {
		let assets: Asset[];

		switch (activeFilter) {
			case 'images':
				assets = $imageAssets;
				break;
			case 'videos':
				assets = $videoAssets;
				break;
			default:
				assets = $savedAssets;
		}

		// Apply search filter
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			assets = assets.filter(
				(a) =>
					a.prompt.toLowerCase().includes(query) ||
					a.tags.some((t) => t.toLowerCase().includes(query))
			);
		}

		// Sort by date (newest first)
		return assets.sort((a, b) => b.createdAt.getTime() - a.createdAt.getTime());
	});

	function toggleSelect(id: string) {
		if (selectedIds.has(id)) {
			selectedIds.delete(id);
			selectedIds = new Set(selectedIds);
		} else {
			selectedIds.add(id);
			selectedIds = new Set(selectedIds);
		}
	}

	function selectAll() {
		selectedIds = new Set(filteredAssets().map((a) => a.id));
	}

	function deselectAll() {
		selectedIds = new Set();
		isSelecting = false;
	}

	function handleAssetClick(asset: Asset) {
		if (isSelecting) {
			toggleSelect(asset.id);
		} else {
			dispatch('select', { asset });
		}
	}

	function handleDelete() {
		const assetsToDelete = $savedAssets.filter((a) => selectedIds.has(a.id));
		if (assetsToDelete.length === 0) return;

		if (confirm(`Delete ${assetsToDelete.length} asset(s)? This cannot be undone.`)) {
			assetsToDelete.forEach((a) => studio.deleteAsset(a.id));
			deselectAll();
		}
	}

	function handleEdit(asset: Asset) {
		dispatch('edit', { asset });
	}

	// Format date
	function formatDate(date: Date): string {
		return new Intl.DateTimeFormat('en-US', {
			month: 'short',
			day: 'numeric'
		}).format(date);
	}
</script>

<div class="asset-library h-full flex flex-col">
	<!-- Header -->
	<div class="flex items-center justify-between mb-6">
		<div>
			<h2 class="text-xl font-semibold text-foreground">Asset Library</h2>
			<p class="text-sm text-muted-foreground mt-0.5">
				{$savedAssets.length} saved {$savedAssets.length === 1 ? 'asset' : 'assets'}
			</p>
		</div>

		{#if isSelecting}
			<div class="flex items-center gap-2">
				<span class="text-sm text-muted-foreground">
					{selectedIds.size} selected
				</span>
				<button
					type="button"
					class="px-3 py-1.5 text-sm rounded-lg bg-muted/50 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
					onclick={selectAll}
				>
					Select All
				</button>
				<button
					type="button"
					class="px-3 py-1.5 text-sm rounded-lg bg-destructive/10 text-destructive hover:bg-destructive/20 transition-colors"
					onclick={handleDelete}
					disabled={selectedIds.size === 0}
				>
					Delete
				</button>
				<button
					type="button"
					class="px-3 py-1.5 text-sm rounded-lg bg-muted/50 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
					onclick={deselectAll}
				>
					Cancel
				</button>
			</div>
		{:else}
			<button
				type="button"
				class="px-3 py-1.5 text-sm rounded-lg bg-muted/50 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
				onclick={() => isSelecting = true}
			>
				Select
			</button>
		{/if}
	</div>

	<!-- Filters and Search -->
	<div class="flex flex-col sm:flex-row items-start sm:items-center gap-4 mb-6">
		<!-- Filter Tabs -->
		<div class="flex items-center gap-1 p-1 bg-muted/30 rounded-xl">
			<button
				type="button"
				class="px-4 py-2 text-sm font-medium rounded-lg transition-all duration-200 {activeFilter === 'all'
					? 'bg-background text-foreground shadow-sm'
					: 'text-muted-foreground hover:text-foreground'}"
				onclick={() => activeFilter = 'all'}
			>
				All
				<span class="ml-1.5 text-xs opacity-70">{$savedAssets.length}</span>
			</button>
			<button
				type="button"
				class="px-4 py-2 text-sm font-medium rounded-lg transition-all duration-200 {activeFilter === 'images'
					? 'bg-background text-foreground shadow-sm'
					: 'text-muted-foreground hover:text-foreground'}"
				onclick={() => activeFilter = 'images'}
			>
				Images
				<span class="ml-1.5 text-xs opacity-70">{$imageAssets.length}</span>
			</button>
			<button
				type="button"
				class="px-4 py-2 text-sm font-medium rounded-lg transition-all duration-200 {activeFilter === 'videos'
					? 'bg-background text-foreground shadow-sm'
					: 'text-muted-foreground hover:text-foreground'}"
				onclick={() => activeFilter = 'videos'}
			>
				Videos
				<span class="ml-1.5 text-xs opacity-70">{$videoAssets.length}</span>
			</button>
		</div>

		<!-- Search -->
		<div class="relative flex-1 max-w-sm">
			<svg class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
			</svg>
			<input
				type="text"
				bind:value={searchQuery}
				placeholder="Search assets..."
				class="w-full pl-10 pr-4 py-2 bg-background border border-border/50 rounded-xl text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
			/>
			{#if searchQuery}
				<button
					type="button"
					class="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
					onclick={() => searchQuery = ''}
					aria-label="Clear search"
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			{/if}
		</div>
	</div>

	<!-- Asset Grid -->
	{#if filteredAssets().length === 0}
		<div class="flex-1 flex flex-col items-center justify-center">
			{#if searchQuery}
				<svg class="w-16 h-16 text-muted-foreground/30 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
				</svg>
				<h3 class="text-lg font-medium text-foreground mb-1">No results found</h3>
				<p class="text-sm text-muted-foreground">Try a different search term</p>
			{:else}
				<svg class="w-16 h-16 text-muted-foreground/30 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
				</svg>
				<h3 class="text-lg font-medium text-foreground mb-1">No assets yet</h3>
				<p class="text-sm text-muted-foreground">Generate content and save it to your library</p>
			{/if}
		</div>
	{:else}
		<div class="flex-1 overflow-y-auto -mx-2 px-2">
			<div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-3">
				{#each filteredAssets() as asset (asset.id)}
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<div
						class="group relative aspect-square rounded-xl overflow-hidden border transition-all duration-200 cursor-pointer {selectedIds.has(asset.id)
							? 'border-primary ring-2 ring-primary/30'
							: 'border-border/50 hover:border-border'}"
						onclick={() => handleAssetClick(asset)}
						role="button"
						tabindex="0"
						onkeydown={(e) => e.key === 'Enter' && handleAssetClick(asset)}
					>
						<!-- Thumbnail -->
						{#if asset.type === 'video'}
							<video
								src={asset.thumbnailUrl || asset.url}
								class="w-full h-full object-cover"
								muted
								loop
								playsinline
							>
								<track kind="captions" />
							</video>
							<!-- Video indicator -->
							<div class="absolute top-2 right-2 p-1 rounded-md bg-black/50 text-white">
								<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24">
									<path d="M5 3l14 9-14 9V3z" />
								</svg>
							</div>
						{:else}
							<img
								src={asset.thumbnailUrl || asset.url}
								alt={asset.prompt}
								class="w-full h-full object-cover"
								loading="lazy"
							/>
						{/if}

						<!-- Selection checkbox -->
						{#if isSelecting}
							<div class="absolute top-2 left-2 w-5 h-5 rounded-md border-2 flex items-center justify-center transition-colors {selectedIds.has(asset.id)
								? 'bg-primary border-primary'
								: 'bg-black/30 border-white/50'}"
							>
								{#if selectedIds.has(asset.id)}
									<svg class="w-3 h-3 text-primary-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
									</svg>
								{/if}
							</div>
						{/if}

						<!-- Hover overlay -->
						<div class="absolute inset-0 bg-gradient-to-t from-black/70 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity">
							<div class="absolute bottom-0 left-0 right-0 p-3">
								<p class="text-white text-xs line-clamp-2">{asset.prompt}</p>
								<p class="text-white/60 text-xs mt-1">{formatDate(asset.createdAt)}</p>
							</div>

							<!-- Quick actions -->
							{#if !isSelecting}
								<div class="absolute top-2 right-2 flex items-center gap-1">
									<button
										type="button"
										class="p-1.5 rounded-lg bg-white/10 text-white hover:bg-white/20 transition-colors"
										onclick={(e) => { e.stopPropagation(); handleEdit(asset); }}
										title="Edit"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
										</svg>
									</button>
								</div>
							{/if}
						</div>

						<!-- Provider badge -->
						<div class="absolute bottom-2 left-2 px-2 py-0.5 rounded-md bg-black/50 text-white text-[10px] font-medium opacity-0 group-hover:opacity-100 transition-opacity">
							{asset.provider}
						</div>
					</div>
				{/each}
			</div>
		</div>
	{/if}
</div>

<style>
	.asset-library {
		/* Container styles */
	}

	.line-clamp-2 {
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
</style>

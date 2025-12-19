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
		Layers
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
	type FilterType = 'all' | 'images' | 'videos';

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

	// Derived
	let filteredAssets = $derived.by(() => {
		let filtered = assets;

		// Filter by type
		if (activeFilter === 'images') {
			filtered = filtered.filter(a => a.type === 'image');
		} else if (activeFilter === 'videos') {
			filtered = filtered.filter(a => a.type === 'video');
		}

		// Filter by search query
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			filtered = filtered.filter(a => a.prompt.toLowerCase().includes(query));
		}

		return filtered;
	});

	let allSelected = $derived(
		filteredAssets.length > 0 && filteredAssets.every(a => selectedIds.has(a.id))
	);

	let imageCount = $derived(assets.filter(a => a.type === 'image').length);
	let videoCount = $derived(assets.filter(a => a.type === 'video').length);

	// Handlers
	function handleFilterChange(filter: FilterType) {
		activeFilter = filter;
	}

	function handleSearchChange(event: Event) {
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
			selectedIds = new Set(filteredAssets.map(a => a.id));
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

	function truncatePrompt(prompt: string, maxLength: number = 50): string {
		if (prompt.length <= maxLength) return prompt;
		return prompt.substring(0, maxLength) + '...';
	}
</script>

<div class="h-full flex flex-col">
	<!-- Header -->
	<div class="shrink-0 flex items-center justify-between px-4 py-3 border-b border-border">
		<div class="flex items-center gap-2">
			<Layers class="w-5 h-5 text-muted-foreground" />
			<h2 class="text-sm font-semibold text-foreground">Asset Library</h2>
		</div>
		<button
			type="button"
			onclick={() => onClose?.()}
			class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
			aria-label="Close asset library"
		>
			<X class="w-5 h-5" />
		</button>
	</div>

	<!-- Filter Tabs -->
	<div class="shrink-0 flex items-center gap-1 px-4 py-2 border-b border-border">
		<button
			type="button"
			onclick={() => handleFilterChange('all')}
			class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md transition-colors {activeFilter === 'all' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
			aria-pressed={activeFilter === 'all'}
		>
			All
			<span class="px-1.5 py-0.5 rounded bg-black/10 text-[10px]">{assets.length}</span>
		</button>
		<button
			type="button"
			onclick={() => handleFilterChange('images')}
			class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md transition-colors {activeFilter === 'images' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
			aria-pressed={activeFilter === 'images'}
		>
			<ImageIcon class="w-3 h-3" />
			Images
			<span class="px-1.5 py-0.5 rounded bg-black/10 text-[10px]">{imageCount}</span>
		</button>
		<button
			type="button"
			onclick={() => handleFilterChange('videos')}
			class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md transition-colors {activeFilter === 'videos' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
			aria-pressed={activeFilter === 'videos'}
		>
			<Film class="w-3 h-3" />
			Videos
			<span class="px-1.5 py-0.5 rounded bg-black/10 text-[10px]">{videoCount}</span>
		</button>
	</div>

	<!-- Search -->
	<div class="shrink-0 px-4 py-3">
		<div class="relative">
			<Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
			<input
				type="text"
				value={searchQuery}
				oninput={handleSearchChange}
				placeholder="Search assets..."
				class="w-full bg-muted border border-border rounded-lg pl-10 pr-4 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
			/>
		</div>
	</div>

	<!-- Multi-select controls -->
	{#if isMultiSelectMode}
		<div class="shrink-0 flex items-center justify-between px-4 py-2 bg-muted/50 border-b border-border">
			<div class="flex items-center gap-3">
				<button
					type="button"
					onclick={selectAll}
					class="flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground"
				>
					{#if allSelected}
						<CheckSquare class="w-4 h-4" />
					{:else}
						<Square class="w-4 h-4" />
					{/if}
					{allSelected ? 'Deselect all' : 'Select all'}
				</button>
				<span class="text-xs text-muted-foreground">
					{selectedIds.size} selected
				</span>
			</div>
			<div class="flex items-center gap-2">
				<button
					type="button"
					onclick={handleBulkDelete}
					disabled={selectedIds.size === 0}
					class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-destructive hover:bg-destructive/10 rounded-md transition-colors disabled:opacity-50"
				>
					<Trash2 class="w-3 h-3" />
					Delete
				</button>
				<button
					type="button"
					onclick={toggleMultiSelect}
					class="text-xs text-muted-foreground hover:text-foreground"
				>
					Cancel
				</button>
			</div>
		</div>
	{:else}
		<div class="shrink-0 flex items-center justify-end px-4 py-2 border-b border-border">
			<button
				type="button"
				onclick={toggleMultiSelect}
				class="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
			>
				<CheckSquare class="w-3 h-3" />
				Select
			</button>
		</div>
	{/if}

	<!-- Asset Grid -->
	<div class="flex-1 overflow-y-auto p-4">
		{#if filteredAssets.length === 0}
			<!-- Empty state -->
			<div class="h-full flex flex-col items-center justify-center text-muted-foreground">
				<div class="w-12 h-12 rounded-full bg-muted/50 flex items-center justify-center mb-3">
					{#if activeFilter === 'images'}
						<ImageIcon class="w-6 h-6" />
					{:else if activeFilter === 'videos'}
						<Film class="w-6 h-6" />
					{:else}
						<Layers class="w-6 h-6" />
					{/if}
				</div>
				<p class="text-sm font-medium">No assets found</p>
				<p class="text-xs mt-1">
					{searchQuery ? 'Try a different search term' : 'Generate some content to get started'}
				</p>
			</div>
		{:else}
			<div class="grid grid-cols-3 gap-3">
				{#each filteredAssets as asset (asset.id)}
					<div
						role="button"
						tabindex="0"
						onclick={() => handleAssetClick(asset)}
						onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && handleAssetClick(asset)}
						class="group relative aspect-square rounded-lg overflow-hidden border border-border hover:border-primary/50 transition-colors focus:outline-none focus:ring-2 focus:ring-primary/50 cursor-pointer"
					>
						<!-- Thumbnail -->
						<img
							src={asset.thumbnailUrl}
							alt={asset.prompt}
							class="w-full h-full object-cover"
						/>

						<!-- Type badge -->
						<div class="absolute top-2 left-2 flex items-center gap-1 px-1.5 py-0.5 rounded bg-black/60 text-white text-[10px]">
							{#if asset.type === 'image'}
								<ImageIcon class="w-2.5 h-2.5" />
							{:else}
								<Film class="w-2.5 h-2.5" />
							{/if}
						</div>

						<!-- Multi-select checkbox -->
						{#if isMultiSelectMode}
							<div class="absolute top-2 right-2">
								{#if selectedIds.has(asset.id)}
									<div class="w-5 h-5 rounded bg-primary text-primary-foreground flex items-center justify-center">
										<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
										</svg>
									</div>
								{:else}
									<div class="w-5 h-5 rounded border-2 border-white/70 bg-black/30"></div>
								{/if}
							</div>
						{/if}

						<!-- Hover overlay with actions -->
						{#if !isMultiSelectMode}
							<div class="absolute inset-0 bg-black/60 opacity-0 group-hover:opacity-100 transition-opacity flex flex-col">
								<!-- Action buttons -->
								<div class="absolute top-2 right-2 flex items-center gap-1">
									<div
										role="button"
										tabindex="0"
										onclick={(e) => handleView(asset, e)}
										onkeydown={(e) => e.key === 'Enter' && handleView(asset, e as unknown as MouseEvent)}
										class="p-1.5 rounded bg-white/20 text-white hover:bg-white/30 transition-colors cursor-pointer"
										aria-label="View"
										title="View"
									>
										<Eye class="w-3.5 h-3.5" />
									</div>
									<div
										role="button"
										tabindex="0"
										onclick={(e) => handleEdit(asset, e)}
										onkeydown={(e) => e.key === 'Enter' && handleEdit(asset, e as unknown as MouseEvent)}
										class="p-1.5 rounded bg-white/20 text-white hover:bg-white/30 transition-colors cursor-pointer"
										aria-label="Edit"
										title="Edit"
									>
										<Edit class="w-3.5 h-3.5" />
									</div>
									<div
										role="button"
										tabindex="0"
										onclick={(e) => handleDelete(asset, e)}
										onkeydown={(e) => e.key === 'Enter' && handleDelete(asset, e as unknown as MouseEvent)}
										class="p-1.5 rounded bg-white/20 text-white hover:bg-destructive/80 transition-colors cursor-pointer"
										aria-label="Delete"
										title="Delete"
									>
										<Trash2 class="w-3.5 h-3.5" />
									</div>
								</div>

								<!-- Truncated prompt -->
								<div class="mt-auto p-2">
									<p class="text-white text-[10px] leading-tight">
										{truncatePrompt(asset.prompt)}
									</p>
								</div>
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	</div>
</div>

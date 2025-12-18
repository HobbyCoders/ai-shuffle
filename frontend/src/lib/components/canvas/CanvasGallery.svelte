<script lang="ts">
	import { canvas, canvasItems, selectedItem, imageCount, videoCount } from '$lib/stores/canvas';
	import type { CanvasItem } from '$lib/stores/canvas';
	import { createEventDispatcher } from 'svelte';

	const dispatch = createEventDispatcher<{
		createImage: void;
		createVideo: void;
	}>();

	let filter: 'all' | 'image' | 'video' = 'all';

	$: filteredItems = filter === 'all'
		? $canvasItems
		: $canvasItems.filter(item => item.type === filter);

	$: filterCounts = {
		all: $canvasItems.length,
		image: $imageCount,
		video: $videoCount
	};

	function handleItemClick(item: CanvasItem) {
		canvas.selectItem(item.id);
	}

	async function handleDelete(item: CanvasItem, event: Event) {
		event.stopPropagation();
		if (confirm('Delete this item?')) {
			await canvas.deleteItem(item.id);
		}
	}

	function handleEdit(item: CanvasItem, event: Event) {
		event.stopPropagation();
		if (item.type === 'image') {
			canvas.setEditingItem(item);
			canvas.setView('edit');
		}
	}

	async function handleDownload(item: CanvasItem, event: Event) {
		event.stopPropagation();
		try {
			// Fetch with credentials to include auth cookie
			const response = await fetch(item.url, { credentials: 'include' });
			if (!response.ok) throw new Error('Download failed');

			const blob = await response.blob();
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = item.filename;
			a.click();
			URL.revokeObjectURL(url);
		} catch (error) {
			console.error('Download failed:', error);
			alert('Download failed. Please try again.');
		}
	}

	function getProviderBadgeColor(provider: string): string {
		switch (provider) {
			case 'google-gemini':
				return 'bg-blue-500/20 text-blue-400';
			case 'google-imagen':
				return 'bg-green-500/20 text-green-400';
			case 'openai-gpt-image':
				return 'bg-emerald-500/20 text-emerald-400';
			case 'google-veo':
				return 'bg-purple-500/20 text-purple-400';
			case 'openai-sora':
				return 'bg-pink-500/20 text-pink-400';
			default:
				return 'bg-muted text-muted-foreground';
		}
	}

	function getProviderDisplayName(provider: string): string {
		switch (provider) {
			case 'google-gemini':
				return 'Gemini';
			case 'google-imagen':
				return 'Imagen';
			case 'openai-gpt-image':
				return 'GPT';
			case 'google-veo':
				return 'Veo';
			case 'openai-sora':
				return 'Sora';
			default:
				return provider;
		}
	}

	function formatDuration(seconds: number | undefined): string {
		if (!seconds) return '';
		return `${seconds}s`;
	}
</script>

<div class="space-y-4">
	<!-- Filter Tabs and Create Buttons -->
	<div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
		<!-- Filter Tabs -->
		<div class="flex gap-1 bg-muted rounded-lg p-1">
			<button
				onclick={() => filter = 'all'}
				class="px-3 py-1.5 text-sm font-medium rounded-md transition-colors {filter === 'all' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
			>
				All <span class="text-xs text-muted-foreground ml-1">({filterCounts.all})</span>
			</button>
			<button
				onclick={() => filter = 'image'}
				class="px-3 py-1.5 text-sm font-medium rounded-md transition-colors {filter === 'image' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
			>
				Images <span class="text-xs text-muted-foreground ml-1">({filterCounts.image})</span>
			</button>
			<button
				onclick={() => filter = 'video'}
				class="px-3 py-1.5 text-sm font-medium rounded-md transition-colors {filter === 'video' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
			>
				Videos <span class="text-xs text-muted-foreground ml-1">({filterCounts.video})</span>
			</button>
		</div>

		<!-- Create Buttons -->
		<div class="flex gap-2">
			<button
				onclick={() => dispatch('createImage')}
				class="px-4 py-2 text-sm font-medium bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors flex items-center gap-2"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
				</svg>
				New Image
			</button>
			<button
				onclick={() => dispatch('createVideo')}
				class="px-4 py-2 text-sm font-medium bg-muted text-foreground border border-border rounded-lg hover:bg-accent transition-colors flex items-center gap-2"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
				</svg>
				New Video
			</button>
		</div>
	</div>

	<!-- Gallery Grid -->
	{#if filteredItems.length === 0}
		<!-- Empty State -->
		<div class="flex flex-col items-center justify-center py-16 text-center">
			<div class="w-20 h-20 rounded-2xl bg-muted/50 flex items-center justify-center mb-4">
				<svg class="w-10 h-10 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
				</svg>
			</div>
			<h3 class="text-lg font-medium text-foreground mb-2">No media yet</h3>
			<p class="text-sm text-muted-foreground mb-6 max-w-sm">
				{#if filter === 'image'}
					You haven't generated any images yet. Click "New Image" to get started.
				{:else if filter === 'video'}
					You haven't generated any videos yet. Click "New Video" to get started.
				{:else}
					Start creating AI-generated images and videos with various providers.
				{/if}
			</p>
			<div class="flex gap-3">
				<button
					onclick={() => dispatch('createImage')}
					class="px-4 py-2 text-sm font-medium bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors"
				>
					Generate Image
				</button>
				<button
					onclick={() => dispatch('createVideo')}
					class="px-4 py-2 text-sm font-medium bg-muted text-foreground border border-border rounded-lg hover:bg-accent transition-colors"
				>
					Generate Video
				</button>
			</div>
		</div>
	{:else}
		<div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
			{#each filteredItems as item (item.id)}
				<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
				<div
					onclick={() => handleItemClick(item)}
					onkeydown={(e) => e.key === 'Enter' && handleItemClick(item)}
					role="button"
					tabindex="0"
					class="group relative aspect-video bg-muted rounded-xl overflow-hidden border border-border hover:border-primary/50 transition-all hover:shadow-lg focus:outline-none focus:ring-2 focus:ring-primary/50 cursor-pointer {$selectedItem?.id === item.id ? 'ring-2 ring-primary' : ''}"
				>
					<!-- Thumbnail -->
					{#if item.type === 'image'}
						<img
							src={item.url}
							alt={item.prompt}
							class="w-full h-full object-cover transition-transform group-hover:scale-105"
							loading="lazy"
						/>
					{:else}
						<video
							src={item.url}
							class="w-full h-full object-cover"
							muted
							preload="metadata"
						>
							<track kind="captions" />
						</video>
						<!-- Video play icon overlay -->
						<div class="absolute inset-0 flex items-center justify-center pointer-events-none">
							<div class="w-12 h-12 rounded-full bg-black/50 flex items-center justify-center">
								<svg class="w-6 h-6 text-white ml-0.5" fill="currentColor" viewBox="0 0 24 24">
									<path d="M8 5v14l11-7z" />
								</svg>
							</div>
						</div>
					{/if}

					<!-- Hover Overlay with Actions -->
					<div class="absolute inset-0 bg-black/60 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center gap-2">
						{#if item.type === 'image'}
							<button
								onclick={(e) => handleEdit(item, e)}
								class="p-2.5 bg-white/20 hover:bg-white/30 rounded-lg transition-colors"
								title="Edit"
							>
								<svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
								</svg>
							</button>
						{/if}
						<button
							onclick={(e) => handleDownload(item, e)}
							class="p-2.5 bg-white/20 hover:bg-white/30 rounded-lg transition-colors"
							title="Download"
						>
							<svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
							</svg>
						</button>
						<button
							onclick={(e) => handleDelete(item, e)}
							class="p-2.5 bg-red-500/80 hover:bg-red-500 rounded-lg transition-colors"
							title="Delete"
						>
							<svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
							</svg>
						</button>
					</div>

					<!-- Badges -->
					<div class="absolute top-2 left-2 right-2 flex items-start justify-between pointer-events-none">
						<!-- Provider Badge -->
						<span class="px-2 py-1 text-xs font-medium rounded-md {getProviderBadgeColor(item.provider)}">
							{getProviderDisplayName(item.provider)}
						</span>

						<!-- Duration Badge (for videos) -->
						{#if item.type === 'video' && item.settings?.duration}
							<span class="px-2 py-1 text-xs font-medium rounded-md bg-black/50 text-white">
								{formatDuration(item.settings.duration)}
							</span>
						{/if}
					</div>

					<!-- Prompt Preview on bottom -->
					<div class="absolute bottom-0 left-0 right-0 p-2 bg-gradient-to-t from-black/80 to-transparent pointer-events-none">
						<p class="text-xs text-white/90 line-clamp-2">{item.prompt}</p>
					</div>
				</div>
			{/each}
		</div>
	{/if}
</div>

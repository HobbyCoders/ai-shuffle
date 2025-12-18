<script lang="ts">
	import { canvas, canvasItems, canvasView, canvasCreateType, isLoading, canvasError, generationProgress, selectedItem, imageCount, videoCount } from '$lib/stores/canvas';
	import CanvasGallery from './CanvasGallery.svelte';
	import ImageGenerator from './ImageGenerator.svelte';
	import VideoGenerator from './VideoGenerator.svelte';
	import ImageEditor from './ImageEditor.svelte';
	import MediaPreview from './MediaPreview.svelte';
	import { onMount, createEventDispatcher } from 'svelte';

	const dispatch = createEventDispatcher<{ close: void }>();

	function handleClose() {
		dispatch('close');
	}

	// Load items on mount
	onMount(() => {
		canvas.loadItems();
	});

	function handleBack() {
		if ($canvasView === 'edit') {
			canvas.setEditingItem(null);
		}
		canvas.setView('gallery');
		canvas.setCreateType(null);
	}

	function startImageCreation() {
		canvas.setCreateType('image');
		canvas.setView('create');
	}

	function startVideoCreation() {
		canvas.setCreateType('video');
		canvas.setView('create');
	}

	// Tab state for gallery view
	let activeTab: 'gallery' | 'image' | 'video' = 'gallery';

	$: {
		if ($canvasView === 'gallery' && $canvasCreateType === null) {
			activeTab = 'gallery';
		} else if ($canvasCreateType === 'image') {
			activeTab = 'image';
		} else if ($canvasCreateType === 'video') {
			activeTab = 'video';
		}
	}

	function setActiveTab(tab: 'gallery' | 'image' | 'video') {
		activeTab = tab;
		if (tab === 'gallery') {
			canvas.setView('gallery');
			canvas.setCreateType(null);
		} else if (tab === 'image') {
			startImageCreation();
		} else if (tab === 'video') {
			startVideoCreation();
		}
	}

	// Get the current view title
	$: viewTitle = $canvasView === 'edit'
		? 'Edit Image'
		: $canvasCreateType === 'image'
			? 'Generate Image'
			: $canvasCreateType === 'video'
				? 'Generate Video'
				: 'Canvas';
</script>

<div class="flex flex-col h-full bg-card rounded-2xl border border-border overflow-hidden">
	<!-- Header -->
	<header class="shrink-0 px-4 sm:px-6 py-4 border-b border-border bg-card">
		<div class="flex items-center justify-between">
			<div class="flex items-center gap-3">
				{#if $canvasView !== 'gallery' || $canvasCreateType !== null}
					<!-- Back button -->
					<button
						onclick={handleBack}
						class="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
						aria-label="Back to gallery"
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
						</svg>
					</button>
				{:else}
					<!-- Icon -->
					<div class="w-10 h-10 rounded-xl bg-primary/15 flex items-center justify-center">
						<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
						</svg>
					</div>
				{/if}
				<div>
					<h2 class="text-lg font-semibold text-foreground">{viewTitle}</h2>
					{#if $canvasView === 'gallery' && $canvasCreateType === null}
						<p class="text-xs text-muted-foreground">
							{$imageCount} images, {$videoCount} videos
						</p>
					{/if}
				</div>
			</div>

			<div class="flex items-center gap-2">
				<!-- Generation indicator -->
				{#if $isLoading}
					<div class="flex items-center gap-2 text-primary">
						<div class="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin"></div>
						<span class="text-sm hidden sm:inline">{$generationProgress || 'Generating...'}</span>
					</div>
				{/if}

				<!-- Close button -->
				<button
					onclick={handleClose}
					class="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
					aria-label="Close Canvas"
					title="Close Canvas"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>
		</div>

		<!-- Tab Navigation - only show in main view -->
		{#if $canvasView === 'gallery' || $canvasCreateType !== null}
			<nav class="flex gap-1 mt-4 -mb-4 overflow-x-auto scrollbar-none" aria-label="Canvas tabs">
				<button
					onclick={() => setActiveTab('gallery')}
					class="px-4 py-2.5 text-sm font-medium rounded-t-lg transition-all whitespace-nowrap border-b-2 -mb-[1px] {activeTab === 'gallery' ? 'text-primary border-primary bg-primary/5' : 'text-muted-foreground hover:text-foreground border-transparent hover:border-muted-foreground/30'}"
				>
					Gallery
				</button>
				<button
					onclick={() => setActiveTab('image')}
					class="px-4 py-2.5 text-sm font-medium rounded-t-lg transition-all whitespace-nowrap border-b-2 -mb-[1px] {activeTab === 'image' ? 'text-primary border-primary bg-primary/5' : 'text-muted-foreground hover:text-foreground border-transparent hover:border-muted-foreground/30'}"
				>
					Image Gen
				</button>
				<button
					onclick={() => setActiveTab('video')}
					class="px-4 py-2.5 text-sm font-medium rounded-t-lg transition-all whitespace-nowrap border-b-2 -mb-[1px] {activeTab === 'video' ? 'text-primary border-primary bg-primary/5' : 'text-muted-foreground hover:text-foreground border-transparent hover:border-muted-foreground/30'}"
				>
					Video Gen
				</button>
			</nav>
		{/if}
	</header>

	<!-- Error Display -->
	{#if $canvasError}
		<div class="mx-4 sm:mx-6 mt-4 bg-destructive/10 border border-destructive text-destructive px-4 py-3 rounded-xl flex items-center justify-between">
			<span class="text-sm">{$canvasError}</span>
			<button
				onclick={() => canvas.clearError()}
				class="p-1 hover:bg-destructive/20 rounded transition-colors"
				aria-label="Dismiss error"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		</div>
	{/if}

	<!-- Main Content Area -->
	<div class="flex-1 overflow-y-auto p-4 sm:p-6">
		{#if $canvasView === 'edit'}
			<ImageEditor />
		{:else if $canvasCreateType === 'image'}
			<ImageGenerator />
		{:else if $canvasCreateType === 'video'}
			<VideoGenerator />
		{:else}
			<CanvasGallery
				on:createImage={startImageCreation}
				on:createVideo={startVideoCreation}
			/>
		{/if}
	</div>

	<!-- Media Preview Modal -->
	{#if $selectedItem}
		<MediaPreview />
	{/if}
</div>

<style>
	.scrollbar-none {
		scrollbar-width: none;
		-ms-overflow-style: none;
	}

	.scrollbar-none::-webkit-scrollbar {
		display: none;
	}
</style>

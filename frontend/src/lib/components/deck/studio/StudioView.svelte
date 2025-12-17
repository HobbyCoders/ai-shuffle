<script lang="ts">
	/**
	 * StudioView - Main studio interface for "The Deck" UI
	 *
	 * Provides direct content creation without chat:
	 * - Split layout: Preview/Canvas (60%) | Controls (40%)
	 * - Shows active generation or empty state
	 * - Variations/history below preview
	 */
	import { studio, activeGeneration, recentGenerations, savedAssets } from '$lib/stores/studio';
	import type { Generation, Asset, GenerationOptions } from '$lib/stores/studio';
	import ImageGenerator from './ImageGenerator.svelte';
	import VideoGenerator from './VideoGenerator.svelte';
	import MediaPreview from './MediaPreview.svelte';
	import AssetLibrary from './AssetLibrary.svelte';
	import GenerationHistory from './GenerationHistory.svelte';

	interface Props {
		/** Currently selected content type */
		contentType?: 'image' | 'video';
		/** Callback when content type changes */
		onContentTypeChange?: (type: 'image' | 'video') => void;
	}

	let {
		contentType = 'image',
		onContentTypeChange
	}: Props = $props();

	// Local state
	let localContentType = $state(contentType);
	let showLibrary = $state(false);

	// Sync with prop
	$effect(() => {
		localContentType = contentType;
	});

	function setContentType(type: 'image' | 'video') {
		localContentType = type;
		onContentTypeChange?.(type);
	}

	// Handle generate events
	async function handleImageGenerate(event: CustomEvent<{ prompt: string; options: GenerationOptions }>) {
		const { prompt, options } = event.detail;
		try {
			await studio.generateImage(prompt, options);
		} catch (e) {
			console.error('[Studio] Image generation failed:', e);
		}
	}

	async function handleVideoGenerate(event: CustomEvent<{ prompt: string; options: GenerationOptions }>) {
		const { prompt, options } = event.detail;
		try {
			await studio.generateVideo(prompt, options);
		} catch (e) {
			console.error('[Studio] Video generation failed:', e);
		}
	}

	// Handle preview actions
	function handleSave(event: CustomEvent<{ generation: Generation }>) {
		studio.saveAsset(event.detail.generation);
	}

	function handleRetry(event: CustomEvent<{ generation: Generation }>) {
		const gen = event.detail.generation;
		if (gen.type === 'image') {
			studio.generateImage(gen.prompt, gen.options);
		} else {
			studio.generateVideo(gen.prompt, gen.options);
		}
	}

	// Handle history clicks
	function handleHistoryClick(event: CustomEvent<{ generation: Generation }>) {
		studio.setActiveGeneration(event.detail.generation.id);
	}

	function handleHistoryRegenerate(event: CustomEvent<{ generation: Generation }>) {
		const gen = event.detail.generation;
		if (gen.type === 'image') {
			studio.generateImage(gen.prompt, gen.options);
		} else {
			studio.generateVideo(gen.prompt, gen.options);
		}
	}

	// Handle library actions
	function handleAssetSelect(event: CustomEvent<{ asset: Asset }>) {
		// When an asset is selected from library, we could load it for editing
		// For now, close the library
		showLibrary = false;
	}
</script>

<div class="studio-view h-full flex flex-col overflow-hidden">
	<!-- Header with content type tabs and library toggle -->
	<header class="flex items-center justify-between px-6 py-4 border-b border-border/30">
		<div class="flex items-center gap-1 p-1 bg-muted/30 rounded-xl">
			<button
				type="button"
				class="px-4 py-2 text-sm font-medium rounded-lg transition-all duration-200 {localContentType === 'image'
					? 'bg-background text-foreground shadow-sm'
					: 'text-muted-foreground hover:text-foreground'}"
				onclick={() => setContentType('image')}
			>
				<span class="flex items-center gap-2">
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
					</svg>
					Image
				</span>
			</button>
			<button
				type="button"
				class="px-4 py-2 text-sm font-medium rounded-lg transition-all duration-200 {localContentType === 'video'
					? 'bg-background text-foreground shadow-sm'
					: 'text-muted-foreground hover:text-foreground'}"
				onclick={() => setContentType('video')}
			>
				<span class="flex items-center gap-2">
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
					</svg>
					Video
				</span>
			</button>
		</div>

		<div class="flex items-center gap-3">
			<!-- Library toggle -->
			<button
				type="button"
				class="flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-xl transition-colors {showLibrary
					? 'bg-primary text-primary-foreground'
					: 'bg-muted/50 text-muted-foreground hover:text-foreground hover:bg-muted'}"
				onclick={() => showLibrary = !showLibrary}
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
				</svg>
				Library
				{#if $savedAssets.length > 0}
					<span class="px-1.5 py-0.5 text-xs rounded-full bg-primary/20 text-primary">
						{$savedAssets.length}
					</span>
				{/if}
			</button>
		</div>
	</header>

	<!-- Main content area -->
	<div class="flex-1 overflow-hidden">
		{#if showLibrary}
			<!-- Asset Library View -->
			<div class="h-full p-6 overflow-y-auto">
				<AssetLibrary
					on:select={handleAssetSelect}
				/>
			</div>
		{:else}
			<!-- Studio Split Layout -->
			<div class="h-full grid grid-cols-1 lg:grid-cols-5 gap-0">
				<!-- Left: Preview Area (60%) -->
				<div class="lg:col-span-3 flex flex-col border-r border-border/30">
					<!-- Main Preview -->
					<div class="flex-1 p-6 overflow-hidden">
						{#if $activeGeneration}
							<MediaPreview
								generation={$activeGeneration}
								on:save={handleSave}
								on:retry={handleRetry}
							/>
						{:else}
							<!-- Empty state -->
							<div class="h-full flex flex-col items-center justify-center">
								<div class="w-24 h-24 rounded-3xl bg-muted/30 flex items-center justify-center mb-6">
									{#if localContentType === 'image'}
										<svg class="w-12 h-12 text-muted-foreground/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
										</svg>
									{:else}
										<svg class="w-12 h-12 text-muted-foreground/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
										</svg>
									{/if}
								</div>
								<h3 class="text-lg font-semibold text-foreground mb-2">
									Create {localContentType === 'image' ? 'an Image' : 'a Video'}
								</h3>
								<p class="text-sm text-muted-foreground text-center max-w-sm">
									Enter a prompt and configure your settings to generate {localContentType === 'image' ? 'stunning images' : 'amazing videos'} with AI.
								</p>
							</div>
						{/if}
					</div>

					<!-- Generation History -->
					{#if $recentGenerations.length > 0}
						<div class="border-t border-border/30 p-4">
							<GenerationHistory
								generations={$recentGenerations}
								on:click={handleHistoryClick}
								on:regenerate={handleHistoryRegenerate}
							/>
						</div>
					{/if}
				</div>

				<!-- Right: Controls Area (40%) -->
				<div class="lg:col-span-2 flex flex-col overflow-hidden bg-muted/10">
					<div class="flex-1 overflow-y-auto p-6">
						{#if localContentType === 'image'}
							<ImageGenerator on:generate={handleImageGenerate} />
						{:else}
							<VideoGenerator on:generate={handleVideoGenerate} />
						{/if}
					</div>
				</div>
			</div>
		{/if}
	</div>
</div>

<style>
	.studio-view {
		background-color: var(--background);
	}
</style>

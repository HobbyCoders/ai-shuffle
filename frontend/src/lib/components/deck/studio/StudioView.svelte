<script lang="ts">
	import { Image, Video, FolderOpen, Mic, FileAudio } from 'lucide-svelte';
	import ImageGenerator from './ImageGenerator.svelte';
	import VideoGenerator from './VideoGenerator.svelte';
	import TTSGenerator from './TTSGenerator.svelte';
	import STTTranscriber from './STTTranscriber.svelte';
	import MediaPreview from './MediaPreview.svelte';
	import AssetLibrary from './AssetLibrary.svelte';
	import GenerationHistory from './GenerationHistory.svelte';
	import type { DeckGeneration } from '../types';
	import { studio, activeGeneration as storeActiveGeneration, activeTab, recentGenerations } from '$lib/stores/studio';
	import type { ImageProvider, VideoProvider, DeckGeneration as StoreDeckGeneration } from '$lib/stores/studio';
	import { onMount } from 'svelte';

	// Props
	interface Props {
		activeGeneration?: DeckGeneration | null;
		onStartGeneration?: (type: 'image' | 'video', config: unknown) => void;
	}

	let { activeGeneration = null, onStartGeneration }: Props = $props();

	// Mobile detection
	let isMobile = $state(false);

	onMount(() => {
		function checkMobile() {
			isMobile = window.innerWidth < 768;
		}
		checkMobile();
		window.addEventListener('resize', checkMobile);
		return () => window.removeEventListener('resize', checkMobile);
	});

	// Handle generation from child components
	async function handleStartGeneration(type: 'image' | 'video', config: unknown) {
		try {
			if (type === 'image') {
				const imageConfig = config as {
					prompt: string;
					provider: string;
					model: string;
					aspectRatio: string;
					style?: string;
					referenceImages?: string[];
					negativePrompt?: string;
					fidelity?: 'low' | 'high';
				};
				await studio.generateImage(imageConfig.prompt, {
					provider: imageConfig.provider as ImageProvider,
					model: imageConfig.model,
					aspectRatio: imageConfig.aspectRatio,
					style: imageConfig.style,
					referenceImages: imageConfig.referenceImages,
					negativePrompt: imageConfig.negativePrompt,
					fidelity: imageConfig.fidelity
				});
			} else {
				const videoConfig = config as {
					prompt: string;
					provider: string;
					model: string;
					duration: number;
					aspectRatio: string;
					startFrame?: string;
				};
				await studio.generateVideo(videoConfig.prompt, {
					provider: videoConfig.provider as VideoProvider,
					model: videoConfig.model,
					aspectRatio: videoConfig.aspectRatio,
					duration: videoConfig.duration,
					sourceImage: videoConfig.startFrame
				});
			}
			// Also call the parent callback if provided
			onStartGeneration?.(type, config);
		} catch (error) {
			console.error('[Studio] Generation failed:', error);
		}
	}

	// State
	let showAssetLibrary = $state(false);
	let selectedAsset: DeckGeneration | null = $state(null);

	// Derived
	let currentPreview = $derived(selectedAsset || activeGeneration);

	// Handlers
	function handleTabChange(tab: 'image' | 'video' | 'tts' | 'stt') {
		studio.setActiveTab(tab);
	}

	function toggleAssetLibrary() {
		showAssetLibrary = !showAssetLibrary;
	}

	function handleAssetSelect(asset: DeckGeneration) {
		selectedAsset = asset;
		showAssetLibrary = false;
	}

	function handleHistorySelect(generation: DeckGeneration) {
		selectedAsset = generation;
	}

	function clearPreview() {
		selectedAsset = null;
	}

	// ========================================================================
	// Media Preview Action Handlers
	// ========================================================================

	/**
	 * Download the generated media
	 */
	function handleDownload(generation: DeckGeneration) {
		if (!generation.resultUrl) return;

		// Create a temporary link and trigger download
		const link = document.createElement('a');
		link.href = generation.resultUrl;
		link.download = `${generation.type}-${generation.id}.${generation.type === 'image' ? 'png' : 'mp4'}`;
		document.body.appendChild(link);
		link.click();
		document.body.removeChild(link);
	}

	/**
	 * Edit the generated image (opens edit mode with the image)
	 */
	async function handleEdit(generation: DeckGeneration) {
		if (generation.type !== 'image' || !generation.resultUrl) return;

		// Switch to image tab and trigger edit mode
		studio.setActiveTab('image');

		// For now, we'll prompt for edit instruction
		const editPrompt = window.prompt('Enter edit instructions:');
		if (editPrompt) {
			// Find the original store generation to get the file path
			const storeGen = $recentGenerations.find(g => g.id === generation.id);
			if (storeGen?.result?.url) {
				await studio.editImage(storeGen.result.url, editPrompt);
			}
		}
	}

	/**
	 * Extend a video (Veo only)
	 */
	async function handleExtend(generation: DeckGeneration) {
		if (generation.type !== 'video') return;

		const extendPrompt = window.prompt('Enter prompt to continue the video:');
		if (extendPrompt) {
			// Find the original store generation to get the source_video_uri
			const storeGen = $recentGenerations.find(g => g.id === generation.id);
			const sourceUri = (storeGen as StoreDeckGeneration | undefined)?.result?.url;
			if (sourceUri) {
				await studio.extendVideo(sourceUri, extendPrompt);
			}
		}
	}

	/**
	 * Save generation to asset library
	 */
	function handleSaveToLibrary(generation: DeckGeneration) {
		// Find the store generation
		const storeGen = $recentGenerations.find(g => g.id === generation.id);
		if (storeGen) {
			const assetId = studio.saveAsset(storeGen);
			if (assetId) {
				// Could show a toast notification here
				console.log('[Studio] Saved to library:', assetId);
			}
		}
	}

	/**
	 * Delete generation from history
	 */
	function handleDelete(generation: DeckGeneration) {
		studio.removeGeneration(generation.id);
		if (selectedAsset?.id === generation.id) {
			selectedAsset = null;
		}
	}

	/**
	 * Retry failed generation
	 */
	async function handleRetry(generation: DeckGeneration) {
		// Find the original store generation to get full settings
		const storeGen = $recentGenerations.find(g => g.id === generation.id);
		if (!storeGen) return;

		if (generation.type === 'image') {
			await studio.generateImage(storeGen.prompt, {
				provider: storeGen.provider as ImageProvider,
				aspectRatio: storeGen.settings.aspectRatio,
				style: storeGen.settings.style
			});
		} else if (generation.type === 'video') {
			await studio.generateVideo(storeGen.prompt, {
				provider: storeGen.provider as VideoProvider,
				aspectRatio: storeGen.settings.aspectRatio,
				duration: storeGen.settings.duration
			});
		}
	}

	/**
	 * Regenerate from history (same as retry but for completed generations)
	 */
	async function handleRegenerate(generation: DeckGeneration) {
		await handleRetry(generation);
	}
</script>

<div class="studio-view" class:mobile={isMobile}>
	<!-- Main Content Area -->
	<div class="studio-main">
		{#if isMobile}
			<!-- Mobile: Tab Header at top -->
			<div class="mobile-tab-header">
				<div class="tab-buttons">
					<button
						type="button"
						onclick={() => handleTabChange('image')}
						class="tab-btn"
						class:active={$activeTab === 'image'}
						aria-pressed={$activeTab === 'image'}
					>
						<Image class="w-4 h-4" />
						<span>Image</span>
					</button>
					<button
						type="button"
						onclick={() => handleTabChange('video')}
						class="tab-btn"
						class:active={$activeTab === 'video'}
						aria-pressed={$activeTab === 'video'}
					>
						<Video class="w-4 h-4" />
						<span>Video</span>
					</button>
					<button
						type="button"
						onclick={() => handleTabChange('tts')}
						class="tab-btn"
						class:active={$activeTab === 'tts'}
						aria-pressed={$activeTab === 'tts'}
					>
						<Mic class="w-4 h-4" />
						<span>Voice</span>
					</button>
					<button
						type="button"
						onclick={() => handleTabChange('stt')}
						class="tab-btn"
						class:active={$activeTab === 'stt'}
						aria-pressed={$activeTab === 'stt'}
					>
						<FileAudio class="w-4 h-4" />
						<span>STT</span>
					</button>
				</div>
				<button
					type="button"
					onclick={toggleAssetLibrary}
					class="library-btn"
					aria-label="Toggle asset library"
					aria-pressed={showAssetLibrary}
				>
					<FolderOpen class="w-5 h-5" />
				</button>
			</div>

			<!-- Mobile: Scrollable content area -->
			<div class="mobile-content">
				<!-- Preview Area -->
				<div class="mobile-preview">
					<MediaPreview
						generation={currentPreview}
						onClear={clearPreview}
						onDownload={handleDownload}
						onEdit={handleEdit}
						onExtend={handleExtend}
						onSaveToLibrary={handleSaveToLibrary}
						onDelete={handleDelete}
						onRetry={handleRetry}
					/>
				</div>

				<!-- Controls -->
				<div class="mobile-controls">
					{#if $activeTab === 'image'}
						<ImageGenerator onStartGeneration={handleStartGeneration} />
					{:else if $activeTab === 'video'}
						<VideoGenerator onStartGeneration={handleStartGeneration} />
					{:else if $activeTab === 'tts'}
						<TTSGenerator />
					{:else if $activeTab === 'stt'}
						<STTTranscriber />
					{/if}
				</div>

				<!-- Generation History -->
				<div class="mobile-history">
					<GenerationHistory
						onSelect={handleHistorySelect}
						onRegenerate={handleRegenerate}
						onDelete={handleDelete}
					/>
				</div>
			</div>
		{:else}
			<!-- Desktop: Side-by-side layout -->
			<!-- Preview Area (60%) -->
			<div class="preview-column">
				<!-- Preview -->
				<div class="preview-content">
					<MediaPreview
						generation={currentPreview}
						onClear={clearPreview}
						onDownload={handleDownload}
						onEdit={handleEdit}
						onExtend={handleExtend}
						onSaveToLibrary={handleSaveToLibrary}
						onDelete={handleDelete}
						onRetry={handleRetry}
					/>
				</div>

				<!-- Generation History -->
				<div class="history-section">
					<GenerationHistory
						onSelect={handleHistorySelect}
						onRegenerate={handleRegenerate}
						onDelete={handleDelete}
					/>
				</div>
			</div>

			<!-- Controls Area (40%) -->
			<div class="controls-column">
				<!-- Tab Header -->
				<div class="tab-header">
					<div class="tab-buttons">
						<button
							type="button"
							onclick={() => handleTabChange('image')}
							class="tab-btn"
							class:active={$activeTab === 'image'}
							aria-pressed={$activeTab === 'image'}
						>
							<Image class="w-4 h-4" />
							Image
						</button>
						<button
							type="button"
							onclick={() => handleTabChange('video')}
							class="tab-btn"
							class:active={$activeTab === 'video'}
							aria-pressed={$activeTab === 'video'}
						>
							<Video class="w-4 h-4" />
							Video
						</button>
						<button
							type="button"
							onclick={() => handleTabChange('tts')}
							class="tab-btn"
							class:active={$activeTab === 'tts'}
							aria-pressed={$activeTab === 'tts'}
						>
							<Mic class="w-4 h-4" />
							Voice
						</button>
						<button
							type="button"
							onclick={() => handleTabChange('stt')}
							class="tab-btn"
							class:active={$activeTab === 'stt'}
							aria-pressed={$activeTab === 'stt'}
						>
							<FileAudio class="w-4 h-4" />
							Transcribe
						</button>
					</div>

					<button
						type="button"
						onclick={toggleAssetLibrary}
						class="library-btn desktop"
						aria-label="Toggle asset library"
						aria-pressed={showAssetLibrary}
					>
						<FolderOpen class="w-4 h-4" />
						<span>Library</span>
					</button>
				</div>

				<!-- Tab Content -->
				<div class="controls-content">
					{#if $activeTab === 'image'}
						<ImageGenerator onStartGeneration={handleStartGeneration} />
					{:else if $activeTab === 'video'}
						<VideoGenerator onStartGeneration={handleStartGeneration} />
					{:else if $activeTab === 'tts'}
						<TTSGenerator />
					{:else if $activeTab === 'stt'}
						<STTTranscriber />
					{/if}
				</div>
			</div>
		{/if}
	</div>

	<!-- Asset Library Overlay -->
	{#if showAssetLibrary}
		<div class="fixed inset-0 z-50 flex">
			<!-- Backdrop -->
			<button
				type="button"
				class="absolute inset-0 bg-black/50 backdrop-blur-sm"
				onclick={toggleAssetLibrary}
				aria-label="Close asset library"
			></button>

			<!-- Library Panel -->
			<div class="absolute right-0 top-0 bottom-0 w-full max-w-md bg-card border-l border-border shadow-2xl animate-slide-in-right">
				<AssetLibrary
					onSelect={handleAssetSelect}
					onClose={toggleAssetLibrary}
				/>
			</div>
		</div>
	{/if}
</div>

<style>
	/* Base layout */
	.studio-view {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: var(--background);
	}

	.studio-main {
		flex: 1;
		display: flex;
		min-height: 0;
	}

	/* Desktop layout */
	.preview-column {
		width: 60%;
		display: flex;
		flex-direction: column;
		border-right: 1px solid var(--border);
	}

	.preview-content {
		flex: 1;
		min-height: 0;
		padding: 1rem;
	}

	.history-section {
		flex-shrink: 0;
		border-top: 1px solid var(--border);
	}

	.controls-column {
		width: 40%;
		display: flex;
		flex-direction: column;
		min-height: 0;
	}

	.tab-header {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.75rem 1rem;
		border-bottom: 1px solid var(--border);
	}

	.tab-buttons {
		display: flex;
		gap: 0.25rem;
	}

	.tab-btn {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.5rem 0.75rem;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: 0.5rem;
		background: transparent;
		border: none;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
		transition: all 0.15s;
	}

	.tab-btn:hover {
		color: hsl(var(--foreground));
		background: hsl(var(--muted));
	}

	.tab-btn.active {
		background: hsl(var(--primary));
		color: hsl(var(--primary-foreground));
	}

	.library-btn {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.5rem 0.75rem;
		font-size: 0.875rem;
		color: hsl(var(--muted-foreground));
		background: transparent;
		border: none;
		border-radius: 0.5rem;
		cursor: pointer;
		transition: all 0.15s;
	}

	.library-btn:hover {
		color: hsl(var(--foreground));
		background: hsl(var(--muted));
	}

	.controls-content {
		flex: 1;
		overflow-y: auto;
	}

	/* Mobile layout */
	.studio-view.mobile .studio-main {
		flex-direction: column;
	}

	.mobile-tab-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.5rem;
		background: hsl(var(--card));
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.mobile-tab-header .tab-buttons {
		flex: 1;
		display: flex;
		gap: 0.125rem;
		overflow-x: auto;
		-webkit-overflow-scrolling: touch;
	}

	.mobile-tab-header .tab-btn {
		flex-shrink: 0;
		padding: 0.5rem 0.625rem;
		font-size: 0.75rem;
		gap: 0.25rem;
	}

	.mobile-tab-header .tab-btn span {
		display: none;
	}

	@media (min-width: 400px) {
		.mobile-tab-header .tab-btn span {
			display: inline;
		}
	}

	.mobile-tab-header .library-btn {
		flex-shrink: 0;
		padding: 0.625rem;
		margin-left: 0.5rem;
	}

	.mobile-content {
		flex: 1;
		overflow-y: auto;
		-webkit-overflow-scrolling: touch;
		display: flex;
		flex-direction: column;
	}

	.mobile-preview {
		min-height: 200px;
		max-height: 300px;
		padding: 0.75rem;
		background: hsl(var(--muted) / 0.3);
	}

	.mobile-controls {
		flex-shrink: 0;
		border-top: 1px solid var(--border);
	}

	.mobile-history {
		flex-shrink: 0;
		border-top: 1px solid var(--border);
		max-height: 150px;
		overflow-y: auto;
	}

	/* Animation */
	@keyframes slide-in-right {
		from {
			transform: translateX(100%);
		}
		to {
			transform: translateX(0);
		}
	}

	.animate-slide-in-right {
		animation: slide-in-right 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}
</style>

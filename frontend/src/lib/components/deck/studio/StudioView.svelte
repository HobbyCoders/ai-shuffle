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

	// Props
	interface Props {
		activeGeneration?: DeckGeneration | null;
		onStartGeneration?: (type: 'image' | 'video', config: unknown) => void;
	}

	let { activeGeneration = null, onStartGeneration }: Props = $props();

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

<div class="flex flex-col h-full bg-background">
	<!-- Main Content Area -->
	<div class="flex-1 flex min-h-0">
		<!-- Preview Area (60%) -->
		<div class="w-[60%] flex flex-col border-r border-border">
			<!-- Preview -->
			<div class="flex-1 min-h-0 p-4">
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
			<div class="shrink-0 border-t border-border">
				<GenerationHistory
					onSelect={handleHistorySelect}
					onRegenerate={handleRegenerate}
					onDelete={handleDelete}
				/>
			</div>
		</div>

		<!-- Controls Area (40%) -->
		<div class="w-[40%] flex flex-col min-h-0">
			<!-- Tab Header -->
			<div class="shrink-0 flex items-center justify-between px-4 py-3 border-b border-border">
				<div class="flex gap-1">
					<button
						type="button"
						onclick={() => handleTabChange('image')}
						class="flex items-center gap-2 px-3 py-2 text-sm font-medium rounded-lg transition-colors {$activeTab === 'image' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
						aria-pressed={$activeTab === 'image'}
					>
						<Image class="w-4 h-4" />
						Image
					</button>
					<button
						type="button"
						onclick={() => handleTabChange('video')}
						class="flex items-center gap-2 px-3 py-2 text-sm font-medium rounded-lg transition-colors {$activeTab === 'video' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
						aria-pressed={$activeTab === 'video'}
					>
						<Video class="w-4 h-4" />
						Video
					</button>
					<button
						type="button"
						onclick={() => handleTabChange('tts')}
						class="flex items-center gap-2 px-3 py-2 text-sm font-medium rounded-lg transition-colors {$activeTab === 'tts' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
						aria-pressed={$activeTab === 'tts'}
					>
						<Mic class="w-4 h-4" />
						Voice
					</button>
					<button
						type="button"
						onclick={() => handleTabChange('stt')}
						class="flex items-center gap-2 px-3 py-2 text-sm font-medium rounded-lg transition-colors {$activeTab === 'stt' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
						aria-pressed={$activeTab === 'stt'}
					>
						<FileAudio class="w-4 h-4" />
						Transcribe
					</button>
				</div>

				<button
					type="button"
					onclick={toggleAssetLibrary}
					class="flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground hover:text-foreground hover:bg-muted rounded-lg transition-colors"
					aria-label="Toggle asset library"
					aria-pressed={showAssetLibrary}
				>
					<FolderOpen class="w-4 h-4" />
					<span class="hidden sm:inline">Library</span>
				</button>
			</div>

			<!-- Tab Content -->
			<div class="flex-1 overflow-y-auto">
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

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
	import { studio, activeGeneration as storeActiveGeneration, activeTab } from '$lib/stores/studio';
	import type { ImageProvider, VideoProvider } from '$lib/stores/studio';

	// Props
	interface Props {
		activeGeneration?: DeckGeneration | null;
		onStartGeneration?: (type: 'image' | 'video', config: unknown) => void;
	}

	let { activeGeneration = null, onStartGeneration }: Props = $props();

	// Handle generation from child components
	async function handleStartGeneration(type: 'image' | 'video', config: unknown) {
		if (type === 'image') {
			const imageConfig = config as {
				prompt: string;
				provider: string;
				aspectRatio: string;
				style?: string;
			};
			await studio.generateImage(imageConfig.prompt, {
				provider: imageConfig.provider as ImageProvider,
				aspectRatio: imageConfig.aspectRatio,
				style: imageConfig.style
			});
		} else {
			const videoConfig = config as {
				prompt: string;
				provider: string;
				duration: number;
				aspectRatio: string;
				startFrame?: string;
			};
			await studio.generateVideo(videoConfig.prompt, {
				provider: videoConfig.provider as VideoProvider,
				aspectRatio: videoConfig.aspectRatio,
				duration: videoConfig.duration,
				sourceImage: videoConfig.startFrame
			});
		}
		// Also call the parent callback if provided
		onStartGeneration?.(type, config);
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
				/>
			</div>

			<!-- Generation History -->
			<div class="shrink-0 border-t border-border">
				<GenerationHistory
					onSelect={handleHistorySelect}
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

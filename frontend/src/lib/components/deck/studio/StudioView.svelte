<script lang="ts">
	import { Image, Video, FolderOpen, Mic, FileAudio, Box, Cpu, Zap, Clock } from 'lucide-svelte';
	import ImageGenerator from './ImageGenerator.svelte';
	import VideoGenerator from './VideoGenerator.svelte';
	import Model3DGenerator from './Model3DGenerator.svelte';
	import TTSGenerator from './TTSGenerator.svelte';
	import STTTranscriber from './STTTranscriber.svelte';
	import MediaPreview from './MediaPreview.svelte';
	import AssetLibrary from './AssetLibrary.svelte';
	import GenerationHistory from './GenerationHistory.svelte';
	import type { DeckGeneration } from '../types';
	import { studio, activeGeneration as storeActiveGeneration, activeTab, recentGenerations } from '$lib/stores/studio';
	import type { ImageProvider, VideoProvider, Model3DProvider, DeckGeneration as StoreDeckGeneration } from '$lib/stores/studio';
	import { onMount } from 'svelte';

	// Props
	interface Props {
		activeGeneration?: DeckGeneration | null;
		onStartGeneration?: (type: 'image' | 'video' | '3d', config: unknown) => void;
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
	async function handleStartGeneration(type: 'image' | 'video' | '3d', config: unknown) {
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
	let showHistorySidebar = $state(false);

	// Derived
	let currentPreview = $derived(selectedAsset || activeGeneration);

	// Mock data for status bar
	let currentModel = $derived.by(() => {
		if ($activeTab === 'image') return 'DALL-E 3';
		if ($activeTab === 'video') return 'Runway Gen-3';
		if ($activeTab === '3d') return 'Meshy 4';
		if ($activeTab === 'tts') return 'ElevenLabs';
		return 'Whisper';
	});

	let generationQueue = $derived($recentGenerations.filter(g => g.status === 'processing').length);

	// Handlers
	function handleTabChange(tab: 'image' | 'video' | '3d' | 'tts' | 'stt') {
		studio.setActiveTab(tab);
	}

	function toggleAssetLibrary() {
		showAssetLibrary = !showAssetLibrary;
	}

	function toggleHistorySidebar() {
		showHistorySidebar = !showHistorySidebar;
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
	<!-- Cosmic background mesh gradient -->
	<div class="cosmic-background"></div>

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
						onclick={() => handleTabChange('3d')}
						class="tab-btn"
						class:active={$activeTab === '3d'}
						aria-pressed={$activeTab === '3d'}
					>
						<Box class="w-4 h-4" />
						<span>3D</span>
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
					{:else if $activeTab === '3d'}
						<Model3DGenerator onStartGeneration={handleStartGeneration} />
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
			<!-- Desktop: Side-by-side layout with floating tab bar -->

			<!-- Preview Area (60%) -->
			<div class="preview-column">
				<!-- Floating Tab Navigation -->
				<div class="floating-tab-container">
					<div class="floating-tab-bar">
						<div class="tab-buttons-cosmic">
							<button
								type="button"
								onclick={() => handleTabChange('image')}
								class="cosmic-tab"
								class:active={$activeTab === 'image'}
								aria-pressed={$activeTab === 'image'}
							>
								<Image class="w-4 h-4" />
								<span>Image</span>
							</button>
							<button
								type="button"
								onclick={() => handleTabChange('video')}
								class="cosmic-tab"
								class:active={$activeTab === 'video'}
								aria-pressed={$activeTab === 'video'}
							>
								<Video class="w-4 h-4" />
								<span>Video</span>
							</button>
							<button
								type="button"
								onclick={() => handleTabChange('3d')}
								class="cosmic-tab"
								class:active={$activeTab === '3d'}
								aria-pressed={$activeTab === '3d'}
							>
								<Box class="w-4 h-4" />
								<span>3D Model</span>
							</button>
							<button
								type="button"
								onclick={() => handleTabChange('tts')}
								class="cosmic-tab"
								class:active={$activeTab === 'tts'}
								aria-pressed={$activeTab === 'tts'}
							>
								<Mic class="w-4 h-4" />
								<span>Voice</span>
							</button>
							<button
								type="button"
								onclick={() => handleTabChange('stt')}
								class="cosmic-tab"
								class:active={$activeTab === 'stt'}
								aria-pressed={$activeTab === 'stt'}
							>
								<FileAudio class="w-4 h-4" />
								<span>Transcribe</span>
							</button>
						</div>

						<div class="tab-actions">
							<button
								type="button"
								onclick={toggleAssetLibrary}
								class="action-btn"
								aria-label="Toggle asset library"
								aria-pressed={showAssetLibrary}
							>
								<FolderOpen class="w-4 h-4" />
								<span>Library</span>
							</button>
						</div>
					</div>
				</div>

				<!-- Preview with vignette -->
				<div class="preview-content-cosmic">
					<div class="preview-vignette">
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
				</div>

				<!-- Generation History -->
				<div class="history-section-cosmic">
					<GenerationHistory
						onSelect={handleHistorySelect}
						onRegenerate={handleRegenerate}
						onDelete={handleDelete}
					/>
				</div>
			</div>

			<!-- Controls Area (40%) -->
			<div class="controls-column-cosmic">
				<!-- Glass panel with depth -->
				<div class="controls-glass-panel">
					<!-- Tab Content -->
					<div class="controls-content-cosmic">
						{#if $activeTab === 'image'}
							<ImageGenerator onStartGeneration={handleStartGeneration} />
						{:else if $activeTab === 'video'}
							<VideoGenerator onStartGeneration={handleStartGeneration} />
						{:else if $activeTab === '3d'}
							<Model3DGenerator onStartGeneration={handleStartGeneration} />
						{:else if $activeTab === 'tts'}
							<TTSGenerator />
						{:else if $activeTab === 'stt'}
							<STTTranscriber />
						{/if}
					</div>
				</div>
			</div>

			<!-- Status Bar -->
			<div class="status-bar-cosmic">
				<div class="status-item">
					<Cpu class="w-3.5 h-3.5" />
					<span class="status-label">Model:</span>
					<span class="status-value">{currentModel}</span>
				</div>

				<div class="status-divider"></div>

				<div class="status-item">
					<Zap class="w-3.5 h-3.5" />
					<span class="status-label">Credits:</span>
					<span class="status-value">2,847</span>
				</div>

				<div class="status-divider"></div>

				<div class="status-item">
					<Clock class="w-3.5 h-3.5" />
					<span class="status-label">Queue:</span>
					<span class="status-value">{generationQueue} active</span>
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

			<!-- Library Panel with glass effect -->
			<div class="library-panel-cosmic">
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
		position: relative;
		display: flex;
		flex-direction: column;
		height: 100%;
		background: var(--background);
		overflow: hidden;
	}

	/* ========================================
	   COSMIC FORGE BACKGROUND
	   ======================================== */
	.cosmic-background {
		position: absolute;
		inset: 0;
		background:
			radial-gradient(circle at 20% 30%, oklch(0.25 0.08 240 / 0.15) 0%, transparent 50%),
			radial-gradient(circle at 80% 70%, oklch(0.30 0.10 180 / 0.12) 0%, transparent 50%),
			radial-gradient(circle at 50% 50%, oklch(0.20 0.05 200 / 0.08) 0%, transparent 70%);
		animation: cosmicShift 20s ease-in-out infinite;
		pointer-events: none;
		z-index: 0;
	}

	@keyframes cosmicShift {
		0%, 100% {
			opacity: 0.5;
			transform: scale(1) rotate(0deg);
		}
		50% {
			opacity: 0.7;
			transform: scale(1.1) rotate(2deg);
		}
	}

	.studio-main {
		position: relative;
		flex: 1;
		display: flex;
		min-height: 0;
		z-index: 1;
	}

	/* ========================================
	   DESKTOP LAYOUT - COSMIC FORGE
	   ======================================== */
	.preview-column {
		width: 60%;
		display: flex;
		flex-direction: column;
		position: relative;
	}

	/* Floating Tab Bar */
	.floating-tab-container {
		position: absolute;
		top: 1.5rem;
		left: 50%;
		transform: translateX(-50%);
		z-index: 100;
		width: max-content;
		max-width: calc(100% - 3rem);
	}

	.floating-tab-bar {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		padding: 0.5rem 0.75rem;
		background: var(--glass-bg);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border: 1px solid var(--glass-border);
		border-radius: 2rem;
		box-shadow:
			0 0 0 1px var(--panel-shadow-inset) inset,
			0 8px 32px var(--panel-shadow-outer),
			0 4px 16px oklch(0 0 0 / 0.3),
			0 0 20px var(--glow-color-soft);
		animation: floatIn 0.6s cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes floatIn {
		from {
			opacity: 0;
			transform: translateY(-20px) scale(0.95);
		}
		to {
			opacity: 1;
			transform: translateY(0) scale(1);
		}
	}

	.tab-buttons-cosmic {
		display: flex;
		gap: 0.25rem;
	}

	.cosmic-tab {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.625rem 1rem;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: 1.5rem;
		background: transparent;
		border: none;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
		position: relative;
		white-space: nowrap;
	}

	.cosmic-tab::before {
		content: '';
		position: absolute;
		inset: 0;
		border-radius: 1.5rem;
		background: var(--primary);
		opacity: 0;
		transition: opacity 0.25s cubic-bezier(0.16, 1, 0.3, 1);
		z-index: -1;
	}

	.cosmic-tab:hover:not(.active) {
		color: var(--foreground);
		background: var(--accent);
	}

	.cosmic-tab.active {
		color: var(--primary-foreground);
	}

	.cosmic-tab.active::before {
		opacity: 1;
		animation: tabPulse 2s ease-in-out infinite;
	}

	.cosmic-tab.active {
		box-shadow: 0 0 20px var(--glow-color);
	}

	@keyframes tabPulse {
		0%, 100% {
			box-shadow: 0 0 20px var(--glow-color);
		}
		50% {
			box-shadow: 0 0 30px var(--glow-color), 0 0 10px var(--glow-color-soft);
		}
	}

	.tab-actions {
		display: flex;
		gap: 0.5rem;
		padding-left: 0.5rem;
		border-left: 1px solid var(--glass-border);
	}

	.action-btn {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.5rem 1rem;
		font-size: 0.875rem;
		color: var(--muted-foreground);
		background: transparent;
		border: none;
		border-radius: 1.5rem;
		cursor: pointer;
		transition: all 0.2s;
		white-space: nowrap;
	}

	.action-btn:hover {
		color: var(--foreground);
		background: var(--accent);
	}

	/* Preview with vignette */
	.preview-content-cosmic {
		flex: 1;
		min-height: 0;
		padding: 6rem 1.5rem 1.5rem;
		position: relative;
	}

	.preview-vignette {
		position: relative;
		width: 100%;
		height: 100%;
		border-radius: 1rem;
		overflow: hidden;
	}

	.preview-vignette::before {
		content: '';
		position: absolute;
		inset: 0;
		border-radius: 1rem;
		background: radial-gradient(circle at center, transparent 40%, oklch(0.14 0.008 260 / 0.6) 100%);
		pointer-events: none;
		z-index: 1;
	}

	.history-section-cosmic {
		flex-shrink: 0;
		border-top: 1px solid var(--border);
		background: oklch(0.16 0.008 260 / 0.7);
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
	}

	/* Controls with layered depth */
	.controls-column-cosmic {
		width: 40%;
		display: flex;
		flex-direction: column;
		min-height: 0;
		border-left: 1px solid var(--border);
		position: relative;
	}

	.controls-column-cosmic::before {
		content: '';
		position: absolute;
		inset: 0;
		background: linear-gradient(
			to bottom,
			oklch(0.18 0.008 260 / 0.5) 0%,
			oklch(0.16 0.008 260 / 0.8) 100%
		);
		pointer-events: none;
	}

	.controls-glass-panel {
		position: relative;
		flex: 1;
		display: flex;
		flex-direction: column;
		min-height: 0;
		z-index: 1;
	}

	.controls-content-cosmic {
		flex: 1;
		overflow-y: auto;
		padding: 1.5rem;
		position: relative;
	}

	.controls-content-cosmic::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		height: 2px;
		background: linear-gradient(
			to right,
			transparent,
			var(--glow-color-soft),
			transparent
		);
	}

	/* ========================================
	   STATUS BAR - COSMIC FORGE
	   ======================================== */
	.status-bar-cosmic {
		position: fixed;
		bottom: 0;
		left: 0;
		right: 0;
		height: 2.5rem;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 1.5rem;
		padding: 0 1.5rem;
		background: var(--glass-bg);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border-top: 1px solid var(--glass-border);
		box-shadow:
			0 0 0 1px var(--panel-shadow-inset) inset,
			0 -4px 16px oklch(0 0 0 / 0.2);
		z-index: 50;
		font-size: 0.75rem;
	}

	.status-item {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		color: var(--muted-foreground);
	}

	.status-item svg {
		opacity: 0.7;
	}

	.status-label {
		font-weight: 500;
	}

	.status-value {
		font-weight: 600;
		color: var(--foreground);
	}

	.status-divider {
		width: 1px;
		height: 1rem;
		background: var(--border);
	}

	/* ========================================
	   MOBILE LAYOUT
	   ======================================== */
	.studio-view.mobile .studio-main {
		flex-direction: column;
	}

	.mobile-tab-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.5rem;
		background: var(--glass-bg);
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
		position: relative;
		z-index: 10;
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
		display: flex;
		align-items: center;
		gap: 0.25rem;
		padding: 0.5rem 0.625rem;
		font-size: 0.75rem;
		font-weight: 500;
		border-radius: 0.5rem;
		background: transparent;
		border: none;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.15s;
	}

	.mobile-tab-header .tab-btn span {
		display: none;
	}

	@media (min-width: 400px) {
		.mobile-tab-header .tab-btn span {
			display: inline;
		}
	}

	.mobile-tab-header .tab-btn:hover {
		color: var(--foreground);
		background: var(--muted);
	}

	.mobile-tab-header .tab-btn.active {
		background: var(--primary);
		color: var(--primary-foreground);
		box-shadow: 0 0 10px var(--glow-color);
	}

	.mobile-tab-header .library-btn {
		flex-shrink: 0;
		padding: 0.625rem;
		margin-left: 0.5rem;
		color: var(--muted-foreground);
		background: transparent;
		border: none;
		border-radius: 0.5rem;
		cursor: pointer;
		transition: all 0.15s;
	}

	.mobile-tab-header .library-btn:hover {
		color: var(--foreground);
		background: var(--muted);
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
		background: oklch(0.16 0.008 260 / 0.5);
	}

	.mobile-controls {
		flex-shrink: 0;
		border-top: 1px solid var(--border);
		padding: 0.75rem;
		background: var(--glass-bg);
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
	}

	.mobile-history {
		flex-shrink: 0;
		border-top: 1px solid var(--border);
		max-height: 150px;
		overflow-y: auto;
		background: oklch(0.16 0.008 260 / 0.7);
	}

	/* ========================================
	   ASSET LIBRARY PANEL
	   ======================================== */
	.library-panel-cosmic {
		position: absolute;
		right: 0;
		top: 0;
		bottom: 0;
		width: 100%;
		max-width: 28rem;
		background: var(--panel-bg);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border-left: 1px solid var(--panel-border);
		box-shadow:
			0 0 0 1px var(--panel-shadow-inset) inset,
			0 8px 32px var(--panel-shadow-outer),
			0 0 40px var(--glow-color-soft);
		animation: slideInRight 0.3s cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes slideInRight {
		from {
			transform: translateX(100%);
			opacity: 0;
		}
		to {
			transform: translateX(0);
			opacity: 1;
		}
	}

	/* Desktop: adjust for status bar */
	@media (min-width: 768px) {
		.studio-main {
			padding-bottom: 2.5rem;
		}
	}
</style>

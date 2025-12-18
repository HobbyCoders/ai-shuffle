<script lang="ts">
	import {
		Download,
		Edit,
		Trash2,
		Save,
		Play,
		Pause,
		RefreshCw,
		X,
		ZoomIn,
		ZoomOut,
		Maximize2,
		AlertCircle,
		ImageIcon,
		Film
	} from 'lucide-svelte';
	import type { DeckGeneration } from '../types';

	// Props
	interface Props {
		generation?: DeckGeneration | null;
		onClear?: () => void;
		onDownload?: (generation: DeckGeneration) => void;
		onEdit?: (generation: DeckGeneration) => void;
		onExtend?: (generation: DeckGeneration) => void;
		onSaveToLibrary?: (generation: DeckGeneration) => void;
		onDelete?: (generation: DeckGeneration) => void;
		onRetry?: (generation: DeckGeneration) => void;
	}

	let {
		generation = null,
		onClear,
		onDownload,
		onEdit,
		onExtend,
		onSaveToLibrary,
		onDelete,
		onRetry
	}: Props = $props();

	// State
	let isZoomed = $state(false);
	let isPanning = $state(false);
	let panOffset = $state({ x: 0, y: 0 });
	let panStart = $state({ x: 0, y: 0 });
	let isPlaying = $state(false);
	let videoElement: HTMLVideoElement | null = $state(null);

	// Derived
	let isLoading = $derived(generation?.status === 'pending' || generation?.status === 'generating');
	let hasError = $derived(generation?.status === 'error');
	let isComplete = $derived(generation?.status === 'complete');
	let isImage = $derived(generation?.type === 'image');
	let isVideo = $derived(generation?.type === 'video');
	let progressPercent = $derived(generation?.progress ?? 0);

	// Handlers
	function handleZoomToggle() {
		isZoomed = !isZoomed;
		if (!isZoomed) {
			panOffset = { x: 0, y: 0 };
		}
	}

	function handleMouseDown(event: MouseEvent) {
		if (!isZoomed) return;
		isPanning = true;
		panStart = { x: event.clientX - panOffset.x, y: event.clientY - panOffset.y };
	}

	function handleMouseMove(event: MouseEvent) {
		if (!isPanning || !isZoomed) return;
		panOffset = {
			x: event.clientX - panStart.x,
			y: event.clientY - panStart.y
		};
	}

	function handleMouseUp() {
		isPanning = false;
	}

	function handlePlayPause() {
		if (!videoElement) return;
		if (isPlaying) {
			videoElement.pause();
		} else {
			videoElement.play();
		}
		isPlaying = !isPlaying;
	}

	function handleVideoEnded() {
		isPlaying = false;
	}

	function handleDownload() {
		if (generation) onDownload?.(generation);
	}

	function handleEdit() {
		if (generation) onEdit?.(generation);
	}

	function handleExtend() {
		if (generation) onExtend?.(generation);
	}

	function handleSave() {
		if (generation) onSaveToLibrary?.(generation);
	}

	function handleDelete() {
		if (generation) onDelete?.(generation);
	}

	function handleRetry() {
		if (generation) onRetry?.(generation);
	}

	function handleClear() {
		onClear?.();
	}

	// Check if provider supports editing
	function supportsEdit(): boolean {
		// Mock: google-gemini and openai-gpt-image support editing
		return true;
	}

	// Check if provider supports extend (Veo only)
	function supportsExtend(): boolean {
		return isVideo;
	}
</script>

<div class="h-full flex flex-col bg-black/20 rounded-xl border border-border overflow-hidden">
	{#if !generation}
		<!-- Empty State -->
		<div class="flex-1 flex flex-col items-center justify-center text-muted-foreground p-8">
			<div class="w-16 h-16 rounded-full bg-muted/50 flex items-center justify-center mb-4">
				<ImageIcon class="w-8 h-8" />
			</div>
			<p class="text-sm font-medium">No preview</p>
			<p class="text-xs mt-1">Generate something to see it here</p>
		</div>
	{:else if isLoading}
		<!-- Loading State -->
		<div class="flex-1 flex flex-col items-center justify-center p-8">
			<!-- Skeleton placeholder -->
			<div class="relative w-full max-w-md aspect-video bg-muted/30 rounded-lg overflow-hidden animate-pulse">
				<div class="absolute inset-0 flex flex-col items-center justify-center gap-4">
					<!-- Progress ring -->
					<div class="relative w-20 h-20">
						<svg class="w-20 h-20 transform -rotate-90" viewBox="0 0 80 80">
							<circle
								cx="40"
								cy="40"
								r="36"
								fill="none"
								stroke="currentColor"
								stroke-width="4"
								class="text-muted-foreground/20"
							/>
							<circle
								cx="40"
								cy="40"
								r="36"
								fill="none"
								stroke="currentColor"
								stroke-width="4"
								stroke-linecap="round"
								stroke-dasharray={226.19}
								stroke-dashoffset={226.19 - (226.19 * progressPercent) / 100}
								class="text-primary transition-all duration-300"
							/>
						</svg>
						<div class="absolute inset-0 flex items-center justify-center">
							<span class="text-sm font-medium text-foreground">{progressPercent}%</span>
						</div>
					</div>
					<!-- Status text -->
					<div class="text-center">
						<p class="text-sm font-medium text-foreground">
							{generation.status === 'pending' ? 'Queued...' : 'Generating...'}
						</p>
						<p class="text-xs text-muted-foreground mt-1 max-w-xs truncate">
							{generation.prompt}
						</p>
					</div>
				</div>
			</div>
		</div>
	{:else if hasError}
		<!-- Error State -->
		<div class="flex-1 flex flex-col items-center justify-center p-8">
			<div class="w-16 h-16 rounded-full bg-destructive/10 flex items-center justify-center mb-4">
				<AlertCircle class="w-8 h-8 text-destructive" />
			</div>
			<p class="text-sm font-medium text-foreground">Generation failed</p>
			<p class="text-xs text-muted-foreground mt-1 max-w-xs text-center">
				Something went wrong. Please try again.
			</p>
			<div class="flex gap-2 mt-4">
				<button
					type="button"
					onclick={handleRetry}
					class="flex items-center gap-2 px-4 py-2 text-sm font-medium bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors"
				>
					<RefreshCw class="w-4 h-4" />
					Retry
				</button>
				<button
					type="button"
					onclick={handleClear}
					class="flex items-center gap-2 px-4 py-2 text-sm font-medium bg-muted text-foreground border border-border rounded-lg hover:bg-muted/80 transition-colors"
				>
					<X class="w-4 h-4" />
					Dismiss
				</button>
			</div>
		</div>
	{:else if isComplete}
		<!-- Complete State - Image or Video -->
		<div class="flex-1 flex flex-col min-h-0">
			<!-- Media Display -->
			<div
				class="flex-1 relative flex items-center justify-center overflow-hidden {isZoomed ? 'cursor-grab' : 'cursor-zoom-in'} {isPanning ? 'cursor-grabbing' : ''}"
				role="button"
				tabindex="0"
				aria-label={isZoomed ? 'Click to zoom out' : 'Click to zoom in'}
				onclick={!isVideo ? handleZoomToggle : undefined}
				onmousedown={isImage ? handleMouseDown : undefined}
				onmousemove={isImage ? handleMouseMove : undefined}
				onmouseup={isImage ? handleMouseUp : undefined}
				onmouseleave={isImage ? handleMouseUp : undefined}
				onkeydown={(e) => e.key === 'Enter' && !isVideo && handleZoomToggle()}
			>
				{#if isImage && generation.resultUrl}
					<img
						src={generation.resultUrl}
						alt={generation.prompt}
						class="max-w-full max-h-full object-contain transition-transform duration-200 {isZoomed ? 'scale-150' : ''}"
						style={isZoomed ? `transform: scale(1.5) translate(${panOffset.x / 1.5}px, ${panOffset.y / 1.5}px)` : ''}
						draggable="false"
					/>
				{:else if isVideo && generation.resultUrl}
					<div class="relative">
						<video
							bind:this={videoElement}
							src={generation.resultUrl}
							class="max-w-full max-h-full object-contain rounded-lg"
							onended={handleVideoEnded}
							onplay={() => isPlaying = true}
							onpause={() => isPlaying = false}
						>
							<track kind="captions" />
						</video>
						<!-- Play/Pause overlay -->
						<button
							type="button"
							onclick={handlePlayPause}
							class="absolute inset-0 flex items-center justify-center bg-black/30 opacity-0 hover:opacity-100 transition-opacity"
							aria-label={isPlaying ? 'Pause video' : 'Play video'}
						>
							<div class="w-16 h-16 rounded-full bg-white/90 flex items-center justify-center">
								{#if isPlaying}
									<Pause class="w-8 h-8 text-black" />
								{:else}
									<Play class="w-8 h-8 text-black ml-1" />
								{/if}
							</div>
						</button>
					</div>
				{/if}

				<!-- Zoom indicator for images -->
				{#if isImage && !isZoomed}
					<div class="absolute bottom-4 right-4 flex items-center gap-1 px-2 py-1 rounded bg-black/50 text-white/80 text-xs">
						<ZoomIn class="w-3 h-3" />
						Click to zoom
					</div>
				{/if}
			</div>

			<!-- Actions Bar -->
			<div class="shrink-0 flex items-center justify-between px-4 py-3 border-t border-border bg-card/50">
				<div class="flex items-center gap-1">
					<!-- Type badge -->
					<div class="flex items-center gap-1 px-2 py-1 rounded bg-muted text-xs text-muted-foreground">
						{#if isImage}
							<ImageIcon class="w-3 h-3" />
							Image
						{:else}
							<Film class="w-3 h-3" />
							Video
						{/if}
					</div>
				</div>

				<div class="flex items-center gap-1">
					<button
						type="button"
						onclick={handleDownload}
						class="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
						aria-label="Download"
						title="Download"
					>
						<Download class="w-4 h-4" />
					</button>

					{#if supportsEdit()}
						<button
							type="button"
							onclick={handleEdit}
							class="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
							aria-label="Edit"
							title="Edit"
						>
							<Edit class="w-4 h-4" />
						</button>
					{/if}

					{#if supportsExtend()}
						<button
							type="button"
							onclick={handleExtend}
							class="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
							aria-label="Extend video"
							title="Extend"
						>
							<Maximize2 class="w-4 h-4" />
						</button>
					{/if}

					<button
						type="button"
						onclick={handleSave}
						class="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
						aria-label="Save to library"
						title="Save to library"
					>
						<Save class="w-4 h-4" />
					</button>

					<div class="w-px h-5 bg-border mx-1"></div>

					<button
						type="button"
						onclick={handleDelete}
						class="p-2 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
						aria-label="Delete"
						title="Delete"
					>
						<Trash2 class="w-4 h-4" />
					</button>
				</div>
			</div>
		</div>
	{/if}
</div>

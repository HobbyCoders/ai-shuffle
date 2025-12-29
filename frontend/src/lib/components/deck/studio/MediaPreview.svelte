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
		Film,
		Sparkles,
		Volume2,
		Info,
		ChevronDown,
		Share2
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
	let zoomLevel = $state(1);
	let isPanning = $state(false);
	let panOffset = $state({ x: 0, y: 0 });
	let panStart = $state({ x: 0, y: 0 });
	let isPlaying = $state(false);
	let videoElement: HTMLVideoElement | null = $state(null);
	let showMetadata = $state(false);

	// Derived
	let isLoading = $derived(generation?.status === 'pending' || generation?.status === 'generating');
	let hasError = $derived(generation?.status === 'error');
	let isComplete = $derived(generation?.status === 'complete');
	let isImage = $derived(generation?.type === 'image');
	let isVideo = $derived(generation?.type === 'video');
	let isAudio = $derived(generation?.type === 'audio');
	let progressPercent = $derived(generation?.progress ?? 0);

	// Handlers
	function handleZoomIn() {
		zoomLevel = Math.min(zoomLevel + 0.5, 3);
		isZoomed = zoomLevel > 1;
	}

	function handleZoomOut() {
		zoomLevel = Math.max(zoomLevel - 0.5, 1);
		isZoomed = zoomLevel > 1;
		if (!isZoomed) {
			panOffset = { x: 0, y: 0 };
		}
	}

	function handleZoomReset() {
		zoomLevel = 1;
		isZoomed = false;
		panOffset = { x: 0, y: 0 };
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

	function handleWheel(event: WheelEvent) {
		if (!isImage) return;
		event.preventDefault();
		if (event.deltaY < 0) {
			handleZoomIn();
		} else {
			handleZoomOut();
		}
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

	function handleShare() {
		// Placeholder for future share functionality
	}

	// Check if provider supports editing
	function supportsEdit(): boolean {
		return true;
	}

	// Check if provider supports extend (Veo only)
	function supportsExtend(): boolean {
		return isVideo;
	}
</script>

<div class="cosmic-preview h-full flex flex-col overflow-hidden">
	{#if !generation}
		<!-- Empty State - Cosmic Forge Style -->
		<div class="flex-1 flex flex-col items-center justify-center p-8 relative overflow-hidden">
			<!-- Animated background gradient -->
			<div class="absolute inset-0 cosmic-gradient opacity-30"></div>

			<div class="relative z-10 flex flex-col items-center">
				<!-- Glowing icon container -->
				<div class="glow-container mb-6">
					<div class="w-20 h-20 rounded-2xl glass-panel flex items-center justify-center border border-primary/20">
						<Sparkles class="w-10 h-10 text-primary animate-pulse-soft" />
					</div>
				</div>

				<h3 class="text-lg font-semibold text-foreground mb-2">No Preview</h3>
				<p class="text-sm text-muted-foreground text-center max-w-xs">
					Generate images, videos, or audio to see them here
				</p>
			</div>
		</div>
	{:else if isLoading}
		<!-- Loading State - Cosmic Forge Style -->
		<div class="flex-1 flex flex-col items-center justify-center p-8 relative overflow-hidden">
			<!-- Animated background -->
			<div class="absolute inset-0 cosmic-gradient opacity-20 animate-pulse-slow"></div>

			<div class="relative z-10 flex flex-col items-center">
				<!-- Cosmic progress ring -->
				<div class="relative w-32 h-32 mb-8">
					<!-- Outer glow ring -->
					<div class="absolute inset-0 rounded-full bg-primary/20 blur-xl animate-pulse-soft"></div>

					<!-- Progress ring -->
					<svg class="w-32 h-32 transform -rotate-90 relative z-10" viewBox="0 0 128 128">
						<!-- Background circle -->
						<circle
							cx="64"
							cy="64"
							r="56"
							fill="none"
							stroke="currentColor"
							stroke-width="3"
							class="text-muted-foreground/20"
						/>
						<!-- Progress circle -->
						<circle
							cx="64"
							cy="64"
							r="56"
							fill="none"
							stroke="currentColor"
							stroke-width="3"
							stroke-linecap="round"
							stroke-dasharray={351.86}
							stroke-dashoffset={351.86 - (351.86 * progressPercent) / 100}
							class="text-primary transition-all duration-500 drop-shadow-[0_0_8px_var(--primary)]"
						/>
					</svg>

					<!-- Center content -->
					<div class="absolute inset-0 flex flex-col items-center justify-center">
						<span class="text-2xl font-bold text-foreground">{progressPercent}%</span>
						<span class="text-xs text-muted-foreground mt-1">
							{generation.status === 'pending' ? 'Queued' : 'Generating'}
						</span>
					</div>
				</div>

				<!-- Prompt preview -->
				<div class="glass-panel px-6 py-3 rounded-full border border-primary/20 max-w-md">
					<p class="text-sm text-foreground text-center truncate">
						{generation.prompt}
					</p>
				</div>
			</div>
		</div>
	{:else if hasError}
		<!-- Error State - Cosmic Forge Style -->
		<div class="flex-1 flex flex-col items-center justify-center p-8 relative overflow-hidden">
			<!-- Error gradient -->
			<div class="absolute inset-0 bg-gradient-to-br from-destructive/10 to-transparent"></div>

			<div class="relative z-10 flex flex-col items-center">
				<!-- Error icon -->
				<div class="w-20 h-20 rounded-2xl glass-panel flex items-center justify-center border border-destructive/30 mb-6">
					<AlertCircle class="w-10 h-10 text-destructive" />
				</div>

				<h3 class="text-lg font-semibold text-foreground mb-2">Generation Failed</h3>
				<p class="text-sm text-muted-foreground text-center max-w-xs mb-6">
					Something went wrong during generation. Please try again.
				</p>

				<!-- Action buttons -->
				<div class="flex gap-3">
					<button
						type="button"
						onclick={handleRetry}
						class="action-btn action-btn-primary"
					>
						<RefreshCw class="w-4 h-4" />
						Retry
					</button>
					<button
						type="button"
						onclick={handleClear}
						class="action-btn action-btn-secondary"
					>
						<X class="w-4 h-4" />
						Dismiss
					</button>
				</div>
			</div>
		</div>
	{:else if isComplete}
		<!-- Complete State - Media Display -->
		<div class="flex-1 flex flex-col min-h-0 relative">
			<!-- Media Display Area -->
			<div
				class="flex-1 relative flex items-center justify-center overflow-hidden bg-black/20 {isZoomed && isImage ? 'cursor-grab' : ''} {isPanning ? 'cursor-grabbing' : ''}"
				role="button"
				tabindex="0"
				aria-label="Media viewer"
				onmousedown={isImage ? handleMouseDown : undefined}
				onmousemove={isImage ? handleMouseMove : undefined}
				onmouseup={isImage ? handleMouseUp : undefined}
				onmouseleave={isImage ? handleMouseUp : undefined}
				onwheel={isImage ? handleWheel : undefined}
			>
				{#if isImage && generation.resultUrl}
					<!-- Image Display -->
					<img
						src={generation.resultUrl}
						alt={generation.prompt}
						class="max-w-full max-h-full object-contain transition-transform duration-300 select-none"
						style={isZoomed ? `transform: scale(${zoomLevel}) translate(${panOffset.x / zoomLevel}px, ${panOffset.y / zoomLevel}px)` : ''}
						draggable="false"
					/>

					<!-- Zoom controls for images -->
					{#if isImage}
						<div class="absolute top-4 right-4 flex flex-col gap-2">
							<button
								type="button"
								onclick={handleZoomIn}
								disabled={zoomLevel >= 3}
								class="zoom-btn"
								aria-label="Zoom in"
								title="Zoom in"
							>
								<ZoomIn class="w-4 h-4" />
							</button>
							<button
								type="button"
								onclick={handleZoomOut}
								disabled={zoomLevel <= 1}
								class="zoom-btn"
								aria-label="Zoom out"
								title="Zoom out"
							>
								<ZoomOut class="w-4 h-4" />
							</button>
							{#if zoomLevel > 1}
								<button
									type="button"
									onclick={handleZoomReset}
									class="zoom-btn"
									aria-label="Reset zoom"
									title="Reset zoom"
								>
									<Maximize2 class="w-4 h-4" />
								</button>
							{/if}
						</div>
					{/if}
				{:else if isVideo && generation.resultUrl}
					<!-- Video Display -->
					<div class="relative max-w-full max-h-full">
						<video
							bind:this={videoElement}
							src={generation.resultUrl}
							class="max-w-full max-h-full object-contain rounded-lg shadow-2xl"
							onended={handleVideoEnded}
							onplay={() => isPlaying = true}
							onpause={() => isPlaying = false}
						>
							<track kind="captions" />
						</video>

						<!-- Video play/pause overlay -->
						<button
							type="button"
							onclick={handlePlayPause}
							class="absolute inset-0 flex items-center justify-center bg-black/0 hover:bg-black/30 transition-colors group"
							aria-label={isPlaying ? 'Pause video' : 'Play video'}
						>
							<div class="video-play-btn opacity-0 group-hover:opacity-100 transition-opacity">
								{#if isPlaying}
									<Pause class="w-10 h-10 text-white" />
								{:else}
									<Play class="w-10 h-10 text-white ml-1" />
								{/if}
							</div>
						</button>
					</div>
				{:else if isAudio && generation.resultUrl}
					<!-- Audio Display -->
					<div class="w-full max-w-2xl px-8">
						<div class="glass-panel p-8 rounded-2xl border border-primary/20">
							<!-- Audio icon -->
							<div class="flex justify-center mb-6">
								<div class="w-24 h-24 rounded-full bg-primary/10 flex items-center justify-center">
									<Volume2 class="w-12 h-12 text-primary" />
								</div>
							</div>

							<!-- Audio player -->
							<audio
								src={generation.resultUrl}
								controls
								class="w-full audio-player"
							>
								<track kind="captions" />
							</audio>

							<!-- Prompt -->
							<div class="mt-6 text-center">
								<p class="text-sm text-muted-foreground line-clamp-2">
									{generation.prompt}
								</p>
							</div>
						</div>
					</div>
				{/if}
			</div>

			<!-- Floating Action Bar -->
			<div class="absolute bottom-6 left-1/2 -translate-x-1/2 z-20">
				<div class="floating-actions">
					<button
						type="button"
						onclick={handleDownload}
						class="action-icon"
						aria-label="Download"
						title="Download"
					>
						<Download class="w-5 h-5" />
					</button>

					{#if supportsEdit()}
						<button
							type="button"
							onclick={handleEdit}
							class="action-icon"
							aria-label="Edit"
							title="Edit"
						>
							<Edit class="w-5 h-5" />
						</button>
					{/if}

					{#if supportsExtend()}
						<button
							type="button"
							onclick={handleExtend}
							class="action-icon"
							aria-label="Extend video"
							title="Extend video"
						>
							<Sparkles class="w-5 h-5" />
						</button>
					{/if}

					<button
						type="button"
						onclick={handleSave}
						class="action-icon"
						aria-label="Save to library"
						title="Save to library"
					>
						<Save class="w-5 h-5" />
					</button>

					<button
						type="button"
						onclick={handleShare}
						class="action-icon"
						aria-label="Share"
						title="Share"
					>
						<Share2 class="w-5 h-5" />
					</button>

					<div class="w-px h-8 bg-white/10"></div>

					<button
						type="button"
						onclick={handleDelete}
						class="action-icon action-icon-danger"
						aria-label="Delete"
						title="Delete"
					>
						<Trash2 class="w-5 h-5" />
					</button>
				</div>
			</div>

			<!-- Metadata Panel (Collapsible) -->
			<div class="metadata-panel {showMetadata ? 'metadata-panel-open' : ''}">
				<!-- Toggle button -->
				<button
					type="button"
					onclick={() => showMetadata = !showMetadata}
					class="metadata-toggle"
				>
					<div class="flex items-center gap-2">
						<Info class="w-4 h-4" />
						<span class="text-sm font-medium">Details</span>
					</div>
					<ChevronDown class="w-4 h-4 transition-transform {showMetadata ? 'rotate-180' : ''}" />
				</button>

				<!-- Metadata content -->
				{#if showMetadata}
					<div class="metadata-content">
						<div class="grid grid-cols-2 gap-4">
							<div class="metadata-item">
								<span class="metadata-label">Type</span>
								<div class="flex items-center gap-2 metadata-value">
									{#if isImage}
										<ImageIcon class="w-4 h-4" />
										<span>Image</span>
									{:else if isVideo}
										<Film class="w-4 h-4" />
										<span>Video</span>
									{:else if isAudio}
										<Volume2 class="w-4 h-4" />
										<span>Audio</span>
									{/if}
								</div>
							</div>

							{#if generation.provider}
								<div class="metadata-item">
									<span class="metadata-label">Provider</span>
									<span class="metadata-value">{generation.provider}</span>
								</div>
							{/if}

							{#if generation.model}
								<div class="metadata-item">
									<span class="metadata-label">Model</span>
									<span class="metadata-value">{generation.model}</span>
								</div>
							{/if}

							<div class="metadata-item col-span-2">
								<span class="metadata-label">Prompt</span>
								<p class="metadata-value text-sm leading-relaxed">
									{generation.prompt}
								</p>
							</div>
						</div>
					</div>
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	/* Cosmic gradient background */
	.cosmic-gradient {
		background: radial-gradient(
			ellipse at 50% 50%,
			oklch(0.72 0.14 180 / 0.2) 0%,
			oklch(0.65 0.12 220 / 0.1) 40%,
			transparent 70%
		);
		animation: cosmic-pulse 8s ease-in-out infinite;
	}

	@keyframes cosmic-pulse {
		0%, 100% { opacity: 0.3; }
		50% { opacity: 0.5; }
	}

	/* Glass morphism panel */
	.glass-panel {
		background: var(--glass-bg, oklch(0.18 0.01 260 / 0.8));
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
	}

	/* Glow container */
	.glow-container {
		position: relative;
	}

	.glow-container::before {
		content: '';
		position: absolute;
		inset: -20px;
		background: radial-gradient(
			circle at center,
			var(--primary) 0%,
			transparent 70%
		);
		opacity: 0.3;
		filter: blur(20px);
		z-index: -1;
	}

	/* Animations */
	@keyframes pulse-soft {
		0%, 100% { opacity: 0.6; }
		50% { opacity: 1; }
	}

	.animate-pulse-soft {
		animation: pulse-soft 3s ease-in-out infinite;
	}

	@keyframes pulse-slow {
		0%, 100% { opacity: 0.2; }
		50% { opacity: 0.4; }
	}

	.animate-pulse-slow {
		animation: pulse-slow 4s ease-in-out infinite;
	}

	/* Action buttons */
	.action-btn {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.75rem 1.5rem;
		border-radius: 0.75rem;
		font-size: 0.875rem;
		font-weight: 500;
		transition: all 0.2s;
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
	}

	.action-btn-primary {
		background: var(--primary);
		color: var(--primary-foreground);
		box-shadow: 0 0 20px var(--primary);
	}

	.action-btn-primary:hover {
		transform: translateY(-2px);
		box-shadow: 0 0 30px var(--primary);
	}

	.action-btn-secondary {
		background: var(--glass-bg, oklch(0.18 0.01 260 / 0.8));
		color: var(--foreground);
		border: 1px solid var(--border);
	}

	.action-btn-secondary:hover {
		background: oklch(0.22 0.01 260 / 0.9);
		border-color: var(--primary);
	}

	/* Floating actions bar */
	.floating-actions {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		padding: 0.5rem;
		background: var(--glass-bg, oklch(0.18 0.01 260 / 0.9));
		backdrop-filter: blur(16px);
		-webkit-backdrop-filter: blur(16px);
		border: 1px solid oklch(0.72 0.14 180 / 0.2);
		border-radius: 1rem;
		box-shadow:
			0 8px 32px rgba(0, 0, 0, 0.4),
			0 0 0 1px oklch(0.72 0.14 180 / 0.1) inset;
	}

	.action-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 2.5rem;
		height: 2.5rem;
		border-radius: 0.5rem;
		color: var(--muted-foreground);
		transition: all 0.2s;
	}

	.action-icon:hover {
		background: oklch(0.72 0.14 180 / 0.15);
		color: var(--foreground);
	}

	.action-icon-danger:hover {
		background: var(--destructive);
		color: white;
	}

	/* Zoom controls */
	.zoom-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 2.5rem;
		height: 2.5rem;
		background: var(--glass-bg, oklch(0.18 0.01 260 / 0.9));
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
		border: 1px solid oklch(0.72 0.14 180 / 0.2);
		border-radius: 0.5rem;
		color: var(--foreground);
		transition: all 0.2s;
	}

	.zoom-btn:hover:not(:disabled) {
		background: oklch(0.72 0.14 180 / 0.2);
		border-color: oklch(0.72 0.14 180 / 0.4);
		box-shadow: 0 0 12px oklch(0.72 0.14 180 / 0.3);
	}

	.zoom-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	/* Video play button */
	.video-play-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 5rem;
		height: 5rem;
		background: oklch(0.18 0.01 260 / 0.9);
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
		border: 2px solid oklch(0.72 0.14 180 / 0.4);
		border-radius: 50%;
		box-shadow: 0 0 30px oklch(0.72 0.14 180 / 0.5);
	}

	/* Audio player */
	.audio-player {
		width: 100%;
		height: 48px;
		border-radius: 12px;
	}

	.audio-player::-webkit-media-controls-panel {
		background: oklch(0.22 0.01 260 / 0.8);
		border-radius: 12px;
	}

	/* Metadata panel */
	.metadata-panel {
		position: absolute;
		bottom: 0;
		left: 0;
		right: 0;
		background: var(--glass-bg, oklch(0.18 0.01 260 / 0.95));
		backdrop-filter: blur(16px);
		-webkit-backdrop-filter: blur(16px);
		border-top: 1px solid oklch(0.72 0.14 180 / 0.2);
		transition: transform 0.3s ease;
		z-index: 10;
	}

	.metadata-toggle {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 1rem 1.5rem;
		color: var(--foreground);
		transition: background 0.2s;
	}

	.metadata-toggle:hover {
		background: oklch(0.22 0.01 260 / 0.5);
	}

	.metadata-content {
		padding: 0 1.5rem 1.5rem;
		animation: slide-down 0.3s ease;
	}

	@keyframes slide-down {
		from {
			opacity: 0;
			transform: translateY(-10px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.metadata-item {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.metadata-label {
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.metadata-value {
		font-size: 0.875rem;
		color: var(--foreground);
	}

	/* Cosmic preview base */
	.cosmic-preview {
		background: var(--card, oklch(0.18 0.008 260));
		border-radius: 0.75rem;
		overflow: hidden;
		position: relative;
	}

	/* Add subtle border glow */
	.cosmic-preview::before {
		content: '';
		position: absolute;
		inset: 0;
		border-radius: 0.75rem;
		padding: 1px;
		background: linear-gradient(
			135deg,
			oklch(0.72 0.14 180 / 0.3) 0%,
			oklch(0.65 0.12 220 / 0.2) 50%,
			transparent 100%
		);
		-webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
		-webkit-mask-composite: xor;
		mask-composite: exclude;
		pointer-events: none;
		z-index: 1;
	}
</style>

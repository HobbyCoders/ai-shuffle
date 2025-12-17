<script lang="ts">
	/**
	 * MediaPreview - Preview component for generated media
	 *
	 * Displays:
	 * - Image: Full display with zoom on click
	 * - Video: Video player with controls
	 * - Loading state: Skeleton with progress bar
	 * - Error state: Error message with retry button
	 * - Actions: Download, Edit, Save to library, Delete
	 */
	import { createEventDispatcher } from 'svelte';
	import type { Generation } from '$lib/stores/studio';

	interface Props {
		generation: Generation;
	}

	let { generation }: Props = $props();

	const dispatch = createEventDispatcher<{
		save: { generation: Generation };
		edit: { generation: Generation };
		delete: { generation: Generation };
		retry: { generation: Generation };
		download: { generation: Generation };
	}>();

	// Local state
	let isZoomed = $state(false);
	let videoElement: HTMLVideoElement | null = $state(null);

	// Status helpers
	const isLoading = $derived(generation.status === 'pending' || generation.status === 'generating');
	const isComplete = $derived(generation.status === 'completed');
	const isFailed = $derived(generation.status === 'failed');

	// Progress percentage
	const progressPercent = $derived(Math.round((generation.progress || 0) * 100));

	function handleDownload() {
		if (!generation.resultUrl) return;

		const link = document.createElement('a');
		link.href = generation.resultUrl;
		link.download = `${generation.type}-${generation.id}.${generation.type === 'video' ? 'mp4' : 'png'}`;
		document.body.appendChild(link);
		link.click();
		document.body.removeChild(link);

		dispatch('download', { generation });
	}

	function handleSave() {
		dispatch('save', { generation });
	}

	function handleEdit() {
		dispatch('edit', { generation });
	}

	function handleDelete() {
		dispatch('delete', { generation });
	}

	function handleRetry() {
		dispatch('retry', { generation });
	}

	function toggleZoom() {
		if (generation.type === 'image' && isComplete) {
			isZoomed = !isZoomed;
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && isZoomed) {
			isZoomed = false;
		}
	}

	// Format date
	function formatDate(date: Date): string {
		return new Intl.DateTimeFormat('en-US', {
			month: 'short',
			day: 'numeric',
			hour: 'numeric',
			minute: '2-digit'
		}).format(date);
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="media-preview h-full flex flex-col">
	<!-- Preview Container -->
	<div class="flex-1 relative rounded-2xl overflow-hidden bg-muted/20 border border-border/30">
		{#if isLoading}
			<!-- Loading State -->
			<div class="absolute inset-0 flex flex-col items-center justify-center p-8">
				<!-- Animated background -->
				<div class="absolute inset-0 bg-gradient-to-br from-primary/5 via-transparent to-primary/5 animate-pulse"></div>

				<!-- Loading animation -->
				<div class="relative mb-6">
					<div class="w-20 h-20 rounded-full border-4 border-muted animate-spin border-t-primary"></div>
					<div class="absolute inset-0 flex items-center justify-center">
						{#if generation.type === 'video'}
							<svg class="w-8 h-8 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
							</svg>
						{:else}
							<svg class="w-8 h-8 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
							</svg>
						{/if}
					</div>
				</div>

				<!-- Status text -->
				<div class="text-center relative z-10">
					<h3 class="text-lg font-semibold text-foreground mb-1">
						{generation.status === 'pending' ? 'Starting generation...' : 'Generating...'}
					</h3>
					<p class="text-sm text-muted-foreground mb-4">
						{generation.type === 'video' ? 'This may take a few minutes' : 'Almost there...'}
					</p>

					<!-- Progress bar -->
					{#if generation.progress !== undefined && generation.progress > 0}
						<div class="w-64 mx-auto">
							<div class="h-2 bg-muted rounded-full overflow-hidden">
								<div
									class="h-full bg-primary rounded-full transition-all duration-500 ease-out"
									style="width: {progressPercent}%"
								></div>
							</div>
							<p class="text-xs text-muted-foreground mt-2">{progressPercent}%</p>
						</div>
					{/if}
				</div>

				<!-- Prompt preview -->
				<div class="absolute bottom-4 left-4 right-4">
					<p class="text-xs text-muted-foreground truncate">
						"{generation.prompt}"
					</p>
				</div>
			</div>
		{:else if isFailed}
			<!-- Error State -->
			<div class="absolute inset-0 flex flex-col items-center justify-center p-8">
				<div class="w-16 h-16 rounded-full bg-destructive/10 flex items-center justify-center mb-4">
					<svg class="w-8 h-8 text-destructive" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
					</svg>
				</div>

				<h3 class="text-lg font-semibold text-foreground mb-2">Generation Failed</h3>
				<p class="text-sm text-muted-foreground text-center mb-6 max-w-sm">
					{generation.error || 'An unexpected error occurred. Please try again.'}
				</p>

				<button
					type="button"
					class="flex items-center gap-2 px-4 py-2 rounded-xl bg-primary text-primary-foreground hover:bg-primary/90 transition-colors font-medium"
					onclick={handleRetry}
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
					</svg>
					Try Again
				</button>
			</div>
		{:else if isComplete && generation.resultUrl}
			<!-- Success State -->
			<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
			<div
				class="absolute inset-0 flex items-center justify-center {generation.type === 'image' ? 'cursor-zoom-in' : ''}"
				onclick={toggleZoom}
			>
				{#if generation.type === 'video'}
					<video
						bind:this={videoElement}
						src={generation.resultUrl}
						class="max-w-full max-h-full object-contain"
						controls
						loop
						playsinline
					>
						<track kind="captions" />
					</video>
				{:else}
					<img
						src={generation.resultUrl}
						alt={generation.prompt}
						class="max-w-full max-h-full object-contain"
					/>
				{/if}
			</div>

			<!-- Provider badge -->
			<div class="absolute top-4 left-4 px-3 py-1.5 rounded-full bg-black/50 backdrop-blur-sm text-white text-xs font-medium">
				{generation.provider}
			</div>
		{:else}
			<!-- Empty/No result state -->
			<div class="absolute inset-0 flex items-center justify-center">
				<p class="text-sm text-muted-foreground">No preview available</p>
			</div>
		{/if}
	</div>

	<!-- Info and Actions -->
	{#if isComplete}
		<div class="mt-4 space-y-3">
			<!-- Prompt -->
			<div class="p-3 rounded-xl bg-muted/20 border border-border/30">
				<p class="text-sm text-foreground line-clamp-2" title={generation.prompt}>
					{generation.prompt}
				</p>
				<p class="text-xs text-muted-foreground mt-1">
					{formatDate(generation.createdAt)}
				</p>
			</div>

			<!-- Action buttons -->
			<div class="flex items-center gap-2">
				<button
					type="button"
					class="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-primary text-primary-foreground hover:bg-primary/90 transition-colors font-medium"
					onclick={handleSave}
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7H5a2 2 0 00-2 2v9a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-3m-1 4l-3 3m0 0l-3-3m3 3V4" />
					</svg>
					Save to Library
				</button>

				<button
					type="button"
					class="p-2.5 rounded-xl bg-muted/50 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
					onclick={handleDownload}
					title="Download"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
					</svg>
				</button>

				<button
					type="button"
					class="p-2.5 rounded-xl bg-muted/50 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
					onclick={handleEdit}
					title="Edit"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
					</svg>
				</button>

				<button
					type="button"
					class="p-2.5 rounded-xl bg-muted/50 text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
					onclick={handleDelete}
					title="Delete"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
					</svg>
				</button>
			</div>
		</div>
	{/if}
</div>

<!-- Zoom Modal for Images -->
{#if isZoomed && generation.type === 'image' && generation.resultUrl}
	<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/90 cursor-zoom-out"
		onclick={toggleZoom}
	>
		<button
			type="button"
			class="absolute top-4 right-4 p-2 rounded-full bg-white/10 text-white hover:bg-white/20 transition-colors"
			onclick={toggleZoom}
			aria-label="Close zoom"
		>
			<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
			</svg>
		</button>

		<img
			src={generation.resultUrl}
			alt={generation.prompt}
			class="max-w-[90vw] max-h-[90vh] object-contain"
		/>
	</div>
{/if}

<style>
	.media-preview {
		/* Container styles */
	}

	.line-clamp-2 {
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
</style>

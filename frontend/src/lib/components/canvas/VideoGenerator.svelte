<script lang="ts">
	import { onMount } from 'svelte';
	import { canvas, isLoading } from '$lib/stores/canvas';
	import { api } from '$lib/api/client';
	import { getProviders, type VideoProvider } from '$lib/api/canvas';
	import ProviderSelector from './ProviderSelector.svelte';

	let prompt = '';
	let provider = '';
	let model: string | null = null;
	let duration = 8;
	let aspectRatio = '';
	let sourceImage: string | null = null;
	let sourceImagePreview: { url: string; file: File } | null = null;

	// Data from API
	let videoProviders: VideoProvider[] = [];
	let loadingProviders = true;

	// Derived state from current provider
	$: currentProvider = videoProviders.find((p) => p.id === provider);
	$: availableDurations = currentProvider?.durations || [];
	$: availableAspectRatios = currentProvider?.aspect_ratios || [];
	$: maxDuration = currentProvider?.max_duration || 8;
	$: supportsImageToVideo = currentProvider?.supports_image_to_video ?? false;

	// Validate duration when provider changes
	$: {
		if (availableDurations.length > 0 && !availableDurations.includes(duration)) {
			duration = availableDurations[availableDurations.length - 1];
		}
	}

	// Validate aspect ratio when provider changes
	$: {
		if (availableAspectRatios.length > 0 && !availableAspectRatios.includes(aspectRatio)) {
			aspectRatio = availableAspectRatios.includes('16:9') ? '16:9' : availableAspectRatios[0];
		}
	}

	onMount(async () => {
		try {
			const providersData = await getProviders();
			videoProviders = providersData.video_providers;

			// Set defaults
			if (videoProviders.length > 0) {
				provider = videoProviders[0].id;
				const defaultModel = videoProviders[0].models.find((m) => m.default) || videoProviders[0].models[0];
				model = defaultModel?.id || null;

				if (videoProviders[0].durations.length > 0) {
					duration = videoProviders[0].durations[videoProviders[0].durations.length - 1];
				}
				if (videoProviders[0].aspect_ratios.length > 0) {
					aspectRatio = videoProviders[0].aspect_ratios.includes('16:9') ? '16:9' : videoProviders[0].aspect_ratios[0];
				}
			}
		} catch (error) {
			console.error('Failed to fetch providers:', error);
		} finally {
			loadingProviders = false;
		}
	});

	function handleProviderChange(newProvider: string) {
		provider = newProvider;
		// Clear source image if provider doesn't support image-to-video
		const newProviderData = videoProviders.find((p) => p.id === newProvider);
		if (!newProviderData?.supports_image_to_video) {
			removeSourceImage();
		}
	}

	function handleModelChange(newModel: string | null) {
		model = newModel;
	}

	async function handleGenerate() {
		if (!prompt.trim()) return;

		canvas.startGeneration();
		canvas.updateProgress('Generating video...');

		try {
			const response = await api.post('/canvas/generate/video', {
				prompt,
				provider,
				model,
				duration,
				aspect_ratio: aspectRatio,
				source_image: sourceImage
			});

			canvas.completeGeneration(response as any);
			canvas.setView('gallery');
			canvas.setCreateType(null);

			// Reset form
			prompt = '';
			sourceImage = null;
			sourceImagePreview = null;
		} catch (error: any) {
			canvas.failGeneration(error.detail || 'Failed to generate video');
		}
	}

	function handleCancel() {
		canvas.setView('gallery');
		canvas.setCreateType(null);
	}

	async function handleSourceImageUpload(event: Event) {
		const input = event.target as HTMLInputElement;
		if (!input.files?.length) return;

		const file = input.files[0];
		if (!file.type.startsWith('image/')) return;

		// Create preview URL
		const url = URL.createObjectURL(file);

		// Clean up old preview
		if (sourceImagePreview) {
			URL.revokeObjectURL(sourceImagePreview.url);
		}

		sourceImagePreview = { url, file };

		// Upload file to Canvas uploads directory
		try {
			const response = await api.uploadFile('/canvas/upload', file);
			sourceImage = response.path;
		} catch (error) {
			console.error('Failed to upload source image:', error);
			URL.revokeObjectURL(url);
			sourceImagePreview = null;
		}

		// Clear input
		input.value = '';
	}

	function removeSourceImage() {
		if (sourceImagePreview) {
			URL.revokeObjectURL(sourceImagePreview.url);
		}
		sourceImage = null;
		sourceImagePreview = null;
	}

	// Get aspect ratio preview dimensions
	function getAspectRatioPreview(ratio: string): { width: number; height: number } {
		const [w, h] = ratio.split(':').map(Number);
		const maxSize = 40;
		if (w > h) {
			return { width: maxSize, height: Math.round((maxSize * h) / w) };
		} else if (h > w) {
			return { width: Math.round((maxSize * w) / h), height: maxSize };
		} else {
			return { width: maxSize, height: maxSize };
		}
	}
</script>

<div class="max-w-2xl mx-auto space-y-6">
	<!-- Prompt Input -->
	<div>
		<label for="video-prompt" class="block text-sm font-medium text-foreground mb-2">Describe your video</label>
		<textarea
			id="video-prompt"
			bind:value={prompt}
			placeholder="A cinematic drone shot flying over a misty forest at dawn..."
			rows="4"
			class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
		></textarea>
	</div>

	<!-- Provider and Model Selector -->
	<ProviderSelector type="video" value={provider} modelValue={model} onChange={handleProviderChange} onModelChange={handleModelChange} providers={videoProviders} loading={loadingProviders} />

	<!-- Duration Selector -->
	<div role="group" aria-labelledby="duration-label">
		<span id="duration-label" class="block text-xs text-muted-foreground mb-2">Duration</span>
		{#if loadingProviders}
			<div class="flex gap-2">
				<div class="flex-1 px-3 py-2.5 bg-muted border border-border rounded-lg">
					<div class="flex items-center justify-center gap-2">
						<div class="w-4 h-4 border-2 border-muted-foreground border-t-transparent rounded-full animate-spin"></div>
					</div>
				</div>
			</div>
		{:else}
			<div class="flex flex-wrap gap-2">
				{#each availableDurations as dur}
					<button type="button" onclick={() => (duration = dur)} class="px-4 py-2.5 text-sm font-medium rounded-lg transition-colors min-w-[60px] {duration === dur ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-hover-overlay border border-border'}">
						{dur}s
					</button>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Aspect Ratio Selector -->
	<div role="group" aria-labelledby="video-aspect-ratio-label">
		<span id="video-aspect-ratio-label" class="block text-xs text-muted-foreground mb-2">Aspect Ratio</span>
		{#if loadingProviders}
			<div class="flex items-center gap-2 py-4">
				<div class="w-4 h-4 border-2 border-muted-foreground border-t-transparent rounded-full animate-spin"></div>
				<span class="text-sm text-muted-foreground">Loading options...</span>
			</div>
		{:else}
			<div class="flex flex-wrap gap-2">
				{#each availableAspectRatios as ratio}
					{@const preview = getAspectRatioPreview(ratio)}
					<button
						type="button"
						onclick={() => (aspectRatio = ratio)}
						class="flex flex-col items-center gap-1.5 px-4 py-2.5 rounded-lg transition-colors {aspectRatio === ratio ? 'bg-primary/10 border-primary text-primary border-2' : 'bg-muted text-foreground hover:bg-hover-overlay border border-border'}"
					>
						<div class="rounded-sm {aspectRatio === ratio ? 'bg-primary' : 'bg-muted-foreground/50'}" style="width: {preview.width}px; height: {preview.height}px;"></div>
						<span class="text-xs font-medium">{ratio}</span>
					</button>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Source Image (for image-to-video) -->
	{#if supportsImageToVideo}
		<div role="group" aria-labelledby="source-image-label">
			<span id="source-image-label" class="block text-xs text-muted-foreground mb-2">Source Image (optional)</span>
			<p class="text-xs text-muted-foreground mb-3">Upload an image to animate it into a video.</p>

			<div class="flex items-start gap-4">
				{#if sourceImagePreview}
					<!-- Source image preview -->
					<div class="relative group">
						<img src={sourceImagePreview.url} alt="Source" class="w-32 h-24 object-cover rounded-lg border border-border" />
						<button
							type="button"
							onclick={removeSourceImage}
							aria-label="Remove source image"
							class="absolute -top-2 -right-2 w-6 h-6 bg-destructive text-destructive-foreground rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
						>
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
							</svg>
						</button>
					</div>
				{:else}
					<!-- Upload button -->
					<label class="w-32 h-24 rounded-lg border-2 border-dashed border-border hover:border-primary/50 flex flex-col items-center justify-center cursor-pointer transition-colors text-muted-foreground hover:text-foreground">
						<svg class="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
						</svg>
						<span class="text-xs mt-1">Add image</span>
						<input type="file" accept="image/*" onchange={handleSourceImageUpload} class="hidden" aria-label="Upload source image for video" />
					</label>
				{/if}

				<div class="flex-1 text-xs text-muted-foreground">
					<p class="mb-1"><strong class="text-foreground">Image-to-Video:</strong></p>
					<p>When you provide a source image, the AI will animate it to create your video, maintaining the visual style and content of the original image.</p>
				</div>
			</div>
		</div>
	{/if}

	<!-- Action Buttons -->
	<div class="flex flex-col-reverse sm:flex-row items-stretch sm:items-center justify-end gap-3 pt-4 border-t border-border">
		<button type="button" onclick={handleCancel} disabled={$isLoading} class="px-6 py-2.5 text-sm font-medium bg-muted text-foreground border border-border rounded-xl hover:bg-accent transition-colors disabled:opacity-50">
			Cancel
		</button>
		<button
			type="button"
			onclick={handleGenerate}
			disabled={!prompt.trim() || $isLoading || loadingProviders}
			class="px-6 py-2.5 text-sm font-medium bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 min-w-[140px]"
		>
			{#if $isLoading}
				<div class="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin"></div>
				Generating...
			{:else}
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
				</svg>
				Generate
			{/if}
		</button>
	</div>
</div>

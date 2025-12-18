<script lang="ts">
	import { onMount } from 'svelte';
	import { canvas, isLoading } from '$lib/stores/canvas';
	import { api } from '$lib/api/client';
	import { getProviders, type ImageProvider } from '$lib/api/canvas';
	import ProviderSelector from './ProviderSelector.svelte';

	let prompt = '';
	let provider = '';
	let model: string | null = null;
	let aspectRatio = '';
	let resolution = '';
	let referenceImages: string[] = [];
	let referenceImagePreviews: { url: string; file: File }[] = [];

	// Data from API
	let imageProviders: ImageProvider[] = [];
	let loadingProviders = true;

	// Derived state from current provider
	$: currentProvider = imageProviders.find((p) => p.id === provider);
	$: availableAspectRatios = currentProvider?.aspect_ratios || [];
	$: availableResolutions = currentProvider?.resolutions || [];
	$: supportsReference = currentProvider?.supports_reference ?? false;

	// Validate aspect ratio when provider changes
	$: {
		if (availableAspectRatios.length > 0 && !availableAspectRatios.includes(aspectRatio)) {
			aspectRatio = availableAspectRatios.includes('16:9') ? '16:9' : availableAspectRatios[0];
		}
	}

	// Validate resolution when provider changes
	$: {
		if (availableResolutions.length > 0 && !availableResolutions.includes(resolution)) {
			resolution = availableResolutions.includes('1K') ? '1K' : availableResolutions[0];
		}
	}

	onMount(async () => {
		try {
			const providersData = await getProviders();
			imageProviders = providersData.image_providers;

			// Set defaults
			if (imageProviders.length > 0) {
				provider = imageProviders[0].id;
				const defaultModel = imageProviders[0].models.find((m) => m.default) || imageProviders[0].models[0];
				model = defaultModel?.id || null;

				if (imageProviders[0].aspect_ratios.length > 0) {
					aspectRatio = imageProviders[0].aspect_ratios.includes('16:9') ? '16:9' : imageProviders[0].aspect_ratios[0];
				}
				if (imageProviders[0].resolutions.length > 0) {
					resolution = imageProviders[0].resolutions.includes('1K') ? '1K' : imageProviders[0].resolutions[0];
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
		// Clear reference images if provider doesn't support them
		const newProviderData = imageProviders.find((p) => p.id === newProvider);
		if (!newProviderData?.supports_reference) {
			referenceImages = [];
			referenceImagePreviews.forEach((p) => URL.revokeObjectURL(p.url));
			referenceImagePreviews = [];
		}
	}

	function handleModelChange(newModel: string | null) {
		model = newModel;
	}

	async function handleGenerate() {
		if (!prompt.trim()) return;

		canvas.startGeneration();
		canvas.updateProgress('Generating image...');

		try {
			const response = await api.post('/canvas/generate/image', {
				prompt,
				provider,
				model,
				aspect_ratio: aspectRatio,
				resolution,
				reference_images: referenceImages.length > 0 ? referenceImages : undefined
			});

			canvas.completeGeneration(response as any);
			canvas.setView('gallery');
			canvas.setCreateType(null);

			// Reset form
			prompt = '';
			referenceImages = [];
			referenceImagePreviews = [];
		} catch (error: any) {
			canvas.failGeneration(error.detail || 'Failed to generate image');
		}
	}

	function handleCancel() {
		canvas.setView('gallery');
		canvas.setCreateType(null);
	}

	async function handleFileUpload(event: Event) {
		const input = event.target as HTMLInputElement;
		if (!input.files?.length) return;

		for (const file of Array.from(input.files)) {
			if (!file.type.startsWith('image/')) continue;

			// Create preview URL
			const url = URL.createObjectURL(file);
			referenceImagePreviews = [...referenceImagePreviews, { url, file }];

			// Upload file to server
			try {
				const response = await api.uploadFile('/files/upload', file);
				referenceImages = [...referenceImages, response.path];
			} catch (error) {
				console.error('Failed to upload reference image:', error);
				// Remove preview on error
				referenceImagePreviews = referenceImagePreviews.filter((p) => p.url !== url);
				URL.revokeObjectURL(url);
			}
		}

		// Clear input
		input.value = '';
	}

	function removeReferenceImage(index: number) {
		const preview = referenceImagePreviews[index];
		if (preview) {
			URL.revokeObjectURL(preview.url);
		}
		referenceImagePreviews = referenceImagePreviews.filter((_, i) => i !== index);
		referenceImages = referenceImages.filter((_, i) => i !== index);
	}

	// Get aspect ratio preview dimensions
	function getAspectRatioPreview(ratio: string): { width: number; height: number } {
		const [w, h] = ratio.split(':').map(Number);
		const maxSize = 40;
		if (w > h) {
			return { width: maxSize, height: Math.round((maxSize * h) / w) };
		} else {
			return { width: Math.round((maxSize * w) / h), height: maxSize };
		}
	}
</script>

<div class="max-w-2xl mx-auto space-y-6">
	<!-- Prompt Input -->
	<div>
		<label class="block text-sm font-medium text-foreground mb-2">Describe your image</label>
		<textarea
			bind:value={prompt}
			placeholder="A serene mountain landscape at sunset with golden clouds..."
			rows="4"
			class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
		></textarea>
	</div>

	<!-- Provider Selector -->
	<div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
		<ProviderSelector
			type="image"
			value={provider}
			modelValue={model}
			onChange={handleProviderChange}
			onModelChange={handleModelChange}
			providers={imageProviders}
			loading={loadingProviders}
		/>

		<!-- Resolution Selector -->
		<div>
			<label class="block text-xs text-muted-foreground mb-1.5">Resolution</label>
			{#if loadingProviders}
				<div class="flex gap-2">
					<div class="flex-1 px-3 py-2.5 bg-muted border border-border rounded-lg">
						<div class="flex items-center justify-center gap-2">
							<div class="w-4 h-4 border-2 border-muted-foreground border-t-transparent rounded-full animate-spin"></div>
						</div>
					</div>
				</div>
			{:else}
				<div class="flex gap-2">
					{#each availableResolutions as res}
						<button
							type="button"
							onclick={() => (resolution = res)}
							class="flex-1 px-3 py-2.5 text-sm font-medium rounded-lg transition-colors {resolution === res ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-hover-overlay border border-border'}"
						>
							{res}
						</button>
					{/each}
				</div>
			{/if}
		</div>
	</div>

	<!-- Aspect Ratio Selector -->
	<div>
		<label class="block text-xs text-muted-foreground mb-2">Aspect Ratio</label>
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
						class="flex flex-col items-center gap-1.5 px-3 py-2 rounded-lg transition-colors {aspectRatio === ratio ? 'bg-primary/10 border-primary text-primary border-2' : 'bg-muted text-foreground hover:bg-hover-overlay border border-border'}"
					>
						<div class="rounded-sm {aspectRatio === ratio ? 'bg-primary' : 'bg-muted-foreground/50'}" style="width: {preview.width}px; height: {preview.height}px;"></div>
						<span class="text-xs font-medium">{ratio}</span>
					</button>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Reference Images (if supported) -->
	{#if supportsReference}
		<div>
			<label class="block text-xs text-muted-foreground mb-2">Reference Images (optional)</label>
			<p class="text-xs text-muted-foreground mb-3">Upload images to guide the style or composition of your generation.</p>

			<div class="flex flex-wrap gap-3">
				<!-- Existing reference image previews -->
				{#each referenceImagePreviews as preview, index}
					<div class="relative group">
						<img src={preview.url} alt="Reference {index + 1}" class="w-20 h-20 object-cover rounded-lg border border-border" />
						<button
							type="button"
							onclick={() => removeReferenceImage(index)}
							aria-label="Remove reference image"
							class="absolute -top-2 -right-2 w-6 h-6 bg-destructive text-destructive-foreground rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
						>
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
							</svg>
						</button>
					</div>
				{/each}

				<!-- Upload button -->
				<label class="w-20 h-20 rounded-lg border-2 border-dashed border-border hover:border-primary/50 flex flex-col items-center justify-center cursor-pointer transition-colors text-muted-foreground hover:text-foreground">
					<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
					</svg>
					<span class="text-xs mt-1">Add</span>
					<input type="file" accept="image/*" multiple onchange={handleFileUpload} class="hidden" />
				</label>
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
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
				</svg>
				Generate
			{/if}
		</button>
	</div>
</div>

<script lang="ts">
	import { Sparkles, Upload, X, Palette, AlertTriangle, Type, ImagePlus } from 'lucide-svelte';
	import {
		ALL_IMAGE_MODELS,
		getImageModel,
		getAspectRatiosForModel,
		getStylePresetsForProvider,
		IMAGE_ASPECT_RATIOS,
		IMAGE_STYLE_PRESETS,
		PROVIDER_DISPLAY_NAMES,
		type ImageModel,
		type ImageProvider
	} from '$lib/types/ai-models';
	import { studio, imageModel, currentImageModelInfo } from '$lib/stores/studio';

	// Props
	interface Props {
		onStartGeneration?: (type: 'image', config: ImageGenerationConfig) => void;
	}

	let { onStartGeneration }: Props = $props();

	// Types
	interface ReferenceImage {
		id: string;
		url: string;
		file?: File;
	}

	interface ImageGenerationConfig {
		prompt: string;
		provider: string;
		model: string;
		aspectRatio: string;
		style?: string;
		referenceImages: string[];
		negativePrompt?: string;
		fidelity?: 'low' | 'high';
	}

	// Group models by provider
	const modelsByProvider = $derived.by(() => {
		const grouped = new Map<ImageProvider, ImageModel[]>();
		for (const model of ALL_IMAGE_MODELS) {
			const provider = model.provider;
			if (!grouped.has(provider)) {
				grouped.set(provider, []);
			}
			grouped.get(provider)!.push(model);
		}
		return grouped;
	});

	// State
	let prompt = $state('');
	let selectedModelId = $state($imageModel);
	let selectedAspectRatio = $state('16:9');
	let selectedStyle = $state('none');
	let referenceImages: ReferenceImage[] = $state([]);
	let isGenerating = $state(false);
	let fileInputElement: HTMLInputElement | null = $state(null);
	let negativePrompt = $state('');
	let fidelity: 'low' | 'high' = $state('high');

	// Sync with store when it changes
	$effect(() => {
		selectedModelId = $imageModel;
	});

	// Derived
	let currentModel = $derived(getImageModel(selectedModelId));
	let currentProvider = $derived(currentModel?.provider || 'google-gemini');
	let canGenerate = $derived(prompt.trim().length > 0 && !isGenerating);
	let showReferenceSection = $derived(currentModel?.capabilities.referenceImages ?? false);
	let maxReferenceImages = $derived(currentModel?.capabilities.maxReferenceImages ?? 0);

	// Filter aspect ratios based on selected model
	let availableAspectRatios = $derived.by(() => {
		if (!currentModel) return IMAGE_ASPECT_RATIOS;
		return getAspectRatiosForModel(selectedModelId);
	});

	// Filter style presets based on selected provider
	let availableStylePresets = $derived.by(() => {
		if (!currentProvider) return IMAGE_STYLE_PRESETS;
		return getStylePresetsForProvider(currentProvider);
	});

	// Reset aspect ratio if current selection is no longer available
	$effect(() => {
		const ratioStillAvailable = availableAspectRatios.some((ar) => ar.value === selectedAspectRatio);
		if (!ratioStillAvailable && availableAspectRatios.length > 0) {
			selectedAspectRatio = availableAspectRatios[0].value;
		}
	});

	// Reset style if current selection is no longer available
	$effect(() => {
		const styleStillAvailable = availableStylePresets.some((s) => s.id === selectedStyle);
		if (!styleStillAvailable && availableStylePresets.length > 0) {
			selectedStyle = availableStylePresets[0].id;
		}
	});

	// Handlers
	function handleModelChange(modelId: string) {
		selectedModelId = modelId;
		studio.setImageModel(modelId);

		// Clear references if new model doesn't support them
		const model = getImageModel(modelId);
		if (!model?.capabilities.referenceImages) {
			clearAllReferences();
		}
	}

	function handleAspectRatioChange(ratio: string) {
		selectedAspectRatio = ratio;
	}

	function handleStyleChange(event: Event) {
		const select = event.target as HTMLSelectElement;
		selectedStyle = select.value;
	}

	function handleFileUpload(event: Event) {
		const input = event.target as HTMLInputElement;
		if (!input.files?.length) return;

		const maxImages = maxReferenceImages;
		const remainingSlots = maxImages - referenceImages.length;

		if (remainingSlots <= 0) return;

		const filesToAdd = Array.from(input.files).slice(0, remainingSlots);

		for (const file of filesToAdd) {
			if (!file.type.startsWith('image/')) continue;

			const id = crypto.randomUUID();
			const url = URL.createObjectURL(file);
			referenceImages = [...referenceImages, { id, url, file }];
		}

		input.value = '';
	}

	function removeReference(id: string) {
		const ref = referenceImages.find((r) => r.id === id);
		if (ref) {
			URL.revokeObjectURL(ref.url);
		}
		referenceImages = referenceImages.filter((r) => r.id !== id);
	}

	function clearAllReferences() {
		referenceImages.forEach((ref) => URL.revokeObjectURL(ref.url));
		referenceImages = [];
	}

	function triggerFileUpload() {
		fileInputElement?.click();
	}

	async function handleGenerate() {
		if (!canGenerate) return;

		isGenerating = true;

		try {
			const config: ImageGenerationConfig = {
				prompt,
				provider: currentProvider,
				model: selectedModelId,
				aspectRatio: selectedAspectRatio,
				style: selectedStyle !== 'none' ? selectedStyle : undefined,
				referenceImages: referenceImages.map((r) => r.url),
				negativePrompt: currentProvider === 'google-imagen' && negativePrompt.trim() ? negativePrompt.trim() : undefined,
				fidelity: currentProvider === 'openai-gpt-image' ? fidelity : undefined
			};

			// Call the callback which will trigger the store's generateImage
			await onStartGeneration?.('image', config);

			// Reset form after successful generation
			prompt = '';
			negativePrompt = '';
		} catch (error) {
			console.error('Generation failed:', error);
		} finally {
			isGenerating = false;
		}
	}

	function getTextRenderingLabel(quality: 'none' | 'basic' | 'good' | 'excellent'): string {
		switch (quality) {
			case 'excellent':
				return 'Excellent';
			case 'good':
				return 'Good';
			case 'basic':
				return 'Basic';
			default:
				return 'None';
		}
	}

	function getTextRenderingColor(quality: 'none' | 'basic' | 'good' | 'excellent'): string {
		switch (quality) {
			case 'excellent':
				return 'bg-green-500/20 text-green-400';
			case 'good':
				return 'bg-blue-500/20 text-blue-400';
			case 'basic':
				return 'bg-yellow-500/20 text-yellow-400';
			default:
				return 'bg-muted text-muted-foreground';
		}
	}
</script>

<div class="p-4 space-y-6">
	<!-- Prompt Input -->
	<div>
		<label for="image-prompt" class="block text-sm font-medium text-foreground mb-2"> Prompt </label>
		<textarea
			id="image-prompt"
			bind:value={prompt}
			placeholder="Describe the image you want to create..."
			rows="4"
			class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
			disabled={isGenerating}
		></textarea>
	</div>

	<!-- Model Selector (Grouped by Provider) -->
	<div>
		<label class="block text-xs text-muted-foreground mb-2">Model</label>
		<div class="space-y-4">
			{#each [...modelsByProvider.entries()] as [provider, models]}
				<div>
					<div class="text-xs font-medium text-muted-foreground mb-2 uppercase tracking-wider">
						{PROVIDER_DISPLAY_NAMES[provider] || provider}
					</div>
					<div class="space-y-2">
						{#each models as model}
							<button
								type="button"
								onclick={() => handleModelChange(model.id)}
								disabled={isGenerating}
								class="w-full flex flex-col gap-2 p-3 rounded-lg border transition-colors text-left {selectedModelId === model.id ? 'border-primary bg-primary/5' : 'border-border hover:border-muted-foreground/50 hover:bg-muted'}"
								aria-pressed={selectedModelId === model.id}
							>
								<div class="flex items-start justify-between gap-2">
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2">
											<span class="text-sm font-medium text-foreground">{model.displayName}</span>
											{#if model.deprecated}
												<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-destructive/20 text-destructive">
													<AlertTriangle class="w-3 h-3" />
													Deprecated
												</span>
											{/if}
										</div>
										<div class="text-xs text-muted-foreground mt-0.5">{model.description}</div>
									</div>
									{#if selectedModelId === model.id}
										<div class="shrink-0 w-2 h-2 rounded-full bg-primary mt-1.5"></div>
									{/if}
								</div>

								<!-- Capability Badges -->
								<div class="flex flex-wrap gap-1.5">
									{#if model.capabilities.editing}
										<span class="inline-flex items-center px-1.5 py-0.5 rounded text-xs bg-blue-500/20 text-blue-400">
											Editing
										</span>
									{/if}
									{#if model.capabilities.inpainting}
										<span class="inline-flex items-center px-1.5 py-0.5 rounded text-xs bg-purple-500/20 text-purple-400">
											Inpainting
										</span>
									{/if}
									{#if model.capabilities.referenceImages}
										<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-cyan-500/20 text-cyan-400">
											<ImagePlus class="w-3 h-3" />
											References ({model.capabilities.maxReferenceImages})
										</span>
									{/if}
									{#if model.capabilities.textRendering !== 'none'}
										<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs {getTextRenderingColor(model.capabilities.textRendering)}">
											<Type class="w-3 h-3" />
											Text: {getTextRenderingLabel(model.capabilities.textRendering)}
										</span>
									{/if}
								</div>
							</button>
						{/each}
					</div>
				</div>
			{/each}
		</div>
	</div>

	<!-- Aspect Ratio -->
	<div>
		<label class="block text-xs text-muted-foreground mb-2">Aspect Ratio</label>
		<div class="flex flex-wrap gap-2">
			{#each availableAspectRatios as ratio}
				<button
					type="button"
					onclick={() => handleAspectRatioChange(ratio.value)}
					disabled={isGenerating}
					class="flex flex-col items-center gap-1.5 px-3 py-2 rounded-lg transition-colors {selectedAspectRatio === ratio.value ? 'bg-primary/10 border-primary text-primary border-2' : 'bg-muted text-foreground hover:bg-muted/80 border border-border'}"
					aria-pressed={selectedAspectRatio === ratio.value}
					aria-label="Aspect ratio {ratio.label}"
				>
					<div
						class="rounded-sm {selectedAspectRatio === ratio.value ? 'bg-primary' : 'bg-muted-foreground/50'}"
						style="width: {ratio.width}px; height: {ratio.height}px;"
					></div>
					<span class="text-xs font-medium">{ratio.value}</span>
				</button>
			{/each}
		</div>
	</div>

	<!-- Style Preset -->
	<div>
		<label for="style-preset" class="block text-xs text-muted-foreground mb-2">
			Style Preset (optional)
		</label>
		<div class="relative">
			<Palette class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
			<select
				id="style-preset"
				value={selectedStyle}
				onchange={handleStyleChange}
				disabled={isGenerating}
				class="w-full bg-muted border border-border rounded-lg pl-10 pr-4 py-2.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 appearance-none cursor-pointer"
			>
				{#each availableStylePresets as preset}
					<option value={preset.id}>{preset.name}</option>
				{/each}
			</select>
			<div class="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none">
				<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
				</svg>
			</div>
		</div>
	</div>

	<!-- Negative Prompt (Google Imagen only) -->
	{#if currentProvider === 'google-imagen'}
		<div>
			<label for="negative-prompt" class="block text-xs text-muted-foreground mb-2">
				Negative Prompt (optional)
			</label>
			<textarea
				id="negative-prompt"
				bind:value={negativePrompt}
				placeholder="Describe what you don't want in the image..."
				rows="2"
				class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
				disabled={isGenerating}
			></textarea>
		</div>
	{/if}

	<!-- Fidelity Toggle (OpenAI GPT Image only) -->
	{#if currentProvider === 'openai-gpt-image'}
		<div>
			<label class="block text-xs text-muted-foreground mb-2">Fidelity</label>
			<div class="flex gap-2">
				<button
					type="button"
					onclick={() => (fidelity = 'low')}
					disabled={isGenerating}
					class="flex-1 px-4 py-2 rounded-lg text-sm font-medium transition-colors {fidelity === 'low' ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80 border border-border'}"
					aria-pressed={fidelity === 'low'}
				>
					Low (Faster)
				</button>
				<button
					type="button"
					onclick={() => (fidelity = 'high')}
					disabled={isGenerating}
					class="flex-1 px-4 py-2 rounded-lg text-sm font-medium transition-colors {fidelity === 'high' ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80 border border-border'}"
					aria-pressed={fidelity === 'high'}
				>
					High (Quality)
				</button>
			</div>
		</div>
	{/if}

	<!-- Reference Images -->
	{#if showReferenceSection}
		<div>
			<label class="block text-xs text-muted-foreground mb-2">
				Reference Images (optional, max {maxReferenceImages})
			</label>
			<p class="text-xs text-muted-foreground mb-3">
				Upload images to guide the style or composition.
			</p>

			<div class="flex flex-wrap gap-3">
				<!-- Existing references -->
				{#each referenceImages as ref (ref.id)}
					<div class="relative group">
						<img src={ref.url} alt="Reference" class="w-20 h-20 object-cover rounded-lg border border-border" />
						<button
							type="button"
							onclick={() => removeReference(ref.id)}
							disabled={isGenerating}
							class="absolute -top-2 -right-2 w-6 h-6 bg-destructive text-destructive-foreground rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity disabled:opacity-50"
							aria-label="Remove reference image"
						>
							<X class="w-4 h-4" />
						</button>
					</div>
				{/each}

				<!-- Upload button (only show if under max limit) -->
				{#if referenceImages.length < maxReferenceImages}
					<button
						type="button"
						onclick={triggerFileUpload}
						disabled={isGenerating}
						class="w-20 h-20 rounded-lg border-2 border-dashed border-border hover:border-primary/50 flex flex-col items-center justify-center cursor-pointer transition-colors text-muted-foreground hover:text-foreground disabled:opacity-50 disabled:cursor-not-allowed"
						aria-label="Add reference image"
					>
						<Upload class="w-5 h-5" />
						<span class="text-xs mt-1">Add</span>
					</button>
				{/if}

				<input
					bind:this={fileInputElement}
					type="file"
					accept="image/*"
					multiple
					onchange={handleFileUpload}
					class="hidden"
					aria-hidden="true"
				/>
			</div>
		</div>
	{/if}

	<!-- Generate Button -->
	<div class="pt-4 border-t border-border">
		<button
			type="button"
			onclick={handleGenerate}
			disabled={!canGenerate}
			class="w-full px-6 py-3 text-sm font-medium bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
		>
			{#if isGenerating}
				<div class="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin"></div>
				Generating...
			{:else}
				<Sparkles class="w-4 h-4" />
				Generate Image
			{/if}
		</button>
	</div>
</div>

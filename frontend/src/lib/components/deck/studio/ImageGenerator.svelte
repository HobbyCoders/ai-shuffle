<script lang="ts">
	import {
		Sparkles,
		Upload,
		X,
		Palette,
		AlertTriangle,
		Type,
		ImagePlus,
		Wand2,
		ChevronDown,
		History,
		Lightbulb,
		Square,
		RectangleHorizontal,
		RectangleVertical,
		Monitor,
		Image as ImageIcon
	} from 'lucide-svelte';
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
	let showAdvanced = $state(false);

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
	let promptLength = $derived(prompt.length);

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

	function handleStyleChange(styleId: string) {
		selectedStyle = styleId;
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
				negativePrompt:
					currentProvider === 'google-imagen' && negativePrompt.trim()
						? negativePrompt.trim()
						: undefined,
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
				return 'bg-green-500/20 text-green-400 border-green-500/30';
			case 'good':
				return 'bg-blue-500/20 text-blue-400 border-blue-500/30';
			case 'basic':
				return 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30';
			default:
				return 'bg-muted text-muted-foreground border-border';
		}
	}

	function getAspectRatioIcon(ratio: string) {
		if (ratio === '1:1') return Square;
		if (ratio === '9:16') return RectangleVertical;
		if (ratio === '16:9' || ratio === '21:9') return RectangleHorizontal;
		return Monitor;
	}

	function getProviderIcon(provider: ImageProvider): string {
		const icons: Record<ImageProvider, string> = {
			'google-gemini': '✨',
			'google-imagen': '🎨',
			'openai-dalle': '🖼️',
			'openai-gpt-image': '🤖'
		};
		return icons[provider] || '🎨';
	}
</script>

<div class="flex flex-col h-full bg-gradient-to-br from-[oklch(0.14_0.008_260)] to-[oklch(0.12_0.01_270)]">
	<!-- Image Preview Area (60%) -->
	<div class="h-[60%] relative overflow-hidden border-b border-white/5">
		<div class="absolute inset-0 flex items-center justify-center">
			<!-- Empty state / Preview placeholder -->
			<div class="text-center space-y-4 px-6">
				<div
					class="w-24 h-24 mx-auto rounded-2xl bg-gradient-to-br from-[oklch(0.72_0.14_180)]/20 to-[oklch(0.65_0.12_200)]/20 flex items-center justify-center backdrop-blur-xl border border-white/10"
				>
					<ImageIcon class="w-12 h-12 text-[oklch(0.72_0.14_180)]" strokeWidth={1.5} />
				</div>
				<div>
					<h3 class="text-lg font-semibold text-white/90 mb-1">No image generated yet</h3>
					<p class="text-sm text-white/50">
						Create your first image with the controls below
					</p>
				</div>
			</div>
		</div>

		<!-- Cosmic background decoration -->
		<div class="absolute inset-0 opacity-30 pointer-events-none">
			<div
				class="absolute top-1/4 left-1/4 w-64 h-64 bg-[oklch(0.72_0.14_180)]/10 rounded-full blur-3xl"
			></div>
			<div
				class="absolute bottom-1/4 right-1/4 w-64 h-64 bg-[oklch(0.65_0.12_200)]/10 rounded-full blur-3xl"
			></div>
		</div>
	</div>

	<!-- Generation Controls (40%) -->
	<div class="h-[40%] overflow-y-auto custom-scrollbar">
		<div class="p-6 space-y-6">
			<!-- Prompt Input -->
			<div class="space-y-3">
				<div class="flex items-center justify-between">
					<label for="image-prompt" class="text-sm font-medium text-white/70">
						Describe Your Vision
					</label>
					<span class="text-xs text-white/40">{promptLength} characters</span>
				</div>
				<div class="relative group">
					<textarea
						id="image-prompt"
						bind:value={prompt}
						placeholder="A serene mountain landscape at sunset, with golden light reflecting off a crystal lake..."
						rows="4"
						disabled={isGenerating}
						class="w-full bg-[oklch(0.18_0.01_260/0.8)] backdrop-blur-xl border border-white/10 rounded-2xl px-4 py-3 text-sm text-white/90 placeholder:text-white/30 focus:outline-none focus:border-[oklch(0.72_0.14_180)]/50 focus:ring-2 focus:ring-[oklch(0.72_0.14_180)]/20 resize-none transition-all duration-300 disabled:opacity-50"
						style="box-shadow: 0 0 0 1px rgba(255,255,255,0.05) inset;"
					></textarea>
					<!-- Animated border on focus -->
					<div
						class="absolute inset-0 rounded-2xl opacity-0 group-focus-within:opacity-100 transition-opacity duration-300 pointer-events-none"
						style="background: linear-gradient(135deg, oklch(0.72 0.14 180 / 0.3), oklch(0.65 0.12 200 / 0.3)); padding: 1px; -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0); -webkit-mask-composite: xor; mask-composite: exclude;"
					></div>
				</div>

				<!-- Quick Actions -->
				<div class="flex gap-2">
					<button
						type="button"
						disabled={isGenerating}
						class="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-xs text-white/70 hover:text-white/90 transition-all duration-200 border border-white/5 hover:border-white/10 disabled:opacity-50"
					>
						<Wand2 class="w-3.5 h-3.5" />
						AI Enhance
					</button>
					<button
						type="button"
						disabled={isGenerating}
						class="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-xs text-white/70 hover:text-white/90 transition-all duration-200 border border-white/5 hover:border-white/10 disabled:opacity-50"
					>
						<Lightbulb class="w-3.5 h-3.5" />
						Examples
					</button>
					<button
						type="button"
						disabled={isGenerating}
						class="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-xs text-white/70 hover:text-white/90 transition-all duration-200 border border-white/5 hover:border-white/10 disabled:opacity-50"
					>
						<History class="w-3.5 h-3.5" />
						History
					</button>
				</div>
			</div>

			<!-- Provider Selection as Visual Cards -->
			<div class="space-y-3">
				<div class="text-sm font-medium text-white/70">AI Provider</div>
				<div class="grid grid-cols-2 gap-3">
					{#each [...modelsByProvider.entries()] as [provider, models]}
						{@const isSelected = currentProvider === provider}
						<div
							class="group relative overflow-hidden rounded-xl border transition-all duration-300 {isSelected
								? 'bg-[oklch(0.72_0.14_180)]/10 border-[oklch(0.72_0.14_180)]/50 shadow-lg shadow-[oklch(0.72_0.14_180)]/20'
								: 'bg-[oklch(0.18_0.01_260/0.6)] border-white/10 hover:border-white/20 hover:bg-[oklch(0.18_0.01_260/0.8)]'}"
						>
							<button
								type="button"
								onclick={() => !isSelected && handleModelChange(models[0].id)}
								disabled={isGenerating}
								class="w-full p-4 text-left"
							>
								<div class="flex items-center gap-3">
									<div
										class="text-2xl transition-transform duration-300 {isSelected
											? 'scale-110'
											: 'group-hover:scale-105'}"
									>
										{getProviderIcon(provider)}
									</div>
									<div class="flex-1">
										<div
											class="text-sm font-semibold {isSelected
												? 'text-[oklch(0.72_0.14_180)]'
												: 'text-white/90'}"
										>
											{PROVIDER_DISPLAY_NAMES[provider] || provider}
										</div>
										<div class="text-xs text-white/50">{models.length} model{models.length > 1 ? 's' : ''}</div>
									</div>
								</div>
							</button>

							{#if isSelected}
								<div class="px-4 pb-4 pt-2 border-t border-white/10 space-y-1.5">
									{#each models as model}
										<button
											type="button"
											onclick={() => handleModelChange(model.id)}
											disabled={isGenerating}
											class="w-full text-left px-2 py-1.5 rounded-lg text-xs transition-colors {selectedModelId ===
											model.id
												? 'bg-[oklch(0.72_0.14_180)]/20 text-white/90'
												: 'text-white/60 hover:bg-white/5 hover:text-white/80'}"
										>
											{model.displayName}
											{#if model.deprecated}
												<span class="ml-1 text-red-400">⚠</span>
											{/if}
										</button>
									{/each}
								</div>
							{/if}

							<!-- Glow effect for selected -->
							{#if isSelected}
								<div
									class="absolute inset-0 bg-gradient-to-br from-[oklch(0.72_0.14_180)]/10 to-transparent pointer-events-none"
								></div>
							{/if}
						</div>
					{/each}
				</div>
			</div>

			<!-- Model Capabilities Display -->
			{#if currentModel}
				<div
					class="flex flex-wrap gap-2 p-3 rounded-xl bg-[oklch(0.18_0.01_260/0.4)] border border-white/5"
				>
					{#if currentModel.capabilities.editing}
						<span
							class="inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs bg-blue-500/20 text-blue-300 border border-blue-500/30"
						>
							Editing
						</span>
					{/if}
					{#if currentModel.capabilities.inpainting}
						<span
							class="inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs bg-purple-500/20 text-purple-300 border border-purple-500/30"
						>
							Inpainting
						</span>
					{/if}
					{#if currentModel.capabilities.referenceImages}
						<span
							class="inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs bg-cyan-500/20 text-cyan-300 border border-cyan-500/30"
						>
							<ImagePlus class="w-3 h-3" />
							References ({currentModel.capabilities.maxReferenceImages})
						</span>
					{/if}
					{#if currentModel.capabilities.textRendering !== 'none'}
						<span
							class="inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs border {getTextRenderingColor(
								currentModel.capabilities.textRendering
							)}"
						>
							<Type class="w-3 h-3" />
							Text: {getTextRenderingLabel(currentModel.capabilities.textRendering)}
						</span>
					{/if}
				</div>
			{/if}

			<!-- Quick Options Row -->
			<div class="grid grid-cols-2 gap-4">
				<!-- Aspect Ratio -->
				<div class="space-y-2">
					<div class="text-xs font-medium text-white/60 uppercase tracking-wider">
						Aspect Ratio
					</div>
					<div class="grid grid-cols-2 gap-2">
						{#each availableAspectRatios.slice(0, 4) as ratio}
							{@const Icon = getAspectRatioIcon(ratio.value)}
							<button
								type="button"
								onclick={() => handleAspectRatioChange(ratio.value)}
								disabled={isGenerating}
								aria-label="Aspect ratio {ratio.value}"
								class="flex flex-col items-center gap-1.5 px-2 py-2.5 rounded-lg transition-all duration-200 {selectedAspectRatio ===
								ratio.value
									? 'bg-[oklch(0.72_0.14_180)]/20 border-[oklch(0.72_0.14_180)]/50 text-[oklch(0.72_0.14_180)] shadow-lg shadow-[oklch(0.72_0.14_180)]/10'
									: 'bg-[oklch(0.18_0.01_260/0.6)] text-white/60 hover:text-white/80 border-white/10 hover:border-white/20'} border"
							>
								<Icon class="w-4 h-4" strokeWidth={1.5} />
								<span class="text-xs font-medium">{ratio.value}</span>
							</button>
						{/each}
					</div>
				</div>

				<!-- Style Preset -->
				<div class="space-y-2">
					<div class="text-xs font-medium text-white/60 uppercase tracking-wider">
						Style
					</div>
					<div class="grid grid-cols-2 gap-2">
						{#each availableStylePresets.slice(0, 4) as preset}
							<button
								type="button"
								onclick={() => handleStyleChange(preset.id)}
								disabled={isGenerating}
								aria-label="Style preset {preset.name}"
								class="flex items-center justify-center gap-1.5 px-2 py-2.5 rounded-lg transition-all duration-200 text-xs {selectedStyle ===
								preset.id
									? 'bg-[oklch(0.72_0.14_180)]/20 border-[oklch(0.72_0.14_180)]/50 text-[oklch(0.72_0.14_180)] shadow-lg shadow-[oklch(0.72_0.14_180)]/10'
									: 'bg-[oklch(0.18_0.01_260/0.6)] text-white/60 hover:text-white/80 border-white/10 hover:border-white/20'} border"
							>
								<Palette class="w-3.5 h-3.5" strokeWidth={1.5} />
								<span class="font-medium truncate">{preset.name}</span>
							</button>
						{/each}
					</div>
				</div>
			</div>

			<!-- Advanced Options (Collapsible) -->
			<div class="space-y-3">
				<button
					type="button"
					onclick={() => (showAdvanced = !showAdvanced)}
					class="flex items-center justify-between w-full px-4 py-2.5 rounded-lg bg-[oklch(0.18_0.01_260/0.6)] hover:bg-[oklch(0.18_0.01_260/0.8)] border border-white/10 hover:border-white/20 transition-all duration-200"
				>
					<span class="text-sm font-medium text-white/70">Advanced Options</span>
					<ChevronDown
						class="w-4 h-4 text-white/50 transition-transform duration-300 {showAdvanced
							? 'rotate-180'
							: ''}"
					/>
				</button>

				{#if showAdvanced}
					<div class="space-y-4 animate-in slide-in-from-top-2 duration-300">
						<!-- Reference Images -->
						{#if showReferenceSection}
							<div class="space-y-3">
								<div class="text-xs font-medium text-white/60 uppercase tracking-wider">
									Reference Images (Max {maxReferenceImages})
								</div>
								<div class="flex flex-wrap gap-3">
									<!-- Existing references -->
									{#each referenceImages as ref (ref.id)}
										<div class="relative group">
											<img
												src={ref.url}
												alt="Reference"
												class="w-20 h-20 object-cover rounded-lg border border-white/20 shadow-lg"
											/>
											<button
												type="button"
												onclick={() => removeReference(ref.id)}
												disabled={isGenerating}
												class="absolute -top-2 -right-2 w-6 h-6 bg-red-500/90 hover:bg-red-500 text-white rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-all duration-200 shadow-lg disabled:opacity-50"
											>
												<X class="w-3.5 h-3.5" />
											</button>
										</div>
									{/each}

									<!-- Upload button -->
									{#if referenceImages.length < maxReferenceImages}
										<button
											type="button"
											onclick={triggerFileUpload}
											disabled={isGenerating}
											class="w-20 h-20 rounded-lg border-2 border-dashed border-white/20 hover:border-[oklch(0.72_0.14_180)]/50 flex flex-col items-center justify-center cursor-pointer transition-all duration-200 bg-[oklch(0.18_0.01_260/0.4)] hover:bg-[oklch(0.18_0.01_260/0.6)] text-white/40 hover:text-[oklch(0.72_0.14_180)] disabled:opacity-50"
										>
											<Upload class="w-5 h-5" strokeWidth={1.5} />
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
									/>
								</div>
							</div>
						{/if}

						<!-- Negative Prompt (Google Imagen only) -->
						{#if currentProvider === 'google-imagen'}
							<div class="space-y-2">
								<label for="negative-prompt" class="text-xs font-medium text-white/60 uppercase tracking-wider">
									Negative Prompt
								</label>
								<textarea
									id="negative-prompt"
									bind:value={negativePrompt}
									placeholder="What to avoid in the image..."
									rows="2"
									disabled={isGenerating}
									class="w-full bg-[oklch(0.18_0.01_260/0.6)] border border-white/10 rounded-lg px-3 py-2 text-sm text-white/90 placeholder:text-white/30 focus:outline-none focus:border-[oklch(0.72_0.14_180)]/50 focus:ring-1 focus:ring-[oklch(0.72_0.14_180)]/20 resize-none transition-all duration-200 disabled:opacity-50"
								></textarea>
							</div>
						{/if}

						<!-- Fidelity Toggle (OpenAI GPT Image only) -->
						{#if currentProvider === 'openai-gpt-image'}
							<div class="space-y-2">
								<div class="text-xs font-medium text-white/60 uppercase tracking-wider">
									Fidelity
								</div>
								<div class="grid grid-cols-2 gap-2">
									<button
										type="button"
										onclick={() => (fidelity = 'low')}
										disabled={isGenerating}
										class="px-4 py-2.5 rounded-lg text-sm font-medium transition-all duration-200 {fidelity ===
										'low'
											? 'bg-[oklch(0.72_0.14_180)] text-white shadow-lg shadow-[oklch(0.72_0.14_180)]/30'
											: 'bg-[oklch(0.18_0.01_260/0.6)] text-white/70 hover:text-white/90 border border-white/10 hover:border-white/20'}"
									>
										Low (Faster)
									</button>
									<button
										type="button"
										onclick={() => (fidelity = 'high')}
										disabled={isGenerating}
										class="px-4 py-2.5 rounded-lg text-sm font-medium transition-all duration-200 {fidelity ===
										'high'
											? 'bg-[oklch(0.72_0.14_180)] text-white shadow-lg shadow-[oklch(0.72_0.14_180)]/30'
											: 'bg-[oklch(0.18_0.01_260/0.6)] text-white/70 hover:text-white/90 border border-white/10 hover:border-white/20'}"
									>
										High (Quality)
									</button>
								</div>
							</div>
						{/if}
					</div>
				{/if}
			</div>

			<!-- Generate Button -->
			<button
				type="button"
				onclick={handleGenerate}
				disabled={!canGenerate}
				class="group relative w-full overflow-hidden rounded-2xl transition-all duration-300 disabled:opacity-50 disabled:cursor-not-allowed {canGenerate && !isGenerating
					? 'hover:scale-[1.02] hover:shadow-2xl hover:shadow-[oklch(0.72_0.14_180)]/30'
					: ''}"
			>
				<!-- Gradient background -->
				<div
					class="absolute inset-0 bg-gradient-to-r from-[oklch(0.72_0.14_180)] to-[oklch(0.65_0.12_200)] transition-opacity duration-300 {canGenerate &&
					!isGenerating
						? 'group-hover:opacity-90'
						: ''}"
				></div>

				<!-- Animated pulse effect when ready -->
				{#if canGenerate && !isGenerating}
					<div
						class="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent -translate-x-full group-hover:translate-x-full transition-transform duration-1000"
					></div>
				{/if}

				<!-- Button content -->
				<div class="relative flex items-center justify-center gap-3 px-6 py-4">
					{#if isGenerating}
						<div class="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin"></div>
						<span class="text-base font-semibold text-white">Forging Your Vision...</span>
					{:else}
						<Sparkles class="w-5 h-5 text-white" strokeWidth={2} />
						<span class="text-base font-semibold text-white">Generate Image</span>
					{/if}
				</div>
			</button>
		</div>
	</div>
</div>

<style>
	.custom-scrollbar::-webkit-scrollbar {
		width: 8px;
	}

	.custom-scrollbar::-webkit-scrollbar-track {
		background: oklch(0.16 0.008 260 / 0.3);
		border-radius: 4px;
	}

	.custom-scrollbar::-webkit-scrollbar-thumb {
		background: oklch(0.72 0.14 180 / 0.3);
		border-radius: 4px;
	}

	.custom-scrollbar::-webkit-scrollbar-thumb:hover {
		background: oklch(0.72 0.14 180 / 0.5);
	}

	@keyframes slide-in-from-top-2 {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.animate-in {
		animation: slide-in-from-top-2 0.3s ease-out;
	}
</style>

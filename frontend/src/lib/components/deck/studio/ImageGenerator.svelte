<script lang="ts">
	import { Sparkles, Upload, X, Zap, Palette, Camera, PenTool } from 'lucide-svelte';

	// Props
	interface Props {
		onStartGeneration?: (type: 'image', config: ImageGenerationConfig) => void;
	}

	let { onStartGeneration }: Props = $props();

	// Types
	interface ImageProvider {
		id: string;
		name: string;
		description: string;
		supportsReference: boolean;
		supportsEdit: boolean;
	}

	interface AspectRatio {
		value: string;
		label: string;
		width: number;
		height: number;
	}

	interface StylePreset {
		id: string;
		name: string;
	}

	interface ReferenceImage {
		id: string;
		url: string;
		file?: File;
	}

	interface ImageGenerationConfig {
		prompt: string;
		provider: string;
		aspectRatio: string;
		style?: string;
		referenceImages: string[];
	}

	// Constants
	const providers: ImageProvider[] = [
		{
			id: 'google-gemini',
			name: 'Nano Banana',
			description: 'Fast, supports editing & references',
			supportsReference: true,
			supportsEdit: true
		},
		{
			id: 'google-imagen',
			name: 'Imagen 4',
			description: 'Highest quality, photorealistic',
			supportsReference: false,
			supportsEdit: false
		},
		{
			id: 'openai-gpt-image',
			name: 'GPT Image',
			description: 'Best text rendering, inpainting',
			supportsReference: false,
			supportsEdit: true
		}
	];

	const aspectRatios: AspectRatio[] = [
		{ value: '1:1', label: '1:1', width: 40, height: 40 },
		{ value: '16:9', label: '16:9', width: 48, height: 27 },
		{ value: '9:16', label: '9:16', width: 27, height: 48 },
		{ value: '4:3', label: '4:3', width: 44, height: 33 }
	];

	const stylePresets: StylePreset[] = [
		{ id: 'none', name: 'None' },
		{ id: 'photographic', name: 'Photographic' },
		{ id: 'digital-art', name: 'Digital Art' },
		{ id: 'anime', name: 'Anime' },
		{ id: 'watercolor', name: 'Watercolor' },
		{ id: 'oil-painting', name: 'Oil Painting' },
		{ id: 'sketch', name: 'Sketch' },
		{ id: 'cinematic', name: 'Cinematic' }
	];

	// State
	let prompt = $state('');
	let selectedProvider = $state('google-gemini');
	let selectedAspectRatio = $state('16:9');
	let selectedStyle = $state('none');
	let referenceImages: ReferenceImage[] = $state([]);
	let isGenerating = $state(false);
	let fileInputElement: HTMLInputElement | null = $state(null);

	// Derived
	let currentProvider = $derived(providers.find(p => p.id === selectedProvider)!);
	let canGenerate = $derived(prompt.trim().length > 0 && !isGenerating);
	let showReferenceSection = $derived(currentProvider.supportsReference);

	// Handlers
	function handleProviderChange(providerId: string) {
		selectedProvider = providerId;
		// Clear references if new provider doesn't support them
		const provider = providers.find(p => p.id === providerId);
		if (!provider?.supportsReference) {
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

		for (const file of Array.from(input.files)) {
			if (!file.type.startsWith('image/')) continue;

			const id = crypto.randomUUID();
			const url = URL.createObjectURL(file);
			referenceImages = [...referenceImages, { id, url, file }];
		}

		input.value = '';
	}

	function removeReference(id: string) {
		const ref = referenceImages.find(r => r.id === id);
		if (ref) {
			URL.revokeObjectURL(ref.url);
		}
		referenceImages = referenceImages.filter(r => r.id !== id);
	}

	function clearAllReferences() {
		referenceImages.forEach(ref => URL.revokeObjectURL(ref.url));
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
				provider: selectedProvider,
				aspectRatio: selectedAspectRatio,
				style: selectedStyle !== 'none' ? selectedStyle : undefined,
				referenceImages: referenceImages.map(r => r.url)
			};

			// Call the callback which will trigger the store's generateImage
			await onStartGeneration?.('image', config);

			// Reset form after successful generation
			prompt = '';
		} catch (error) {
			console.error('Generation failed:', error);
		} finally {
			isGenerating = false;
		}
	}

	function getProviderIcon(providerId: string) {
		switch (providerId) {
			case 'google-gemini':
				return Zap;
			case 'google-imagen':
				return Camera;
			case 'openai-gpt-image':
				return PenTool;
			default:
				return Sparkles;
		}
	}
</script>

<div class="p-4 space-y-6">
	<!-- Prompt Input -->
	<div>
		<label for="image-prompt" class="block text-sm font-medium text-foreground mb-2">
			Prompt
		</label>
		<textarea
			id="image-prompt"
			bind:value={prompt}
			placeholder="Describe the image you want to create..."
			rows="4"
			class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
			disabled={isGenerating}
		></textarea>
	</div>

	<!-- Provider Selector -->
	<div>
		<label class="block text-xs text-muted-foreground mb-2">Provider</label>
		<div class="space-y-2">
			{#each providers as provider}
				{@const ProviderIcon = getProviderIcon(provider.id)}
				<button
					type="button"
					onclick={() => handleProviderChange(provider.id)}
					disabled={isGenerating}
					class="w-full flex items-start gap-3 p-3 rounded-lg border transition-colors text-left {selectedProvider === provider.id ? 'border-primary bg-primary/5' : 'border-border hover:border-muted-foreground/50 hover:bg-muted'}"
					aria-pressed={selectedProvider === provider.id}
				>
					<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {selectedProvider === provider.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
						<ProviderIcon class="w-4 h-4" />
					</div>
					<div class="flex-1 min-w-0">
						<div class="text-sm font-medium text-foreground">{provider.name}</div>
						<div class="text-xs text-muted-foreground">{provider.description}</div>
					</div>
					{#if selectedProvider === provider.id}
						<div class="shrink-0 w-2 h-2 rounded-full bg-primary mt-2"></div>
					{/if}
				</button>
			{/each}
		</div>
	</div>

	<!-- Aspect Ratio -->
	<div>
		<label class="block text-xs text-muted-foreground mb-2">Aspect Ratio</label>
		<div class="flex flex-wrap gap-2">
			{#each aspectRatios as ratio}
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
					<span class="text-xs font-medium">{ratio.label}</span>
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
				{#each stylePresets as preset}
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

	<!-- Reference Images -->
	{#if showReferenceSection}
		<div>
			<label class="block text-xs text-muted-foreground mb-2">
				Reference Images (optional)
			</label>
			<p class="text-xs text-muted-foreground mb-3">
				Upload images to guide the style or composition.
			</p>

			<div class="flex flex-wrap gap-3">
				<!-- Existing references -->
				{#each referenceImages as ref (ref.id)}
					<div class="relative group">
						<img
							src={ref.url}
							alt="Reference"
							class="w-20 h-20 object-cover rounded-lg border border-border"
						/>
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

				<!-- Upload button -->
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

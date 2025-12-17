<script lang="ts">
	/**
	 * ImageGenerator - Direct image generation interface
	 *
	 * Provides:
	 * - Large prompt textarea
	 * - Provider selector with icons
	 * - Aspect ratio buttons
	 * - Style presets dropdown
	 * - Generate button with loading state
	 */
	import { createEventDispatcher } from 'svelte';
	import { studio, imageProvider, defaultAspectRatio, studioLoading } from '$lib/stores/studio';
	import type { ImageProvider, AspectRatio, GenerationOptions } from '$lib/stores/studio';

	const dispatch = createEventDispatcher<{
		generate: { prompt: string; options: GenerationOptions };
	}>();

	// Local state
	let prompt = $state('');
	let selectedProvider = $state<ImageProvider>($imageProvider);
	let selectedAspectRatio = $state<AspectRatio>($defaultAspectRatio);
	let selectedStyle = $state<string>('');
	let enhancePrompt = $state(true);
	let showAdvanced = $state(false);
	let negativePrompt = $state('');
	let seed = $state<string>('');

	// Sync with store
	$effect(() => {
		selectedProvider = $imageProvider;
	});
	$effect(() => {
		selectedAspectRatio = $defaultAspectRatio;
	});

	// Provider definitions
	const providers: Array<{
		value: ImageProvider;
		label: string;
		icon: string;
		description: string;
		color: string;
	}> = [
		{
			value: 'google-gemini',
			label: 'Nano Banana',
			icon: 'M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z',
			description: 'Fast, supports editing',
			color: 'text-amber-400'
		},
		{
			value: 'google-imagen',
			label: 'Imagen 4',
			icon: 'M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z',
			description: 'Highest quality',
			color: 'text-blue-400'
		},
		{
			value: 'openai-gpt-image',
			label: 'GPT Image',
			icon: 'M13 10V3L4 14h7v7l9-11h-7z',
			description: 'Best text rendering',
			color: 'text-green-400'
		}
	];

	// Aspect ratio options
	const aspectRatios: Array<{ value: AspectRatio; label: string; icon: string }> = [
		{ value: '1:1', label: 'Square', icon: 'w-5 h-5' },
		{ value: '16:9', label: 'Wide', icon: 'w-7 h-4' },
		{ value: '9:16', label: 'Tall', icon: 'w-4 h-7' },
		{ value: '4:3', label: 'Standard', icon: 'w-5 h-4' },
		{ value: '3:4', label: 'Portrait', icon: 'w-4 h-5' }
	];

	// Style presets
	const stylePresets = [
		{ value: '', label: 'None' },
		{ value: 'photorealistic', label: 'Photorealistic' },
		{ value: 'artistic', label: 'Artistic' },
		{ value: 'cartoon', label: 'Cartoon' },
		{ value: 'anime', label: 'Anime' },
		{ value: 'oil-painting', label: 'Oil Painting' },
		{ value: 'watercolor', label: 'Watercolor' },
		{ value: 'digital-art', label: 'Digital Art' },
		{ value: '3d-render', label: '3D Render' },
		{ value: 'pixel-art', label: 'Pixel Art' },
		{ value: 'cinematic', label: 'Cinematic' }
	];

	function handleProviderChange(provider: ImageProvider) {
		selectedProvider = provider;
		studio.setProvider('image', provider);
	}

	function handleAspectRatioChange(ratio: AspectRatio) {
		selectedAspectRatio = ratio;
		studio.setDefaultAspectRatio(ratio);
	}

	function handleGenerate() {
		if (!prompt.trim() || $studioLoading) return;

		const options: GenerationOptions = {
			aspectRatio: selectedAspectRatio,
			style: selectedStyle || undefined,
			enhancePrompt,
			negativePrompt: negativePrompt || undefined,
			seed: seed ? parseInt(seed, 10) : undefined
		};

		dispatch('generate', { prompt: prompt.trim(), options });
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
			e.preventDefault();
			handleGenerate();
		}
	}
</script>

<div class="image-generator space-y-6">
	<!-- Prompt Input -->
	<div class="space-y-2">
		<label for="prompt" class="block text-sm font-medium text-foreground">
			Prompt
		</label>
		<textarea
			id="prompt"
			bind:value={prompt}
			onkeydown={handleKeydown}
			placeholder="Describe the image you want to create..."
			rows="4"
			class="w-full px-4 py-3 bg-background border border-border/50 rounded-xl text-sm text-foreground placeholder:text-muted-foreground resize-none focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
		></textarea>
		<p class="text-xs text-muted-foreground">
			Press Cmd/Ctrl + Enter to generate
		</p>
	</div>

	<!-- Provider Selector -->
	<div class="space-y-3">
		<label class="block text-sm font-medium text-foreground">
			Provider
		</label>
		<div class="grid gap-2">
			{#each providers as provider}
				<button
					type="button"
					class="flex items-center gap-3 p-3 rounded-xl border transition-all duration-200 text-left {selectedProvider === provider.value
						? 'border-primary bg-primary/5 ring-1 ring-primary/30'
						: 'border-border/50 bg-background hover:border-border hover:bg-muted/30'}"
					onclick={() => handleProviderChange(provider.value)}
				>
					<div class="w-10 h-10 rounded-lg bg-muted/50 flex items-center justify-center flex-shrink-0 {provider.color}">
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={provider.icon} />
						</svg>
					</div>
					<div class="flex-1 min-w-0">
						<div class="text-sm font-medium text-foreground">{provider.label}</div>
						<div class="text-xs text-muted-foreground">{provider.description}</div>
					</div>
					{#if selectedProvider === provider.value}
						<svg class="w-5 h-5 text-primary flex-shrink-0" fill="currentColor" viewBox="0 0 24 24">
							<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z" />
						</svg>
					{/if}
				</button>
			{/each}
		</div>
	</div>

	<!-- Aspect Ratio -->
	<div class="space-y-3">
		<label class="block text-sm font-medium text-foreground">
			Aspect Ratio
		</label>
		<div class="flex flex-wrap gap-2">
			{#each aspectRatios as ratio}
				<button
					type="button"
					class="flex flex-col items-center gap-1.5 px-4 py-3 rounded-xl border transition-all duration-200 {selectedAspectRatio === ratio.value
						? 'border-primary bg-primary/5 ring-1 ring-primary/30'
						: 'border-border/50 bg-background hover:border-border hover:bg-muted/30'}"
					onclick={() => handleAspectRatioChange(ratio.value)}
				>
					<div class="flex items-center justify-center h-8">
						<div class="{ratio.icon} rounded border-2 {selectedAspectRatio === ratio.value ? 'border-primary' : 'border-muted-foreground/50'}"></div>
					</div>
					<span class="text-xs {selectedAspectRatio === ratio.value ? 'text-primary font-medium' : 'text-muted-foreground'}">
						{ratio.label}
					</span>
				</button>
			{/each}
		</div>
	</div>

	<!-- Style Preset -->
	<div class="space-y-2">
		<label for="style" class="block text-sm font-medium text-foreground">
			Style Preset
		</label>
		<select
			id="style"
			bind:value={selectedStyle}
			class="w-full px-4 py-2.5 bg-background border border-border/50 rounded-xl text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
		>
			{#each stylePresets as preset}
				<option value={preset.value}>{preset.label}</option>
			{/each}
		</select>
	</div>

	<!-- Enhance Prompt Toggle -->
	<div class="flex items-center justify-between py-2">
		<div>
			<div class="text-sm font-medium text-foreground">Enhance Prompt</div>
			<div class="text-xs text-muted-foreground">AI improves your prompt for better results</div>
		</div>
		<button
			type="button"
			role="switch"
			aria-checked={enhancePrompt}
			class="relative inline-flex h-6 w-11 items-center rounded-full transition-colors {enhancePrompt ? 'bg-primary' : 'bg-muted'}"
			onclick={() => enhancePrompt = !enhancePrompt}
		>
			<span
				class="inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform {enhancePrompt ? 'translate-x-6' : 'translate-x-1'}"
			></span>
		</button>
	</div>

	<!-- Advanced Settings -->
	<div class="space-y-3">
		<button
			type="button"
			class="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
			onclick={() => showAdvanced = !showAdvanced}
		>
			<svg
				class="w-4 h-4 transition-transform duration-200 {showAdvanced ? 'rotate-90' : ''}"
				fill="none"
				stroke="currentColor"
				viewBox="0 0 24 24"
			>
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
			</svg>
			Advanced Settings
		</button>

		{#if showAdvanced}
			<div class="space-y-4 pl-6 border-l-2 border-border/50 animate-in slide-in-from-top-2">
				<!-- Negative Prompt -->
				<div class="space-y-2">
					<label for="negative-prompt" class="block text-sm font-medium text-foreground">
						Negative Prompt
					</label>
					<textarea
						id="negative-prompt"
						bind:value={negativePrompt}
						placeholder="What to avoid in the image..."
						rows="2"
						class="w-full px-3 py-2 bg-background border border-border/50 rounded-lg text-sm text-foreground placeholder:text-muted-foreground resize-none focus:outline-none focus:ring-2 focus:ring-primary/50 transition-all"
					></textarea>
				</div>

				<!-- Seed -->
				<div class="space-y-2">
					<label for="seed" class="block text-sm font-medium text-foreground">
						Seed
					</label>
					<input
						id="seed"
						type="text"
						bind:value={seed}
						placeholder="Random"
						class="w-full px-3 py-2 bg-background border border-border/50 rounded-lg text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 transition-all"
					/>
					<p class="text-xs text-muted-foreground">
						Use the same seed to reproduce results
					</p>
				</div>
			</div>
		{/if}
	</div>

	<!-- Generate Button -->
	<button
		type="button"
		class="w-full flex items-center justify-center gap-3 px-6 py-4 rounded-xl font-semibold text-base transition-all duration-200 {$studioLoading || !prompt.trim()
			? 'bg-muted text-muted-foreground cursor-not-allowed'
			: 'bg-primary text-primary-foreground hover:bg-primary/90 shadow-lg shadow-primary/25 hover:shadow-primary/40 hover:scale-[1.02] active:scale-[0.98]'}"
		onclick={handleGenerate}
		disabled={$studioLoading || !prompt.trim()}
	>
		{#if $studioLoading}
			<svg class="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24">
				<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
				<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
			</svg>
			Generating...
		{:else}
			<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
			</svg>
			Generate Image
		{/if}
	</button>
</div>

<style>
	.image-generator {
		/* Smooth animations */
	}

	@keyframes slide-in-from-top {
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
		animation: slide-in-from-top 0.2s ease-out;
	}
</style>

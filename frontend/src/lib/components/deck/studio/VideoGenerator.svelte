<script lang="ts">
	/**
	 * VideoGenerator - Direct video generation interface
	 *
	 * Provides:
	 * - Prompt textarea
	 * - Provider selector (Veo, Sora)
	 * - Duration selector
	 * - Aspect ratio buttons
	 * - Audio toggle (Veo 3)
	 * - Image-to-video option
	 * - Generate button with loading state
	 */
	import { createEventDispatcher } from 'svelte';
	import { studio, videoProvider, defaultAspectRatio, studioLoading } from '$lib/stores/studio';
	import type { VideoProvider, AspectRatio, GenerationOptions } from '$lib/stores/studio';

	const dispatch = createEventDispatcher<{
		generate: { prompt: string; options: GenerationOptions };
	}>();

	// Local state
	let prompt = $state('');
	let selectedProvider = $state<VideoProvider>($videoProvider);
	let selectedAspectRatio = $state<AspectRatio>($defaultAspectRatio);
	let selectedDuration = $state(6);
	let enableAudio = $state(false);
	let showAdvanced = $state(false);
	let startFrameUrl = $state('');
	let fileInput: HTMLInputElement | null = $state(null);

	// Sync with store
	$effect(() => {
		selectedProvider = $videoProvider;
	});
	$effect(() => {
		selectedAspectRatio = $defaultAspectRatio;
	});

	// Provider definitions
	const providers: Array<{
		value: VideoProvider;
		label: string;
		icon: string;
		description: string;
		color: string;
		maxDuration: number;
		supportsAudio: boolean;
	}> = [
		{
			value: 'google-veo',
			label: 'Veo',
			icon: 'M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664zM21 12a9 9 0 11-18 0 9 9 0 0118 0z',
			description: 'Supports extend, audio with Veo 3',
			color: 'text-purple-400',
			maxDuration: 8,
			supportsAudio: true
		},
		{
			value: 'openai-sora',
			label: 'Sora',
			icon: 'M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z',
			description: 'Up to 12 seconds',
			color: 'text-red-400',
			maxDuration: 12,
			supportsAudio: false
		}
	];

	// Duration options based on provider
	const durationOptions = $derived(() => {
		const maxDuration = providers.find(p => p.value === selectedProvider)?.maxDuration || 8;
		if (maxDuration === 12) {
			return [4, 6, 8, 12];
		}
		return [4, 6, 8];
	});

	// Aspect ratio options
	const aspectRatios: Array<{ value: AspectRatio; label: string; icon: string }> = [
		{ value: '16:9', label: 'Wide', icon: 'w-7 h-4' },
		{ value: '9:16', label: 'Tall', icon: 'w-4 h-7' },
		{ value: '1:1', label: 'Square', icon: 'w-5 h-5' }
	];

	// Check if current provider supports audio
	const supportsAudio = $derived(
		providers.find(p => p.value === selectedProvider)?.supportsAudio || false
	);

	function handleProviderChange(provider: VideoProvider) {
		selectedProvider = provider;
		studio.setProvider('video', provider);
		// Reset duration if exceeds max for new provider
		const maxDuration = providers.find(p => p.value === provider)?.maxDuration || 8;
		if (selectedDuration > maxDuration) {
			selectedDuration = maxDuration;
		}
		// Disable audio if provider doesn't support it
		if (!providers.find(p => p.value === provider)?.supportsAudio) {
			enableAudio = false;
		}
	}

	function handleAspectRatioChange(ratio: AspectRatio) {
		selectedAspectRatio = ratio;
		studio.setDefaultAspectRatio(ratio);
	}

	function handleFileSelect(e: Event) {
		const target = e.target as HTMLInputElement;
		const file = target.files?.[0];
		if (file) {
			const reader = new FileReader();
			reader.onload = () => {
				startFrameUrl = reader.result as string;
			};
			reader.readAsDataURL(file);
		}
	}

	function clearStartFrame() {
		startFrameUrl = '';
		if (fileInput) {
			fileInput.value = '';
		}
	}

	function handleGenerate() {
		if (!prompt.trim() || $studioLoading) return;

		const options: GenerationOptions = {
			aspectRatio: selectedAspectRatio,
			duration: selectedDuration
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

<div class="video-generator space-y-6">
	<!-- Prompt Input -->
	<div class="space-y-2">
		<label for="prompt" class="block text-sm font-medium text-foreground">
			Prompt
		</label>
		<textarea
			id="prompt"
			bind:value={prompt}
			onkeydown={handleKeydown}
			placeholder="Describe the video you want to create..."
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

	<!-- Duration Selector -->
	<div class="space-y-3">
		<label class="block text-sm font-medium text-foreground">
			Duration
		</label>
		<div class="flex flex-wrap gap-2">
			{#each durationOptions() as duration}
				<button
					type="button"
					class="px-4 py-2.5 rounded-xl border font-medium text-sm transition-all duration-200 {selectedDuration === duration
						? 'border-primary bg-primary/5 text-primary ring-1 ring-primary/30'
						: 'border-border/50 bg-background text-muted-foreground hover:border-border hover:bg-muted/30 hover:text-foreground'}"
					onclick={() => selectedDuration = duration}
				>
					{duration}s
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

	<!-- Audio Toggle (Veo only) -->
	{#if supportsAudio}
		<div class="flex items-center justify-between py-2 px-4 bg-muted/20 rounded-xl">
			<div>
				<div class="text-sm font-medium text-foreground flex items-center gap-2">
					<svg class="w-4 h-4 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />
					</svg>
					Generate with Audio
				</div>
				<div class="text-xs text-muted-foreground">Veo 3 can generate audio for your video</div>
			</div>
			<button
				type="button"
				role="switch"
				aria-checked={enableAudio}
				class="relative inline-flex h-6 w-11 items-center rounded-full transition-colors {enableAudio ? 'bg-purple-500' : 'bg-muted'}"
				onclick={() => enableAudio = !enableAudio}
			>
				<span
					class="inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform {enableAudio ? 'translate-x-6' : 'translate-x-1'}"
				></span>
			</button>
		</div>
	{/if}

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
			<div class="space-y-4 pl-6 border-l-2 border-border/50 animate-in">
				<!-- Image-to-Video (Start Frame) -->
				<div class="space-y-3">
					<label class="block text-sm font-medium text-foreground">
						Start Frame (Optional)
					</label>
					<p class="text-xs text-muted-foreground">
						Upload an image to use as the first frame of your video
					</p>

					{#if startFrameUrl}
						<div class="relative inline-block">
							<img
								src={startFrameUrl}
								alt="Start frame"
								class="h-32 rounded-lg border border-border/50 object-cover"
							/>
							<button
								type="button"
								class="absolute -top-2 -right-2 p-1 rounded-full bg-destructive text-destructive-foreground shadow-lg hover:bg-destructive/90"
								onclick={clearStartFrame}
								aria-label="Remove start frame"
							>
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
								</svg>
							</button>
						</div>
					{:else}
						<label class="flex flex-col items-center justify-center w-full h-32 border-2 border-dashed border-border/50 rounded-xl hover:border-primary/50 hover:bg-muted/20 transition-colors cursor-pointer">
							<svg class="w-8 h-8 text-muted-foreground mb-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
							</svg>
							<span class="text-sm text-muted-foreground">Click to upload</span>
							<input
								bind:this={fileInput}
								type="file"
								accept="image/*"
								class="hidden"
								onchange={handleFileSelect}
							/>
						</label>
					{/if}
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
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
			</svg>
			Generate Video
		{/if}
	</button>

	<!-- Estimated time notice -->
	<p class="text-xs text-center text-muted-foreground">
		Video generation typically takes 1-3 minutes
	</p>
</div>

<style>
	.video-generator {
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

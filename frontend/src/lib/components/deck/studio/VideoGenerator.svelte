<script lang="ts">
	import { Clapperboard, Upload, X, Volume2, VolumeX, ImageIcon, Film } from 'lucide-svelte';

	// Props
	interface Props {
		onStartGeneration?: (type: 'video', config: VideoGenerationConfig) => void;
	}

	let { onStartGeneration }: Props = $props();

	// Types
	interface VideoProvider {
		id: string;
		name: string;
		description: string;
		maxDuration: number;
		durations: number[];
		supportsAudio: boolean;
		supportsFrameBridging: boolean;
		supportsImageToVideo: boolean;
	}

	interface AspectRatio {
		value: string;
		label: string;
		width: number;
		height: number;
	}

	interface FrameImage {
		id: string;
		url: string;
		file?: File;
		type: 'start' | 'end';
	}

	interface VideoGenerationConfig {
		prompt: string;
		provider: string;
		duration: number;
		aspectRatio: string;
		audioEnabled: boolean;
		startFrame?: string;
		endFrame?: string;
	}

	// Constants
	const providers: VideoProvider[] = [
		{
			id: 'google-veo',
			name: 'Veo',
			description: 'Supports extend, frame bridging, audio (Veo 3)',
			maxDuration: 8,
			durations: [4, 6, 8],
			supportsAudio: true,
			supportsFrameBridging: true,
			supportsImageToVideo: true
		},
		{
			id: 'openai-sora',
			name: 'Sora',
			description: 'Up to 12 seconds, good quality',
			maxDuration: 12,
			durations: [4, 6, 8, 12],
			supportsAudio: false,
			supportsFrameBridging: false,
			supportsImageToVideo: true
		}
	];

	const aspectRatios: AspectRatio[] = [
		{ value: '16:9', label: '16:9', width: 48, height: 27 },
		{ value: '9:16', label: '9:16', width: 27, height: 48 },
		{ value: '1:1', label: '1:1', width: 40, height: 40 }
	];

	// State
	let prompt = $state('');
	let selectedProvider = $state('google-veo');
	let selectedDuration = $state(8);
	let selectedAspectRatio = $state('16:9');
	let audioEnabled = $state(true);
	let startFrame: FrameImage | null = $state(null);
	let endFrame: FrameImage | null = $state(null);
	let isGenerating = $state(false);
	let startFrameInput: HTMLInputElement | null = $state(null);
	let endFrameInput: HTMLInputElement | null = $state(null);

	// Derived
	let currentProvider = $derived(providers.find(p => p.id === selectedProvider)!);
	let availableDurations = $derived(currentProvider.durations);
	let canGenerate = $derived(prompt.trim().length > 0 && !isGenerating);
	let showAudioToggle = $derived(currentProvider.supportsAudio);
	let showFrameBridging = $derived(currentProvider.supportsFrameBridging);
	let showImageToVideo = $derived(currentProvider.supportsImageToVideo);

	// Effects to validate duration when provider changes
	$effect(() => {
		if (!availableDurations.includes(selectedDuration)) {
			selectedDuration = availableDurations[availableDurations.length - 1];
		}
	});

	// Handlers
	function handleProviderChange(providerId: string) {
		selectedProvider = providerId;
		// Clear frames if new provider doesn't support them
		const provider = providers.find(p => p.id === providerId);
		if (!provider?.supportsImageToVideo) {
			clearFrame('start');
			clearFrame('end');
		}
		if (!provider?.supportsFrameBridging) {
			clearFrame('end');
		}
	}

	function handleDurationChange(duration: number) {
		selectedDuration = duration;
	}

	function handleAspectRatioChange(ratio: string) {
		selectedAspectRatio = ratio;
	}

	function toggleAudio() {
		audioEnabled = !audioEnabled;
	}

	function handleFrameUpload(event: Event, type: 'start' | 'end') {
		const input = event.target as HTMLInputElement;
		if (!input.files?.length) return;

		const file = input.files[0];
		if (!file.type.startsWith('image/')) return;

		const id = crypto.randomUUID();
		const url = URL.createObjectURL(file);
		const frame: FrameImage = { id, url, file, type };

		if (type === 'start') {
			if (startFrame) URL.revokeObjectURL(startFrame.url);
			startFrame = frame;
		} else {
			if (endFrame) URL.revokeObjectURL(endFrame.url);
			endFrame = frame;
		}

		input.value = '';
	}

	function clearFrame(type: 'start' | 'end') {
		if (type === 'start' && startFrame) {
			URL.revokeObjectURL(startFrame.url);
			startFrame = null;
		} else if (type === 'end' && endFrame) {
			URL.revokeObjectURL(endFrame.url);
			endFrame = null;
		}
	}

	function triggerFrameUpload(type: 'start' | 'end') {
		if (type === 'start') {
			startFrameInput?.click();
		} else {
			endFrameInput?.click();
		}
	}

	async function handleGenerate() {
		if (!canGenerate) return;

		isGenerating = true;

		try {
			const config: VideoGenerationConfig = {
				prompt,
				provider: selectedProvider,
				duration: selectedDuration,
				aspectRatio: selectedAspectRatio,
				audioEnabled: showAudioToggle ? audioEnabled : false,
				startFrame: startFrame?.url,
				endFrame: endFrame?.url
			};

			// Call the callback which will trigger the store's generateVideo
			await onStartGeneration?.('video', config);

			// Reset form after successful generation
			prompt = '';
		} catch (error) {
			console.error('Generation failed:', error);
		} finally {
			isGenerating = false;
		}
	}
</script>

<div class="p-4 space-y-6">
	<!-- Prompt Input -->
	<div>
		<label for="video-prompt" class="block text-sm font-medium text-foreground mb-2">
			Prompt
		</label>
		<textarea
			id="video-prompt"
			bind:value={prompt}
			placeholder="Describe the video you want to create..."
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
				<button
					type="button"
					onclick={() => handleProviderChange(provider.id)}
					disabled={isGenerating}
					class="w-full flex items-start gap-3 p-3 rounded-lg border transition-colors text-left {selectedProvider === provider.id ? 'border-primary bg-primary/5' : 'border-border hover:border-muted-foreground/50 hover:bg-muted'}"
					aria-pressed={selectedProvider === provider.id}
				>
					<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {selectedProvider === provider.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
						<Film class="w-4 h-4" />
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

	<!-- Duration Selector -->
	<div>
		<label class="block text-xs text-muted-foreground mb-2">Duration</label>
		<div class="flex flex-wrap gap-2">
			{#each availableDurations as duration}
				<button
					type="button"
					onclick={() => handleDurationChange(duration)}
					disabled={isGenerating}
					class="px-4 py-2.5 text-sm font-medium rounded-lg transition-colors min-w-[60px] {selectedDuration === duration ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80 border border-border'}"
					aria-pressed={selectedDuration === duration}
				>
					{duration}s
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

	<!-- Audio Toggle (Veo 3 only) -->
	{#if showAudioToggle}
		<div>
			<label class="block text-xs text-muted-foreground mb-2">Audio (Veo 3)</label>
			<button
				type="button"
				onclick={toggleAudio}
				disabled={isGenerating}
				class="flex items-center gap-3 px-4 py-3 rounded-lg border transition-colors w-full {audioEnabled ? 'border-primary bg-primary/5' : 'border-border hover:bg-muted'}"
				aria-pressed={audioEnabled}
			>
				<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {audioEnabled ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
					{#if audioEnabled}
						<Volume2 class="w-4 h-4" />
					{:else}
						<VolumeX class="w-4 h-4" />
					{/if}
				</div>
				<div class="flex-1 text-left">
					<div class="text-sm font-medium text-foreground">
						{audioEnabled ? 'Audio Enabled' : 'Audio Disabled'}
					</div>
					<div class="text-xs text-muted-foreground">
						Generate video with synchronized audio
					</div>
				</div>
				<div class="shrink-0 w-10 h-6 rounded-full transition-colors {audioEnabled ? 'bg-primary' : 'bg-muted-foreground/30'}">
					<div class="w-5 h-5 mt-0.5 rounded-full bg-white shadow-sm transition-transform {audioEnabled ? 'translate-x-4' : 'translate-x-0.5'}"></div>
				</div>
			</button>
		</div>
	{/if}

	<!-- Image-to-Video Section -->
	{#if showImageToVideo}
		<div>
			<label class="block text-xs text-muted-foreground mb-2">
				Image-to-Video (optional)
			</label>
			<p class="text-xs text-muted-foreground mb-3">
				Upload frames to animate. {showFrameBridging ? 'Use end frame for frame bridging.' : ''}
			</p>

			<div class="flex gap-4">
				<!-- Start Frame -->
				<div class="flex-1">
					<div class="text-xs font-medium text-foreground mb-2">Start Frame</div>
					{#if startFrame}
						<div class="relative group">
							<img
								src={startFrame.url}
								alt="Start frame"
								class="w-full aspect-video object-cover rounded-lg border border-border"
							/>
							<button
								type="button"
								onclick={() => clearFrame('start')}
								disabled={isGenerating}
								class="absolute -top-2 -right-2 w-6 h-6 bg-destructive text-destructive-foreground rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity disabled:opacity-50"
								aria-label="Remove start frame"
							>
								<X class="w-4 h-4" />
							</button>
						</div>
					{:else}
						<button
							type="button"
							onclick={() => triggerFrameUpload('start')}
							disabled={isGenerating}
							class="w-full aspect-video rounded-lg border-2 border-dashed border-border hover:border-primary/50 flex flex-col items-center justify-center cursor-pointer transition-colors text-muted-foreground hover:text-foreground disabled:opacity-50 disabled:cursor-not-allowed"
							aria-label="Upload start frame"
						>
							<ImageIcon class="w-6 h-6" />
							<span class="text-xs mt-1">Upload</span>
						</button>
					{/if}
					<input
						bind:this={startFrameInput}
						type="file"
						accept="image/*"
						onchange={(e) => handleFrameUpload(e, 'start')}
						class="hidden"
						aria-hidden="true"
					/>
				</div>

				<!-- End Frame (Frame Bridging) -->
				{#if showFrameBridging}
					<div class="flex-1">
						<div class="text-xs font-medium text-foreground mb-2">End Frame</div>
						{#if endFrame}
							<div class="relative group">
								<img
									src={endFrame.url}
									alt="End frame"
									class="w-full aspect-video object-cover rounded-lg border border-border"
								/>
								<button
									type="button"
									onclick={() => clearFrame('end')}
									disabled={isGenerating}
									class="absolute -top-2 -right-2 w-6 h-6 bg-destructive text-destructive-foreground rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity disabled:opacity-50"
									aria-label="Remove end frame"
								>
									<X class="w-4 h-4" />
								</button>
							</div>
						{:else}
							<button
								type="button"
								onclick={() => triggerFrameUpload('end')}
								disabled={isGenerating}
								class="w-full aspect-video rounded-lg border-2 border-dashed border-border hover:border-primary/50 flex flex-col items-center justify-center cursor-pointer transition-colors text-muted-foreground hover:text-foreground disabled:opacity-50 disabled:cursor-not-allowed"
								aria-label="Upload end frame"
							>
								<ImageIcon class="w-6 h-6" />
								<span class="text-xs mt-1">Upload</span>
							</button>
						{/if}
						<input
							bind:this={endFrameInput}
							type="file"
							accept="image/*"
							onchange={(e) => handleFrameUpload(e, 'end')}
							class="hidden"
							aria-hidden="true"
						/>
					</div>
				{/if}
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
				<Clapperboard class="w-4 h-4" />
				Generate Video
			{/if}
		</button>
	</div>
</div>

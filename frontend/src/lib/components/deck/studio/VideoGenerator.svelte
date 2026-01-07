<script lang="ts">
	import { Clapperboard, Upload, X, Volume2, VolumeX, ImageIcon, Film, FastForward } from 'lucide-svelte';
	import { ALL_VIDEO_MODELS, getVideoModel, VIDEO_ASPECT_RATIOS, PROVIDER_DISPLAY_NAMES, type VideoModel } from '$lib/types/ai-models';
	import { studio, videoModel, currentVideoModelInfo } from '$lib/stores/studio';

	// Props
	interface Props {
		onStartGeneration?: (type: 'video', config: VideoGenerationConfig) => void;
	}

	let { onStartGeneration }: Props = $props();

	// Types
	interface FrameImage {
		id: string;
		url: string;
		file?: File;
		type: 'start' | 'end';
	}

	interface VideoGenerationConfig {
		prompt: string;
		provider: string;
		model: string;
		duration: number;
		aspectRatio: string;
		resolution: string;
		audioEnabled: boolean;
		startFrame?: string;
		endFrame?: string;
	}

	// Group models by provider
	interface ProviderGroup {
		provider: string;
		displayName: string;
		models: VideoModel[];
	}

	function getModelsGroupedByProvider(): ProviderGroup[] {
		const groups: Map<string, VideoModel[]> = new Map();

		for (const model of ALL_VIDEO_MODELS) {
			if (model.deprecated) continue;
			const existing = groups.get(model.provider) || [];
			existing.push(model);
			groups.set(model.provider, existing);
		}

		return Array.from(groups.entries()).map(([provider, models]) => ({
			provider,
			displayName: PROVIDER_DISPLAY_NAMES[provider] || provider,
			models
		}));
	}

	const providerGroups = getModelsGroupedByProvider();

	// State
	let prompt = $state('');
	let selectedModelId = $state($videoModel);
	let selectedDuration = $state(8);
	let selectedAspectRatio = $state('16:9');
	let selectedResolution = $state('1080p');
	let audioEnabled = $state(true);
	let startFrame: FrameImage | null = $state(null);
	let endFrame: FrameImage | null = $state(null);
	let isGenerating = $state(false);
	let startFrameInput: HTMLInputElement | null = $state(null);
	let endFrameInput: HTMLInputElement | null = $state(null);

	// Derived - Current model info
	let currentModel = $derived(getVideoModel(selectedModelId) || ALL_VIDEO_MODELS[0]);
	let availableDurations = $derived(currentModel.durations);
	let availableResolutions = $derived(currentModel.resolutions);
	let canGenerate = $derived(prompt.trim().length > 0 && !isGenerating);

	// Capability checks
	let showAudioToggle = $derived(currentModel.capabilities.nativeAudio);
	let showFrameBridging = $derived(currentModel.capabilities.frameBridging);
	let showImageToVideo = $derived(currentModel.capabilities.imageToVideo);
	let showExtend = $derived(currentModel.capabilities.extension);

	// Capability badges
	let capabilityBadges = $derived.by(() => {
		const badges: { label: string; color: string }[] = [];
		const caps = currentModel.capabilities;

		if (caps.imageToVideo) badges.push({ label: 'Image to Video', color: 'bg-blue-500/20 text-blue-400' });
		if (caps.frameBridging) badges.push({ label: 'Frame Bridging', color: 'bg-purple-500/20 text-purple-400' });
		if (caps.extension) badges.push({ label: 'Extend', color: 'bg-green-500/20 text-green-400' });
		if (caps.nativeAudio) badges.push({ label: 'Audio', color: 'bg-orange-500/20 text-orange-400' });
		if (caps.remix) badges.push({ label: 'Remix', color: 'bg-pink-500/20 text-pink-400' });

		return badges;
	});

	// Get badges for a specific model
	function getModelBadges(model: VideoModel): { label: string; color: string }[] {
		const badges: { label: string; color: string }[] = [];
		const caps = model.capabilities;

		if (caps.imageToVideo) badges.push({ label: 'Image to Video', color: 'bg-blue-500/20 text-blue-400' });
		if (caps.frameBridging) badges.push({ label: 'Frame Bridging', color: 'bg-purple-500/20 text-purple-400' });
		if (caps.extension) badges.push({ label: 'Extend', color: 'bg-green-500/20 text-green-400' });
		if (caps.nativeAudio) badges.push({ label: 'Audio', color: 'bg-orange-500/20 text-orange-400' });
		if (caps.remix) badges.push({ label: 'Remix', color: 'bg-pink-500/20 text-pink-400' });

		return badges;
	}

	// Effects to validate duration when model changes
	$effect(() => {
		if (!availableDurations.includes(selectedDuration)) {
			selectedDuration = availableDurations[availableDurations.length - 1];
		}
	});

	// Effects to validate resolution when model changes
	$effect(() => {
		if (!availableResolutions.includes(selectedResolution)) {
			selectedResolution = availableResolutions[0];
		}
	});

	// Handlers
	function handleModelChange(modelId: string) {
		selectedModelId = modelId;
		studio.setVideoModel(modelId);

		const model = getVideoModel(modelId);
		if (!model) return;

		// Clear frames if new model doesn't support them
		if (!model.capabilities.imageToVideo) {
			clearFrame('start');
			clearFrame('end');
		}
		if (!model.capabilities.frameBridging) {
			clearFrame('end');
		}
	}

	function handleDurationChange(duration: number) {
		selectedDuration = duration;
	}

	function handleAspectRatioChange(ratio: string) {
		selectedAspectRatio = ratio;
	}

	function handleResolutionChange(resolution: string) {
		selectedResolution = resolution;
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
				provider: currentModel.provider,
				model: selectedModelId,
				duration: selectedDuration,
				aspectRatio: selectedAspectRatio,
				resolution: selectedResolution,
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

	// Get aspect ratio display info
	function getAspectRatioInfo(value: string) {
		const ratio = VIDEO_ASPECT_RATIOS.find(r => r.value === value);
		return ratio || { value, label: value, width: 40, height: 40 };
	}

	// Filter aspect ratios for current model
	let availableAspectRatios = $derived(
		VIDEO_ASPECT_RATIOS.filter(ar => currentModel.aspectRatios.includes(ar.value))
	);
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

	<!-- Model Selector - Grouped by Provider -->
	<div>
		<label id="video-model-label" class="block text-xs text-muted-foreground mb-2">Model</label>
		<div class="space-y-4" role="group" aria-labelledby="video-model-label">
			{#each providerGroups as group}
				<div>
					<div class="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-2">
						{group.displayName}
					</div>
					<div class="space-y-2">
						{#each group.models as model}
							{@const badges = getModelBadges(model)}
							<button
								type="button"
								onclick={() => handleModelChange(model.id)}
								disabled={isGenerating}
								class="w-full flex flex-col gap-2 p-3 rounded-lg border transition-colors text-left {selectedModelId === model.id ? 'border-primary bg-primary/5' : 'border-border hover:border-muted-foreground/50 hover:bg-muted'}"
								aria-pressed={selectedModelId === model.id}
							>
								<div class="flex items-start gap-3">
									<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {selectedModelId === model.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
										<Film class="w-4 h-4" />
									</div>
									<div class="flex-1 min-w-0">
										<div class="text-sm font-medium text-foreground">{model.displayName}</div>
										<div class="text-xs text-muted-foreground">{model.description}</div>
									</div>
									{#if selectedModelId === model.id}
										<div class="shrink-0 w-2 h-2 rounded-full bg-primary mt-2"></div>
									{/if}
								</div>
								<!-- Capability Badges -->
								{#if badges.length > 0}
									<div class="flex flex-wrap gap-1 ml-11">
										{#each badges as badge}
											<span class="text-[10px] px-1.5 py-0.5 rounded {badge.color}">
												{badge.label}
											</span>
										{/each}
									</div>
								{/if}
							</button>
						{/each}
					</div>
				</div>
			{/each}
		</div>
	</div>

	<!-- Duration Selector -->
	<div>
		<label id="video-duration-label" class="block text-xs text-muted-foreground mb-2">Duration</label>
		<div class="flex flex-wrap gap-2" role="group" aria-labelledby="video-duration-label">
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

	<!-- Resolution Selector -->
	<div>
		<label id="video-resolution-label" class="block text-xs text-muted-foreground mb-2">Resolution</label>
		<div class="flex flex-wrap gap-2" role="group" aria-labelledby="video-resolution-label">
			{#each availableResolutions as resolution}
				<button
					type="button"
					onclick={() => handleResolutionChange(resolution)}
					disabled={isGenerating}
					class="px-4 py-2.5 text-sm font-medium rounded-lg transition-colors min-w-[70px] {selectedResolution === resolution ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80 border border-border'}"
					aria-pressed={selectedResolution === resolution}
				>
					{resolution}
				</button>
			{/each}
		</div>
	</div>

	<!-- Aspect Ratio -->
	<div>
		<label id="video-aspect-ratio-label" class="block text-xs text-muted-foreground mb-2">Aspect Ratio</label>
		<div class="flex flex-wrap gap-2" role="group" aria-labelledby="video-aspect-ratio-label">
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
					<span class="text-xs font-medium">{ratio.label}</span>
				</button>
			{/each}
		</div>
	</div>

	<!-- Audio Toggle (only if model supports nativeAudio) -->
	{#if showAudioToggle}
		<div>
			<label id="video-audio-label" class="block text-xs text-muted-foreground mb-2">Audio ({currentModel.displayName})</label>
			<button
				type="button"
				onclick={toggleAudio}
				disabled={isGenerating}
				class="flex items-center gap-3 px-4 py-3 rounded-lg border transition-colors w-full {audioEnabled ? 'border-primary bg-primary/5' : 'border-border hover:bg-muted'}"
				aria-pressed={audioEnabled}
				aria-labelledby="video-audio-label"
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

	<!-- Image-to-Video Section (only if model supports imageToVideo) -->
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

				<!-- End Frame (Frame Bridging - only if model supports frameBridging) -->
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

	<!-- Extend Button (only if model supports extension) -->
	{#if showExtend}
		<div class="p-3 rounded-lg border border-border bg-muted/50">
			<div class="flex items-center gap-3">
				<div class="shrink-0 w-8 h-8 rounded-lg bg-green-500/20 text-green-400 flex items-center justify-center">
					<FastForward class="w-4 h-4" />
				</div>
				<div class="flex-1">
					<div class="text-sm font-medium text-foreground">Video Extension</div>
					<div class="text-xs text-muted-foreground">
						This model supports extending existing videos up to {currentModel.maxDuration}s total
					</div>
				</div>
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

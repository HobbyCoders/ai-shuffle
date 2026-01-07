<script lang="ts">
	import {
		Clapperboard, Upload, X, Volume2, VolumeX, ImageIcon, Film,
		Play, Pause, SkipForward, SkipBack, Download, Repeat,
		Plus, ChevronRight, Sparkles, Zap, Settings2
	} from 'lucide-svelte';
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

	// Mode type
	type GenerationMode = 'text-to-video' | 'image-to-video' | 'frame-bridge';

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
	let currentMode: GenerationMode = $state('text-to-video');
	let showAdvanced = $state(false);

	// Video player state (placeholder)
	let isPlaying = $state(false);
	let videoProgress = $state(0);

	// Derived - Current model info
	let currentModel = $derived(getVideoModel(selectedModelId) || ALL_VIDEO_MODELS[0]);
	let availableDurations = $derived(currentModel.durations);
	let availableResolutions = $derived(currentModel.resolutions);
	let canGenerate = $derived(
		prompt.trim().length > 0 &&
		!isGenerating &&
		(currentMode === 'text-to-video' ||
		 (currentMode === 'image-to-video' && startFrame !== null) ||
		 (currentMode === 'frame-bridge' && startFrame !== null && endFrame !== null))
	);

	// Capability checks
	let showAudioToggle = $derived(currentModel.capabilities.nativeAudio);
	let showFrameBridging = $derived(currentModel.capabilities.frameBridging);
	let showImageToVideo = $derived(currentModel.capabilities.imageToVideo);
	let showExtend = $derived(currentModel.capabilities.extension);

	// Available modes based on current model
	let availableModes = $derived.by(() => {
		const modes: GenerationMode[] = ['text-to-video'];
		if (currentModel.capabilities.imageToVideo) modes.push('image-to-video');
		if (currentModel.capabilities.frameBridging) modes.push('frame-bridge');
		return modes;
	});

	// Get unique providers
	let uniqueProviders = $derived.by(() => {
		const providers = new Set<string>();
		ALL_VIDEO_MODELS.forEach(model => {
			if (!model.deprecated) providers.add(model.provider);
		});
		return Array.from(providers);
	});

	// Filter models by selected provider
	let selectedProvider = $state<string | null>(null);
	let filteredModels = $derived.by(() => {
		if (!selectedProvider) return ALL_VIDEO_MODELS.filter(m => !m.deprecated);
		return ALL_VIDEO_MODELS.filter(m => m.provider === selectedProvider && !m.deprecated);
	});

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

	// Effect to validate mode when model changes
	$effect(() => {
		if (!availableModes.includes(currentMode)) {
			currentMode = 'text-to-video';
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

	function handleModeChange(mode: GenerationMode) {
		currentMode = mode;
		// Clear frames when switching modes
		if (mode === 'text-to-video') {
			clearFrame('start');
			clearFrame('end');
		} else if (mode === 'image-to-video') {
			clearFrame('end');
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

	// Mock video player controls (for future implementation)
	function togglePlay() {
		isPlaying = !isPlaying;
	}
</script>

<div class="h-full flex flex-col bg-[var(--background)] overflow-hidden">
	<!-- Video Preview Area (55%) -->
	<div class="flex-[55] min-h-0 p-6 pb-3">
		<div class="h-full relative rounded-2xl overflow-hidden border border-[var(--border)] bg-gradient-to-br from-[var(--card)] to-[var(--background)] shadow-xl">
			<!-- Video Preview Placeholder -->
			<div class="absolute inset-0 flex items-center justify-center">
				<div class="text-center space-y-4">
					<div class="w-20 h-20 mx-auto rounded-full bg-gradient-to-br from-[var(--primary)]/20 to-[var(--primary)]/5 flex items-center justify-center border border-[var(--primary)]/20">
						<Film class="w-10 h-10 text-[var(--primary)]" />
					</div>
					<div class="space-y-1">
						<p class="text-sm font-medium text-[var(--foreground)]">No video loaded</p>
						<p class="text-xs text-[var(--muted-foreground)]">Generate a video to preview it here</p>
					</div>
				</div>
			</div>

			<!-- Video Controls Overlay (bottom) -->
			<div class="absolute bottom-0 left-0 right-0 p-4 bg-gradient-to-t from-black/80 via-black/50 to-transparent">
				<div class="space-y-2">
					<!-- Progress Bar -->
					<div class="h-1 bg-white/20 rounded-full overflow-hidden">
						<div class="h-full bg-[var(--primary)] transition-all duration-300" style="width: {videoProgress}%"></div>
					</div>

					<!-- Controls -->
					<div class="flex items-center justify-between">
						<div class="flex items-center gap-2">
							<button
								type="button"
								class="w-8 h-8 rounded-lg bg-white/10 hover:bg-white/20 backdrop-blur-sm flex items-center justify-center transition-colors text-white"
								disabled
							>
								<SkipBack class="w-4 h-4" />
							</button>
							<button
								type="button"
								onclick={togglePlay}
								class="w-10 h-10 rounded-lg bg-[var(--primary)] hover:bg-[var(--primary)]/90 flex items-center justify-center transition-colors text-white shadow-lg"
								disabled
							>
								{#if isPlaying}
									<Pause class="w-5 h-5" />
								{:else}
									<Play class="w-5 h-5" />
								{/if}
							</button>
							<button
								type="button"
								class="w-8 h-8 rounded-lg bg-white/10 hover:bg-white/20 backdrop-blur-sm flex items-center justify-center transition-colors text-white"
								disabled
							>
								<SkipForward class="w-4 h-4" />
							</button>
						</div>

						<div class="flex items-center gap-2">
							{#if showExtend}
								<button
									type="button"
									class="px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/20 backdrop-blur-sm flex items-center gap-1.5 transition-colors text-white text-xs font-medium"
									disabled
								>
									<Plus class="w-3 h-3" />
									Extend
								</button>
							{/if}
							<button
								type="button"
								class="px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/20 backdrop-blur-sm flex items-center gap-1.5 transition-colors text-white text-xs font-medium"
								disabled
							>
								<Repeat class="w-3 h-3" />
								Loop
							</button>
							<button
								type="button"
								class="px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/20 backdrop-blur-sm flex items-center gap-1.5 transition-colors text-white text-xs font-medium"
								disabled
							>
								<Download class="w-3 h-3" />
								Download
							</button>
						</div>
					</div>
				</div>
			</div>
		</div>
	</div>

	<!-- Mini Timeline (10%) -->
	<div class="flex-[10] min-h-0 px-6 py-2">
		<div class="h-full">
			<div class="flex items-center gap-2 h-full">
				<!-- Placeholder thumbnails -->
				<div class="flex items-center gap-2 flex-1 overflow-x-auto scrollbar-thin scrollbar-thumb-[var(--border)] scrollbar-track-transparent">
					<div class="text-xs text-[var(--muted-foreground)] whitespace-nowrap">Timeline:</div>
					<button
						type="button"
						class="shrink-0 w-24 h-14 rounded-lg border-2 border-dashed border-[var(--border)] hover:border-[var(--primary)]/50 flex items-center justify-center gap-1 transition-colors group"
						disabled
					>
						<Plus class="w-4 h-4 text-[var(--muted-foreground)] group-hover:text-[var(--primary)]" />
						<span class="text-xs text-[var(--muted-foreground)] group-hover:text-[var(--primary)]">Add clip</span>
					</button>
				</div>
			</div>
		</div>
	</div>

	<!-- Generation Controls (35%) -->
	<div class="flex-[35] min-h-0 px-6 pb-6 pt-3 overflow-y-auto scrollbar-thin scrollbar-thumb-[var(--border)] scrollbar-track-transparent">
		<div class="space-y-4">
			<!-- Mode Switcher -->
			<div class="space-y-2">
				<label class="text-xs font-medium text-[var(--muted-foreground)] uppercase tracking-wider">Generation Mode</label>
				<div class="grid grid-cols-3 gap-2 p-1 bg-[var(--card)] rounded-xl border border-[var(--border)]">
					<button
						type="button"
						onclick={() => handleModeChange('text-to-video')}
						disabled={!availableModes.includes('text-to-video') || isGenerating}
						class="px-3 py-2.5 text-xs font-medium rounded-lg transition-all {currentMode === 'text-to-video' ? 'bg-[var(--primary)] text-white shadow-lg' : 'text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--muted)]'} disabled:opacity-50 disabled:cursor-not-allowed"
					>
						<div class="flex flex-col items-center gap-1">
							<Sparkles class="w-4 h-4" />
							<span>Text→Video</span>
						</div>
					</button>
					<button
						type="button"
						onclick={() => handleModeChange('image-to-video')}
						disabled={!availableModes.includes('image-to-video') || isGenerating}
						class="px-3 py-2.5 text-xs font-medium rounded-lg transition-all {currentMode === 'image-to-video' ? 'bg-[var(--primary)] text-white shadow-lg' : 'text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--muted)]'} disabled:opacity-50 disabled:cursor-not-allowed"
					>
						<div class="flex flex-col items-center gap-1">
							<ImageIcon class="w-4 h-4" />
							<span>Image→Video</span>
						</div>
					</button>
					<button
						type="button"
						onclick={() => handleModeChange('frame-bridge')}
						disabled={!availableModes.includes('frame-bridge') || isGenerating}
						class="px-3 py-2.5 text-xs font-medium rounded-lg transition-all {currentMode === 'frame-bridge' ? 'bg-[var(--primary)] text-white shadow-lg' : 'text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--muted)]'} disabled:opacity-50 disabled:cursor-not-allowed"
					>
						<div class="flex flex-col items-center gap-1">
							<Zap class="w-4 h-4" />
							<span>Bridge</span>
						</div>
					</button>
				</div>
			</div>

			<!-- Frame Upload Section (for image-to-video and frame-bridge modes) -->
			{#if currentMode === 'image-to-video' || currentMode === 'frame-bridge'}
				<div class="space-y-3">
					<div class="flex items-center gap-2">
						<label class="text-xs font-medium text-[var(--muted-foreground)] uppercase tracking-wider">
							{currentMode === 'frame-bridge' ? 'Start & End Frames' : 'Source Image'}
						</label>
					</div>

					<div class="grid {currentMode === 'frame-bridge' ? 'grid-cols-2' : 'grid-cols-1'} gap-3">
						<!-- Start Frame -->
						<div class="space-y-2">
							{#if currentMode === 'frame-bridge'}
								<div class="text-xs text-[var(--muted-foreground)]">Start Frame</div>
							{/if}
							{#if startFrame}
								<div class="relative group">
									<img
										src={startFrame.url}
										alt="Start frame"
										class="w-full aspect-video object-cover rounded-lg border border-[var(--border)]"
									/>
									<button
										type="button"
										onclick={() => clearFrame('start')}
										disabled={isGenerating}
										class="absolute -top-2 -right-2 w-6 h-6 bg-red-500 text-white rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity shadow-lg disabled:opacity-50"
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
									class="w-full aspect-video rounded-lg border-2 border-dashed border-[var(--border)] hover:border-[var(--primary)]/50 hover:bg-[var(--primary)]/5 flex flex-col items-center justify-center transition-all group disabled:opacity-50 disabled:cursor-not-allowed"
									aria-label="Upload start frame"
								>
									<Upload class="w-6 h-6 text-[var(--muted-foreground)] group-hover:text-[var(--primary)] transition-colors" />
									<span class="text-xs mt-2 text-[var(--muted-foreground)] group-hover:text-[var(--primary)] transition-colors">Upload Image</span>
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

						<!-- End Frame (Frame Bridging only) -->
						{#if currentMode === 'frame-bridge'}
							<div class="space-y-2">
								<div class="text-xs text-[var(--muted-foreground)]">End Frame</div>
								{#if endFrame}
									<div class="relative group">
										<img
											src={endFrame.url}
											alt="End frame"
											class="w-full aspect-video object-cover rounded-lg border border-[var(--border)]"
										/>
										<button
											type="button"
											onclick={() => clearFrame('end')}
											disabled={isGenerating}
											class="absolute -top-2 -right-2 w-6 h-6 bg-red-500 text-white rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity shadow-lg disabled:opacity-50"
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
										class="w-full aspect-video rounded-lg border-2 border-dashed border-[var(--border)] hover:border-[var(--primary)]/50 hover:bg-[var(--primary)]/5 flex flex-col items-center justify-center transition-all group disabled:opacity-50 disabled:cursor-not-allowed"
										aria-label="Upload end frame"
									>
										<Upload class="w-6 h-6 text-[var(--muted-foreground)] group-hover:text-[var(--primary)] transition-colors" />
										<span class="text-xs mt-2 text-[var(--muted-foreground)] group-hover:text-[var(--primary)] transition-colors">Upload Image</span>
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

							<!-- Transition indicator -->
							<div class="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-8 h-8 rounded-full bg-[var(--primary)] flex items-center justify-center shadow-lg pointer-events-none">
								<ChevronRight class="w-5 h-5 text-white" />
							</div>
						{/if}
					</div>
				</div>
			{/if}

			<!-- Prompt Input -->
			<div class="space-y-2">
				<label for="video-prompt" class="text-xs font-medium text-[var(--muted-foreground)] uppercase tracking-wider">
					Prompt
				</label>
				<textarea
					id="video-prompt"
					bind:value={prompt}
					placeholder="Describe the video you want to create..."
					rows="3"
					class="w-full bg-[var(--card)] border border-[var(--border)] rounded-xl px-4 py-3 text-sm text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:outline-none focus:ring-2 focus:ring-[var(--primary)]/50 resize-none transition-all"
					disabled={isGenerating}
				></textarea>
			</div>

			<!-- Provider & Model Selection -->
			<div class="space-y-2">
				<label class="text-xs font-medium text-[var(--muted-foreground)] uppercase tracking-wider">Provider & Model</label>
				<div class="grid grid-cols-2 gap-2">
					{#each providerGroups as group}
						{@const isSelected = group.models.some(m => m.id === selectedModelId)}
						<button
							type="button"
							onclick={() => handleModelChange(group.models[0].id)}
							disabled={isGenerating}
							class="p-3 rounded-xl border transition-all {isSelected ? 'border-[var(--primary)] bg-[var(--primary)]/10' : 'border-[var(--border)] hover:border-[var(--primary)]/50 bg-[var(--card)]'} disabled:opacity-50"
						>
							<div class="flex flex-col items-center gap-2">
								<div class="w-10 h-10 rounded-lg bg-gradient-to-br {isSelected ? 'from-[var(--primary)] to-[var(--primary)]/70' : 'from-[var(--muted)] to-[var(--muted)]/70'} flex items-center justify-center">
									<Film class="w-5 h-5 {isSelected ? 'text-white' : 'text-[var(--muted-foreground)]'}" />
								</div>
								<div class="text-xs font-medium {isSelected ? 'text-[var(--primary)]' : 'text-[var(--foreground)]'}">
									{group.displayName}
								</div>
								<div class="flex flex-wrap gap-1 justify-center">
									{#each group.models[0].capabilities as cap}
										{#if typeof cap === 'object' && 'imageToVideo' in cap && cap.imageToVideo}
											<div class="w-1.5 h-1.5 rounded-full bg-blue-400"></div>
										{/if}
										{#if typeof cap === 'object' && 'nativeAudio' in cap && cap.nativeAudio}
											<div class="w-1.5 h-1.5 rounded-full bg-orange-400"></div>
										{/if}
									{/each}
								</div>
							</div>
						</button>
					{/each}
				</div>

				<!-- Model details dropdown -->
				{#if providerGroups.length > 0}
					<details class="group">
						<summary class="px-3 py-2 rounded-lg bg-[var(--card)] border border-[var(--border)] cursor-pointer hover:bg-[var(--muted)] transition-colors text-xs text-[var(--muted-foreground)] flex items-center justify-between">
							<span>{currentModel.displayName}</span>
							<ChevronRight class="w-4 h-4 transition-transform group-open:rotate-90" />
						</summary>
						<div class="mt-2 p-3 rounded-lg bg-[var(--card)] border border-[var(--border)] space-y-2">
							<p class="text-xs text-[var(--muted-foreground)]">{currentModel.description}</p>
							<div class="flex flex-wrap gap-1">
								{#if currentModel.capabilities.imageToVideo}
									<span class="text-[10px] px-2 py-0.5 rounded bg-blue-500/20 text-blue-400">Image→Video</span>
								{/if}
								{#if currentModel.capabilities.frameBridging}
									<span class="text-[10px] px-2 py-0.5 rounded bg-purple-500/20 text-purple-400">Frame Bridge</span>
								{/if}
								{#if currentModel.capabilities.extension}
									<span class="text-[10px] px-2 py-0.5 rounded bg-green-500/20 text-green-400">Extend</span>
								{/if}
								{#if currentModel.capabilities.nativeAudio}
									<span class="text-[10px] px-2 py-0.5 rounded bg-orange-500/20 text-orange-400">Audio</span>
								{/if}
							</div>
						</div>
					</details>
				{/if}
			</div>

			<!-- Duration & Settings -->
			<div class="grid grid-cols-2 gap-3">
				<div class="space-y-2">
					<label class="text-xs font-medium text-[var(--muted-foreground)] uppercase tracking-wider">Duration</label>
					<div class="flex flex-wrap gap-1">
						{#each availableDurations as duration}
							<button
								type="button"
								onclick={() => handleDurationChange(duration)}
								disabled={isGenerating}
								class="px-3 py-2 text-xs font-medium rounded-lg transition-all {selectedDuration === duration ? 'bg-[var(--primary)] text-white shadow-lg' : 'bg-[var(--card)] text-[var(--foreground)] hover:bg-[var(--muted)] border border-[var(--border)]'} disabled:opacity-50"
								aria-pressed={selectedDuration === duration}
							>
								{duration}s
							</button>
						{/each}
					</div>
				</div>

				<div class="space-y-2">
					<label class="text-xs font-medium text-[var(--muted-foreground)] uppercase tracking-wider">Resolution</label>
					<div class="flex flex-wrap gap-1">
						{#each availableResolutions as resolution}
							<button
								type="button"
								onclick={() => handleResolutionChange(resolution)}
								disabled={isGenerating}
								class="px-3 py-2 text-xs font-medium rounded-lg transition-all {selectedResolution === resolution ? 'bg-[var(--primary)] text-white shadow-lg' : 'bg-[var(--card)] text-[var(--foreground)] hover:bg-[var(--muted)] border border-[var(--border)]'} disabled:opacity-50"
								aria-pressed={selectedResolution === resolution}
							>
								{resolution}
							</button>
						{/each}
					</div>
				</div>
			</div>

			<!-- Advanced Settings Toggle -->
			<div class="space-y-3">
				<button
					type="button"
					onclick={() => showAdvanced = !showAdvanced}
					class="w-full px-3 py-2 rounded-lg bg-[var(--card)] border border-[var(--border)] hover:bg-[var(--muted)] transition-colors flex items-center justify-between text-xs text-[var(--muted-foreground)]"
				>
					<div class="flex items-center gap-2">
						<Settings2 class="w-4 h-4" />
						<span>Advanced Settings</span>
					</div>
					<ChevronRight class="w-4 h-4 transition-transform {showAdvanced ? 'rotate-90' : ''}" />
				</button>

				{#if showAdvanced}
					<div class="space-y-3 p-3 rounded-lg bg-[var(--card)] border border-[var(--border)]">
						<!-- Aspect Ratio -->
						<div class="space-y-2">
							<label class="text-xs font-medium text-[var(--muted-foreground)]">Aspect Ratio</label>
							<div class="flex flex-wrap gap-2">
								{#each availableAspectRatios as ratio}
									<button
										type="button"
										onclick={() => handleAspectRatioChange(ratio.value)}
										disabled={isGenerating}
										class="flex flex-col items-center gap-1.5 px-3 py-2 rounded-lg transition-all {selectedAspectRatio === ratio.value ? 'bg-[var(--primary)]/10 border-[var(--primary)] border-2' : 'bg-[var(--background)] border border-[var(--border)] hover:bg-[var(--muted)]'} disabled:opacity-50"
										aria-pressed={selectedAspectRatio === ratio.value}
										aria-label="Aspect ratio {ratio.label}"
									>
										<div
											class="rounded-sm {selectedAspectRatio === ratio.value ? 'bg-[var(--primary)]' : 'bg-[var(--muted-foreground)]/50'}"
											style="width: {ratio.width}px; height: {ratio.height}px;"
										></div>
										<span class="text-xs font-medium">{ratio.label}</span>
									</button>
								{/each}
							</div>
						</div>

						<!-- Audio Toggle -->
						{#if showAudioToggle}
							<div class="space-y-2">
								<label class="text-xs font-medium text-[var(--muted-foreground)]">Audio</label>
								<button
									type="button"
									onclick={toggleAudio}
									disabled={isGenerating}
									class="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg border transition-all {audioEnabled ? 'border-[var(--primary)] bg-[var(--primary)]/10' : 'border-[var(--border)] hover:bg-[var(--muted)]'} disabled:opacity-50"
									aria-pressed={audioEnabled}
								>
									<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {audioEnabled ? 'bg-[var(--primary)] text-white' : 'bg-[var(--muted)] text-[var(--muted-foreground)]'}">
										{#if audioEnabled}
											<Volume2 class="w-4 h-4" />
										{:else}
											<VolumeX class="w-4 h-4" />
										{/if}
									</div>
									<div class="flex-1 text-left">
										<div class="text-xs font-medium text-[var(--foreground)]">
											{audioEnabled ? 'Audio Enabled' : 'Audio Disabled'}
										</div>
									</div>
									<div class="shrink-0 w-10 h-5 rounded-full transition-colors {audioEnabled ? 'bg-[var(--primary)]' : 'bg-[var(--muted-foreground)]/30'}">
										<div class="w-4 h-4 mt-0.5 rounded-full bg-white shadow-sm transition-transform {audioEnabled ? 'translate-x-5' : 'translate-x-0.5'}"></div>
									</div>
								</button>
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
				class="w-full px-6 py-4 text-sm font-semibold bg-gradient-to-r from-[var(--primary)] to-[var(--primary)]/80 text-white rounded-xl hover:from-[var(--primary)]/90 hover:to-[var(--primary)]/70 transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 shadow-lg shadow-[var(--primary)]/20"
			>
				{#if isGenerating}
					<div class="w-5 h-5 border-2 border-current border-t-transparent rounded-full animate-spin"></div>
					<span>Generating...</span>
				{:else}
					<Clapperboard class="w-5 h-5" />
					<span>Generate Video</span>
				{/if}
			</button>

			<!-- Progress Display (when generating) -->
			{#if isGenerating}
				<div class="p-4 rounded-xl bg-[var(--card)] border border-[var(--border)] space-y-3">
					<div class="flex items-center justify-between text-xs">
						<span class="text-[var(--muted-foreground)]">Processing</span>
						<span class="text-[var(--primary)] font-medium">45%</span>
					</div>
					<div class="h-2 bg-[var(--background)] rounded-full overflow-hidden">
						<div class="h-full bg-gradient-to-r from-[var(--primary)] to-[var(--primary)]/70 transition-all duration-500 animate-pulse" style="width: 45%"></div>
					</div>
					<div class="flex items-center justify-between text-xs text-[var(--muted-foreground)]">
						<span>Stage: Rendering</span>
						<span>~30s remaining</span>
					</div>
				</div>
			{/if}
		</div>
	</div>
</div>

<style>
	/* Custom scrollbar styles */
	.scrollbar-thin {
		scrollbar-width: thin;
	}

	.scrollbar-thin::-webkit-scrollbar {
		width: 6px;
		height: 6px;
	}

	.scrollbar-thin::-webkit-scrollbar-track {
		background: transparent;
	}

	.scrollbar-thin::-webkit-scrollbar-thumb {
		background: var(--border);
		border-radius: 3px;
	}

	.scrollbar-thin::-webkit-scrollbar-thumb:hover {
		background: var(--muted-foreground);
	}

	/* Smooth animations */
	* {
		transition-property: background-color, border-color, color, fill, stroke;
		transition-timing-function: cubic-bezier(0.4, 0, 0.2, 1);
		transition-duration: 150ms;
	}

	button {
		transition-property: all;
	}
</style>

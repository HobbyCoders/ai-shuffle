<script lang="ts">
	import { Volume2, Play, Pause, Download, Mic2 } from 'lucide-svelte';
	import {
		ALL_TTS_MODELS,
		getTTSModel,
		type TTSModel,
		type TTSVoice
	} from '$lib/types/ai-models';
	import {
		studio,
		ttsModel,
		ttsVoice,
		currentTTSModelInfo,
		defaultTTSSpeed,
		defaultTTSFormat
	} from '$lib/stores/studio';

	// Props
	interface Props {
		onStartGeneration?: (type: 'tts', config: TTSGenerationConfig) => void;
	}

	let { onStartGeneration }: Props = $props();

	// Types
	interface TTSGenerationConfig {
		text: string;
		model: string;
		voice: string;
		speed: number;
		outputFormat: string;
		voiceInstructions?: string;
	}

	// Group models by provider
	const modelsByProvider = $derived.by(() => {
		const groups: Record<string, TTSModel[]> = {
			'google-tts': [],
			'openai-tts': []
		};
		for (const model of ALL_TTS_MODELS) {
			if (groups[model.provider]) {
				groups[model.provider].push(model);
			}
		}
		return groups;
	});

	const providerLabels: Record<string, string> = {
		'google-tts': 'Google Cloud TTS',
		'openai-tts': 'OpenAI TTS'
	};

	// State
	let text = $state('');
	let selectedModel = $state($ttsModel);
	let selectedVoice = $state($ttsVoice);
	let speed = $state($defaultTTSSpeed);
	let outputFormat = $state($defaultTTSFormat);
	let voiceInstructions = $state('');
	let isGenerating = $state(false);

	// Audio playback state
	let audioUrl: string | null = $state(null);
	let audioElement: HTMLAudioElement | null = $state(null);
	let isPlaying = $state(false);
	let audioDuration = $state(0);
	let audioCurrentTime = $state(0);

	// Derived
	let currentModel = $derived(getTTSModel(selectedModel));
	let availableVoices = $derived(currentModel?.voices || []);
	let speedRange = $derived(currentModel?.speedRange || { min: 0.25, max: 4.0 });
	let outputFormats = $derived(currentModel?.outputFormats || ['mp3']);
	let showVoiceInstructions = $derived(currentModel?.capabilities.steerability === true);
	let canGenerate = $derived(text.trim().length > 0 && !isGenerating);

	// Effects to sync with store
	$effect(() => {
		selectedModel = $ttsModel;
	});

	$effect(() => {
		selectedVoice = $ttsVoice;
	});

	$effect(() => {
		speed = $defaultTTSSpeed;
	});

	$effect(() => {
		outputFormat = $defaultTTSFormat;
	});

	// Reset voice when model changes if current voice is not available
	$effect(() => {
		if (availableVoices.length > 0) {
			const voiceExists = availableVoices.some(v => v.id === selectedVoice);
			if (!voiceExists) {
				selectedVoice = availableVoices[0].id;
				studio.setTTSVoice(selectedVoice);
			}
		}
	});

	// Handlers
	function handleModelChange(modelId: string) {
		selectedModel = modelId;
		studio.setTTSModel(modelId);
		// Reset voice instructions when switching away from steerable model
		const model = getTTSModel(modelId);
		if (!model?.capabilities.steerability) {
			voiceInstructions = '';
		}
	}

	function handleVoiceChange(event: Event) {
		const select = event.target as HTMLSelectElement;
		selectedVoice = select.value;
		studio.setTTSVoice(selectedVoice);
	}

	function handleSpeedChange(event: Event) {
		const input = event.target as HTMLInputElement;
		speed = parseFloat(input.value);
		studio.setDefaultTTSSpeed(speed);
	}

	function handleFormatChange(event: Event) {
		const select = event.target as HTMLSelectElement;
		outputFormat = select.value;
		studio.setDefaultTTSFormat(outputFormat);
	}

	async function handleGenerate() {
		if (!canGenerate) return;

		isGenerating = true;

		try {
			const config: TTSGenerationConfig = {
				text,
				model: selectedModel,
				voice: selectedVoice,
				speed,
				outputFormat,
				voiceInstructions: showVoiceInstructions && voiceInstructions.trim() ? voiceInstructions : undefined
			};

			// Call the callback if provided
			if (onStartGeneration) {
				await onStartGeneration('tts', config);
			} else {
				// Directly call the store method
				const result = await studio.generateTTS(text, {
					model: selectedModel,
					voice: selectedVoice,
					speed,
					outputFormat,
					voiceInstructions: showVoiceInstructions && voiceInstructions.trim() ? voiceInstructions : undefined
				});

				if (result?.result?.url) {
					// Clean up previous audio
					if (audioUrl) {
						URL.revokeObjectURL(audioUrl);
					}
					audioUrl = result.result.url;
				}
			}

			// Clear text after successful generation
			text = '';
		} catch (error) {
			console.error('TTS generation failed:', error);
		} finally {
			isGenerating = false;
		}
	}

	function togglePlayback() {
		if (!audioElement) return;

		if (isPlaying) {
			audioElement.pause();
		} else {
			audioElement.play();
		}
	}

	function handleAudioPlay() {
		isPlaying = true;
	}

	function handleAudioPause() {
		isPlaying = false;
	}

	function handleAudioEnded() {
		isPlaying = false;
		audioCurrentTime = 0;
	}

	function handleAudioTimeUpdate() {
		if (audioElement) {
			audioCurrentTime = audioElement.currentTime;
		}
	}

	function handleAudioLoadedMetadata() {
		if (audioElement) {
			audioDuration = audioElement.duration;
		}
	}

	function handleSeek(event: Event) {
		const input = event.target as HTMLInputElement;
		const time = parseFloat(input.value);
		if (audioElement) {
			audioElement.currentTime = time;
			audioCurrentTime = time;
		}
	}

	function downloadAudio() {
		if (!audioUrl) return;
		const a = document.createElement('a');
		a.href = audioUrl;
		a.download = `tts-${Date.now()}.${outputFormat}`;
		document.body.appendChild(a);
		a.click();
		document.body.removeChild(a);
	}

	function formatTime(seconds: number): string {
		const mins = Math.floor(seconds / 60);
		const secs = Math.floor(seconds % 60);
		return `${mins}:${secs.toString().padStart(2, '0')}`;
	}

	function getGenderIcon(gender?: 'male' | 'female' | 'neutral'): string {
		switch (gender) {
			case 'male':
				return 'M';
			case 'female':
				return 'F';
			case 'neutral':
				return 'N';
			default:
				return '-';
		}
	}

	function getCapabilityBadges(model: TTSModel): { label: string; active: boolean }[] {
		return [
			{ label: 'Streaming', active: model.capabilities.streaming },
			{ label: 'SSML', active: model.capabilities.ssml },
			{ label: 'Custom Voices', active: model.capabilities.customVoices },
			{ label: 'Steerability', active: model.capabilities.steerability },
			{ label: 'Multi-Speaker', active: model.capabilities.multiSpeaker },
			{ label: 'Emotion Control', active: model.capabilities.emotionControl }
		].filter(b => b.active);
	}
</script>

<div class="p-4 space-y-6">
	<!-- Text Input -->
	<div>
		<label for="tts-text" class="block text-sm font-medium text-foreground mb-2">
			Text to Convert
		</label>
		<textarea
			id="tts-text"
			bind:value={text}
			placeholder="Enter the text you want to convert to speech..."
			rows="4"
			class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
			disabled={isGenerating}
		></textarea>
		{#if currentModel}
			<p class="text-xs text-muted-foreground mt-1">
				Max {currentModel.maxInputLength.toLocaleString()} characters
			</p>
		{/if}
	</div>

	<!-- Model Selector -->
	<div>
		<label class="block text-xs text-muted-foreground mb-2">Model</label>
		<div class="space-y-4">
			{#each Object.entries(modelsByProvider) as [provider, models]}
				{#if models.length > 0}
					<div>
						<div class="text-xs font-medium text-muted-foreground mb-2 uppercase tracking-wide">
							{providerLabels[provider]}
						</div>
						<div class="space-y-2">
							{#each models as model}
								<button
									type="button"
									onclick={() => handleModelChange(model.id)}
									disabled={isGenerating}
									class="w-full flex flex-col gap-2 p-3 rounded-lg border transition-colors text-left {selectedModel === model.id ? 'border-primary bg-primary/5' : 'border-border hover:border-muted-foreground/50 hover:bg-muted'}"
									aria-pressed={selectedModel === model.id}
								>
									<div class="flex items-start gap-3">
										<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {selectedModel === model.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
											<Volume2 class="w-4 h-4" />
										</div>
										<div class="flex-1 min-w-0">
											<div class="text-sm font-medium text-foreground">{model.displayName}</div>
											<div class="text-xs text-muted-foreground">{model.description}</div>
										</div>
										{#if selectedModel === model.id}
											<div class="shrink-0 w-2 h-2 rounded-full bg-primary mt-2"></div>
										{/if}
									</div>
									<!-- Capability Badges -->
									{#if getCapabilityBadges(model).length > 0}
										<div class="flex flex-wrap gap-1 pl-11">
											{#each getCapabilityBadges(model) as badge}
												<span class="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
													{badge.label}
												</span>
											{/each}
										</div>
									{/if}
								</button>
							{/each}
						</div>
					</div>
				{/if}
			{/each}
		</div>
	</div>

	<!-- Voice Selector -->
	{#if availableVoices.length > 0}
		<div>
			<label for="voice-select" class="block text-xs text-muted-foreground mb-2">
				Voice
			</label>
			<div class="relative">
				<Mic2 class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
				<select
					id="voice-select"
					value={selectedVoice}
					onchange={handleVoiceChange}
					disabled={isGenerating}
					class="w-full bg-muted border border-border rounded-lg pl-10 pr-4 py-2.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 appearance-none cursor-pointer"
				>
					{#each availableVoices as voice}
						<option value={voice.id}>
							{voice.name} ({getGenderIcon(voice.gender)})
						</option>
					{/each}
				</select>
				<div class="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none">
					<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</div>
			</div>
		</div>
	{/if}

	<!-- Speed Slider -->
	<div>
		<div class="flex items-center justify-between mb-2">
			<label for="speed-slider" class="text-xs text-muted-foreground">
				Speed
			</label>
			<span class="text-xs font-medium text-foreground">{speed.toFixed(2)}x</span>
		</div>
		<input
			id="speed-slider"
			type="range"
			min={speedRange.min}
			max={speedRange.max}
			step="0.05"
			value={speed}
			oninput={handleSpeedChange}
			disabled={isGenerating}
			class="w-full h-2 bg-muted rounded-lg appearance-none cursor-pointer accent-primary"
		/>
		<div class="flex justify-between text-[10px] text-muted-foreground mt-1">
			<span>{speedRange.min}x</span>
			<span>1.0x</span>
			<span>{speedRange.max}x</span>
		</div>
	</div>

	<!-- Output Format -->
	<div>
		<label for="format-select" class="block text-xs text-muted-foreground mb-2">
			Output Format
		</label>
		<select
			id="format-select"
			value={outputFormat}
			onchange={handleFormatChange}
			disabled={isGenerating}
			class="w-full bg-muted border border-border rounded-lg px-4 py-2.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 appearance-none cursor-pointer"
		>
			{#each outputFormats as format}
				<option value={format}>{format.toUpperCase()}</option>
			{/each}
		</select>
	</div>

	<!-- Voice Instructions (for steerable models like GPT-4o-mini-TTS) -->
	{#if showVoiceInstructions}
		<div>
			<label for="voice-instructions" class="block text-xs text-muted-foreground mb-2">
				Voice Instructions (optional)
			</label>
			<p class="text-xs text-muted-foreground mb-2">
				Describe how the voice should speak (e.g., "Speak slowly and calmly like a meditation guide")
			</p>
			<textarea
				id="voice-instructions"
				bind:value={voiceInstructions}
				placeholder="Speak with enthusiasm and energy..."
				rows="2"
				class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
				disabled={isGenerating}
			></textarea>
		</div>
	{/if}

	<!-- Audio Preview/Playback -->
	{#if audioUrl}
		<div class="border border-border rounded-xl p-4 bg-muted/50">
			<div class="text-xs text-muted-foreground mb-3">Generated Audio</div>

			<audio
				bind:this={audioElement}
				src={audioUrl}
				onplay={handleAudioPlay}
				onpause={handleAudioPause}
				onended={handleAudioEnded}
				ontimeupdate={handleAudioTimeUpdate}
				onloadedmetadata={handleAudioLoadedMetadata}
				class="hidden"
			></audio>

			<div class="flex items-center gap-3">
				<button
					type="button"
					onclick={togglePlayback}
					class="shrink-0 w-10 h-10 rounded-full bg-primary text-primary-foreground flex items-center justify-center hover:bg-primary/90 transition-colors"
					aria-label={isPlaying ? 'Pause' : 'Play'}
				>
					{#if isPlaying}
						<Pause class="w-4 h-4" />
					{:else}
						<Play class="w-4 h-4 ml-0.5" />
					{/if}
				</button>

				<div class="flex-1">
					<input
						type="range"
						min="0"
						max={audioDuration || 0}
						step="0.1"
						value={audioCurrentTime}
						oninput={handleSeek}
						class="w-full h-1.5 bg-border rounded-lg appearance-none cursor-pointer accent-primary"
					/>
					<div class="flex justify-between text-[10px] text-muted-foreground mt-1">
						<span>{formatTime(audioCurrentTime)}</span>
						<span>{formatTime(audioDuration)}</span>
					</div>
				</div>

				<button
					type="button"
					onclick={downloadAudio}
					class="shrink-0 w-8 h-8 rounded-lg bg-muted text-muted-foreground flex items-center justify-center hover:bg-muted/80 hover:text-foreground transition-colors"
					aria-label="Download audio"
				>
					<Download class="w-4 h-4" />
				</button>
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
				<Volume2 class="w-4 h-4" />
				Generate Speech
			{/if}
		</button>
	</div>
</div>

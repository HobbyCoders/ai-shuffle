<script lang="ts">
	import { Volume2, Play, Pause, Download, Mic2, SkipBack, SkipForward, Repeat, Gauge } from 'lucide-svelte';
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
		'google-tts': 'Google Cloud',
		'openai-tts': 'OpenAI'
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
	let isLooping = $state(false);

	// Derived
	let currentModel = $derived(getTTSModel(selectedModel));
	let availableVoices = $derived(currentModel?.voices || []);
	let speedRange = $derived(currentModel?.speedRange || { min: 0.25, max: 4.0 });
	let outputFormats = $derived(currentModel?.outputFormats || ['mp3']);
	let showVoiceInstructions = $derived(currentModel?.capabilities.steerability === true);
	let canGenerate = $derived(text.trim().length > 0 && !isGenerating);
	let characterCount = $derived(text.length);
	let maxCharacters = $derived(currentModel?.maxInputLength || 4096);
	let isNearLimit = $derived(characterCount > maxCharacters * 0.9);

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

	function handleVoiceChange(voiceId: string) {
		selectedVoice = voiceId;
		studio.setTTSVoice(voiceId);
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
		if (!isLooping) {
			audioCurrentTime = 0;
		}
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

	function skipBackward() {
		if (audioElement) {
			audioElement.currentTime = Math.max(0, audioElement.currentTime - 10);
		}
	}

	function skipForward() {
		if (audioElement) {
			audioElement.currentTime = Math.min(audioDuration, audioElement.currentTime + 10);
		}
	}

	function toggleLoop() {
		isLooping = !isLooping;
		if (audioElement) {
			audioElement.loop = isLooping;
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
				return '♂';
			case 'female':
				return '♀';
			case 'neutral':
				return '◆';
			default:
				return '';
		}
	}

	function getVoiceInitial(voiceName: string): string {
		return voiceName.charAt(0).toUpperCase();
	}
</script>

<div class="flex flex-col h-full overflow-hidden">
	<!-- Audio Preview Section - 30% -->
	{#if audioUrl}
		<div class="h-[30%] border-b border-border/50 p-6 bg-gradient-to-b from-card via-card to-background/50">
			<div class="text-xs text-muted-foreground mb-4 uppercase tracking-wider font-medium">Audio Preview</div>

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

			<!-- Animated Waveform -->
			<div class="mb-6 h-16 flex items-center justify-center gap-1 px-4 rounded-lg bg-card/50 backdrop-blur-sm border border-border/30">
				{#each Array(32) as _, i}
					<div
						class="w-1 bg-primary/60 rounded-full transition-all duration-300"
						style="height: {isPlaying ? Math.random() * 60 + 20 : 20}%; animation: {isPlaying ? `pulse ${0.5 + Math.random() * 0.5}s ease-in-out infinite alternate` : 'none'}; animation-delay: {i * 0.05}s;"
					></div>
				{/each}
			</div>

			<!-- Playback Controls -->
			<div class="flex items-center gap-4 mb-4">
				<button
					type="button"
					onclick={skipBackward}
					class="w-9 h-9 rounded-lg bg-card border border-border/50 text-muted-foreground hover:text-foreground hover:border-primary/50 transition-all flex items-center justify-center"
					aria-label="Skip backward 10 seconds"
				>
					<SkipBack class="w-4 h-4" />
				</button>

				<button
					type="button"
					onclick={togglePlayback}
					class="w-12 h-12 rounded-full bg-primary text-primary-foreground flex items-center justify-center hover:bg-primary/90 transition-all shadow-lg shadow-primary/25"
					aria-label={isPlaying ? 'Pause' : 'Play'}
				>
					{#if isPlaying}
						<Pause class="w-5 h-5" />
					{:else}
						<Play class="w-5 h-5 ml-0.5" />
					{/if}
				</button>

				<button
					type="button"
					onclick={skipForward}
					class="w-9 h-9 rounded-lg bg-card border border-border/50 text-muted-foreground hover:text-foreground hover:border-primary/50 transition-all flex items-center justify-center"
					aria-label="Skip forward 10 seconds"
				>
					<SkipForward class="w-4 h-4" />
				</button>

				<!-- Progress Bar -->
				<div class="flex-1">
					<input
						type="range"
						min="0"
						max={audioDuration || 0}
						step="0.1"
						value={audioCurrentTime}
						oninput={handleSeek}
						class="w-full h-1.5 bg-border/30 rounded-full appearance-none cursor-pointer accent-primary"
						style="background: linear-gradient(to right, oklch(0.72 0.14 180) 0%, oklch(0.72 0.14 180) {(audioCurrentTime / audioDuration) * 100}%, oklch(0.18 0.008 260 / 0.3) {(audioCurrentTime / audioDuration) * 100}%, oklch(0.18 0.008 260 / 0.3) 100%)"
					/>
					<div class="flex justify-between text-[10px] text-muted-foreground mt-1 px-1">
						<span class="font-mono">{formatTime(audioCurrentTime)}</span>
						<span class="font-mono">{formatTime(audioDuration)}</span>
					</div>
				</div>

				<button
					type="button"
					onclick={toggleLoop}
					class="w-9 h-9 rounded-lg border transition-all flex items-center justify-center {isLooping ? 'bg-primary/10 border-primary/50 text-primary' : 'bg-card border-border/50 text-muted-foreground hover:text-foreground'}"
					aria-label="Toggle loop"
				>
					<Repeat class="w-4 h-4" />
				</button>

				<button
					type="button"
					onclick={downloadAudio}
					class="w-9 h-9 rounded-lg bg-card border border-border/50 text-muted-foreground hover:text-foreground hover:border-primary/50 transition-all flex items-center justify-center"
					aria-label="Download audio"
				>
					<Download class="w-4 h-4" />
				</button>
			</div>
		</div>
	{/if}

	<!-- Controls Section - 70% -->
	<div class="flex-1 overflow-y-auto p-6 space-y-6">
		<!-- Text Input -->
		<div>
			<label for="tts-text" class="block text-xs text-muted-foreground mb-2 uppercase tracking-wider font-medium">
				Text Input
			</label>
			<div class="relative">
				<textarea
					id="tts-text"
					bind:value={text}
					placeholder="Enter text to speak..."
					rows="5"
					class="w-full bg-card border border-border/50 rounded-xl px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 resize-none transition-all"
					disabled={isGenerating}
				></textarea>
				<div class="absolute bottom-3 right-3 text-xs font-mono {isNearLimit ? 'text-destructive' : 'text-muted-foreground'}">
					{characterCount}/{maxCharacters}
				</div>
			</div>
		</div>

		<!-- Voice Selection Cards -->
		{#if availableVoices.length > 0}
			<div>
				<label class="block text-xs text-muted-foreground mb-3 uppercase tracking-wider font-medium">
					Voice Selection
				</label>
				<div class="grid grid-cols-2 gap-2">
					{#each availableVoices.slice(0, 6) as voice}
						<button
							type="button"
							onclick={() => handleVoiceChange(voice.id)}
							disabled={isGenerating}
							class="flex items-center gap-3 p-3 rounded-lg border transition-all text-left {selectedVoice === voice.id ? 'border-primary bg-primary/5 shadow-lg shadow-primary/10' : 'border-border/50 hover:border-primary/30 hover:bg-card/50'} disabled:opacity-50"
						>
							<div class="shrink-0 w-9 h-9 rounded-full flex items-center justify-center text-xs font-bold {selectedVoice === voice.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
								{getVoiceInitial(voice.name)}
							</div>
							<div class="flex-1 min-w-0">
								<div class="text-xs font-medium text-foreground truncate">{voice.name}</div>
								<div class="text-[10px] text-muted-foreground">{getGenderIcon(voice.gender)} {voice.language || 'EN'}</div>
							</div>
						</button>
					{/each}
				</div>
				{#if availableVoices.length > 6}
					<details class="mt-2">
						<summary class="text-xs text-primary hover:text-primary/80 cursor-pointer select-none">
							Show {availableVoices.length - 6} more voices
						</summary>
						<div class="grid grid-cols-2 gap-2 mt-2">
							{#each availableVoices.slice(6) as voice}
								<button
									type="button"
									onclick={() => handleVoiceChange(voice.id)}
									disabled={isGenerating}
									class="flex items-center gap-3 p-3 rounded-lg border transition-all text-left {selectedVoice === voice.id ? 'border-primary bg-primary/5 shadow-lg shadow-primary/10' : 'border-border/50 hover:border-primary/30 hover:bg-card/50'} disabled:opacity-50"
								>
									<div class="shrink-0 w-9 h-9 rounded-full flex items-center justify-center text-xs font-bold {selectedVoice === voice.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
										{getVoiceInitial(voice.name)}
									</div>
									<div class="flex-1 min-w-0">
										<div class="text-xs font-medium text-foreground truncate">{voice.name}</div>
										<div class="text-[10px] text-muted-foreground">{getGenderIcon(voice.gender)} {voice.language || 'EN'}</div>
									</div>
								</button>
							{/each}
						</div>
					</details>
				{/if}
			</div>
		{/if}

		<!-- Provider Selection -->
		<div>
			<label class="block text-xs text-muted-foreground mb-3 uppercase tracking-wider font-medium">
				Provider
			</label>
			<div class="flex gap-2">
				{#each Object.entries(modelsByProvider) as [provider, models]}
					{#if models.length > 0}
						{@const model = models[0]}
						<button
							type="button"
							onclick={() => handleModelChange(model.id)}
							disabled={isGenerating}
							class="flex-1 flex items-center gap-2 p-3 rounded-lg border transition-all {selectedModel === model.id ? 'border-primary bg-primary/5' : 'border-border/50 hover:border-primary/30 hover:bg-card/50'} disabled:opacity-50"
						>
							<div class="w-8 h-8 rounded-lg flex items-center justify-center {selectedModel === model.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
								<Volume2 class="w-4 h-4" />
							</div>
							<div class="flex-1 text-left">
								<div class="text-xs font-medium text-foreground">{providerLabels[provider]}</div>
							</div>
						</button>
					{/if}
				{/each}
			</div>
		</div>

		<!-- Options -->
		<div class="space-y-4 p-4 rounded-xl bg-card/30 border border-border/30">
			<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium">Options</div>

			<!-- Speed Slider -->
			<div>
				<div class="flex items-center justify-between mb-2">
					<label for="speed-slider" class="text-xs text-muted-foreground flex items-center gap-2">
						<Gauge class="w-3.5 h-3.5" />
						Speed
					</label>
					<span class="text-xs font-mono font-medium text-foreground bg-muted px-2 py-0.5 rounded">{speed.toFixed(2)}x</span>
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
					Format
				</label>
				<select
					id="format-select"
					value={outputFormat}
					onchange={handleFormatChange}
					disabled={isGenerating}
					class="w-full bg-muted border border-border/50 rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 appearance-none cursor-pointer"
				>
					{#each outputFormats as format}
						<option value={format}>{format.toUpperCase()}</option>
					{/each}
				</select>
			</div>
		</div>

		<!-- Voice Instructions (for steerable models) -->
		{#if showVoiceInstructions}
			<div>
				<label for="voice-instructions" class="block text-xs text-muted-foreground mb-2 uppercase tracking-wider font-medium">
					Voice Instructions (Optional)
				</label>
				<textarea
					id="voice-instructions"
					bind:value={voiceInstructions}
					placeholder="Describe how the voice should speak (e.g., 'Speak slowly and calmly like a meditation guide')"
					rows="2"
					class="w-full bg-card border border-border/50 rounded-xl px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none transition-all"
					disabled={isGenerating}
				></textarea>
			</div>
		{/if}

		<!-- Generate Button -->
		<button
			type="button"
			onclick={handleGenerate}
			disabled={!canGenerate}
			class="w-full px-6 py-4 text-sm font-medium bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 shadow-lg shadow-primary/25"
		>
			{#if isGenerating}
				<div class="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin"></div>
				Generating Speech...
			{:else}
				<Volume2 class="w-5 h-5" />
				Generate Speech
			{/if}
		</button>
	</div>
</div>

<style>
	@keyframes pulse {
		from {
			opacity: 0.6;
		}
		to {
			opacity: 1;
		}
	}
</style>

<script lang="ts">
	/**
	 * StudioCard - Quick generation card for The Deck
	 *
	 * A compact, mode-based interface for fast iteration on:
	 * - Image generation
	 * - Video generation
	 * - Text-to-Speech (TTS)
	 * - Speech-to-Text (STT)
	 *
	 * For full tool suites, users can switch to Studio workspace.
	 */

	import {
		Image,
		Film,
		Volume2,
		Mic,
		Loader2,
		Sparkles,
		ExternalLink,
		Play,
		Pause,
		Download,
		Upload,
		X,
		ChevronDown
	} from 'lucide-svelte';
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import {
		studio,
		imageModel,
		videoModel,
		ttsModel,
		ttsVoice,
		sttModel,
		type DeckGeneration
	} from '$lib/stores/studio';
	import {
		ALL_IMAGE_MODELS,
		ALL_VIDEO_MODELS,
		ALL_TTS_MODELS,
		ALL_STT_MODELS,
		getImageModel,
		getVideoModel,
		getTTSModel,
		getSTTModel,
		IMAGE_ASPECT_RATIOS,
		VIDEO_ASPECT_RATIOS,
		PROVIDER_DISPLAY_NAMES
	} from '$lib/types/ai-models';
	import { api } from '$lib/api/client';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let {
		card,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false
	}: Props = $props();

	// Mode state
	type StudioMode = 'image' | 'video' | 'tts' | 'stt';
	let activeMode: StudioMode = $state('image');

	// Generation state
	let isGenerating = $state(false);
	let error = $state<string | null>(null);
	let lastGeneration = $state<DeckGeneration | null>(null);

	// Image mode state
	let imagePrompt = $state('');
	let selectedImageModel = $state($imageModel);
	let selectedImageAspectRatio = $state('16:9');

	// Video mode state
	let videoPrompt = $state('');
	let selectedVideoModel = $state($videoModel);
	let selectedVideoDuration = $state(8);
	let selectedVideoAspectRatio = $state('16:9');

	// TTS mode state
	let ttsText = $state('');
	let selectedTTSModel = $state($ttsModel);
	let selectedTTSVoice = $state($ttsVoice);
	let ttsSpeed = $state(1.0);

	// STT mode state
	let sttFile: File | null = $state(null);
	let sttFileUrl: string | null = $state(null);
	let selectedSTTModel = $state($sttModel);
	let sttLanguage = $state('auto');
	let sttTranscript = $state<string | null>(null);
	let isTranscribing = $state(false);

	// Audio playback state (for TTS results)
	let audioElement: HTMLAudioElement | null = $state(null);
	let isPlaying = $state(false);

	// Derived values
	let currentImageModel = $derived(getImageModel(selectedImageModel));
	let currentVideoModel = $derived(getVideoModel(selectedVideoModel));
	let currentTTSModel = $derived(getTTSModel(selectedTTSModel));
	let currentSTTModel = $derived(getSTTModel(selectedSTTModel));

	// Filtered aspect ratios based on model
	let availableImageAspectRatios = $derived(
		IMAGE_ASPECT_RATIOS.filter(
			(ar) => !currentImageModel?.aspectRatios || currentImageModel.aspectRatios.includes(ar.value)
		).slice(0, 5) // Only show first 5 for compact UI
	);

	let availableVideoAspectRatios = $derived(
		VIDEO_ASPECT_RATIOS.filter((ar) => currentVideoModel?.aspectRatios.includes(ar.value))
	);

	let availableVideoDurations = $derived(currentVideoModel?.durations || [4, 8]);

	let availableTTSVoices = $derived(currentTTSModel?.voices || []);

	// Model options grouped by provider (for compact dropdowns)
	let imageModelOptions = $derived(
		ALL_IMAGE_MODELS.filter((m) => !m.deprecated).map((m) => ({
			value: m.id,
			label: m.displayName,
			provider: PROVIDER_DISPLAY_NAMES[m.provider] || m.provider
		}))
	);

	let videoModelOptions = $derived(
		ALL_VIDEO_MODELS.filter((m) => !m.deprecated).map((m) => ({
			value: m.id,
			label: m.displayName,
			provider: PROVIDER_DISPLAY_NAMES[m.provider] || m.provider
		}))
	);

	let ttsModelOptions = $derived(
		ALL_TTS_MODELS.map((m) => ({
			value: m.id,
			label: m.displayName,
			provider: PROVIDER_DISPLAY_NAMES[m.provider] || m.provider
		}))
	);

	let sttModelOptions = $derived(
		ALL_STT_MODELS.map((m) => ({
			value: m.id,
			label: m.displayName,
			provider: PROVIDER_DISPLAY_NAMES[m.provider] || m.provider
		}))
	);

	// Common languages for STT
	const STT_LANGUAGES = [
		{ code: 'auto', name: 'Auto-detect' },
		{ code: 'en', name: 'English' },
		{ code: 'es', name: 'Spanish' },
		{ code: 'fr', name: 'French' },
		{ code: 'de', name: 'German' },
		{ code: 'ja', name: 'Japanese' },
		{ code: 'zh', name: 'Chinese' },
		{ code: 'ko', name: 'Korean' }
	];

	// Handlers
	function setMode(mode: StudioMode) {
		activeMode = mode;
		error = null;
	}

	async function handleImageGenerate() {
		if (!imagePrompt.trim() || isGenerating) return;

		isGenerating = true;
		error = null;

		try {
			const result = await studio.generateImage(imagePrompt, {
				model: selectedImageModel,
				aspectRatio: selectedImageAspectRatio
			});

			if (result) {
				lastGeneration = result;
				imagePrompt = '';
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Image generation failed';
		} finally {
			isGenerating = false;
		}
	}

	async function handleVideoGenerate() {
		if (!videoPrompt.trim() || isGenerating) return;

		isGenerating = true;
		error = null;

		try {
			const result = await studio.generateVideo(videoPrompt, {
				model: selectedVideoModel,
				duration: selectedVideoDuration,
				aspectRatio: selectedVideoAspectRatio
			});

			if (result) {
				lastGeneration = result;
				videoPrompt = '';
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Video generation failed';
		} finally {
			isGenerating = false;
		}
	}

	async function handleTTSGenerate() {
		if (!ttsText.trim() || isGenerating) return;

		isGenerating = true;
		error = null;

		try {
			const result = await studio.generateTTS(ttsText, {
				model: selectedTTSModel,
				voice: selectedTTSVoice,
				speed: ttsSpeed
			});

			if (result) {
				lastGeneration = result;
				ttsText = '';
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'TTS generation failed';
		} finally {
			isGenerating = false;
		}
	}

	function handleSTTFileSelect(event: Event) {
		const input = event.target as HTMLInputElement;
		if (!input.files?.length) return;

		const file = input.files[0];
		if (sttFileUrl) {
			URL.revokeObjectURL(sttFileUrl);
		}

		sttFile = file;
		sttFileUrl = URL.createObjectURL(file);
		sttTranscript = null;
		input.value = '';
	}

	function clearSTTFile() {
		if (sttFileUrl) {
			URL.revokeObjectURL(sttFileUrl);
		}
		sttFile = null;
		sttFileUrl = null;
		sttTranscript = null;
	}

	async function handleSTTTranscribe() {
		if (!sttFile || isTranscribing) return;

		isTranscribing = true;
		error = null;

		try {
			// Upload the file first
			const uploadResponse = await api.uploadFile('/files/audio', sttFile);

			// Then transcribe
			const result = await studio.transcribeAudio(uploadResponse.full_path, {
				model: selectedSTTModel,
				language: sttLanguage !== 'auto' ? sttLanguage : undefined
			});

			if (result?.result?.transcript) {
				sttTranscript = result.result.transcript;
				lastGeneration = result;
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Transcription failed';
		} finally {
			isTranscribing = false;
		}
	}

	function toggleAudioPlayback() {
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
	}

	function downloadResult() {
		if (!lastGeneration?.result?.url) return;

		const a = document.createElement('a');
		a.href = lastGeneration.result.url;
		a.download = `studio-${lastGeneration.type}-${Date.now()}`;
		document.body.appendChild(a);
		a.click();
		document.body.removeChild(a);
	}

	// Sync with store changes
	$effect(() => {
		selectedImageModel = $imageModel;
	});

	$effect(() => {
		selectedVideoModel = $videoModel;
	});

	$effect(() => {
		selectedTTSModel = $ttsModel;
	});

	$effect(() => {
		selectedTTSVoice = $ttsVoice;
	});

	$effect(() => {
		selectedSTTModel = $sttModel;
	});

	// Update store when model changes
	function handleImageModelChange(e: Event) {
		const select = e.target as HTMLSelectElement;
		selectedImageModel = select.value;
		studio.setImageModel(select.value);
	}

	function handleVideoModelChange(e: Event) {
		const select = e.target as HTMLSelectElement;
		selectedVideoModel = select.value;
		studio.setVideoModel(select.value);
	}

	function handleTTSModelChange(e: Event) {
		const select = e.target as HTMLSelectElement;
		selectedTTSModel = select.value;
		studio.setTTSModel(select.value);
	}

	function handleTTSVoiceChange(e: Event) {
		const select = e.target as HTMLSelectElement;
		selectedTTSVoice = select.value;
		studio.setTTSVoice(select.value);
	}

	function handleSTTModelChange(e: Event) {
		const select = e.target as HTMLSelectElement;
		selectedSTTModel = select.value;
		studio.setSTTModel(select.value);
	}
</script>

{#if mobile}
	<!-- Mobile: No BaseCard wrapper, just the content -->
	<div class="studio-content mobile">
		<!-- Preview Area -->
		<div class="preview-area">
			{#if isGenerating || isTranscribing}
				<div class="preview-loading">
					<Loader2 size={32} class="animate-spin" />
					<span class="loading-text">
						{isTranscribing ? 'Transcribing...' : 'Generating...'}
					</span>
				</div>
			{:else if lastGeneration?.result?.url && lastGeneration.type === 'image'}
				<img src={lastGeneration.result.url} alt="Generated" class="preview-image" />
			{:else if lastGeneration?.result?.url && lastGeneration.type === 'video'}
				<video src={lastGeneration.result.url} controls class="preview-video">
					<track kind="captions" />
				</video>
			{:else if lastGeneration?.result?.url && lastGeneration.type === 'tts'}
				<div class="audio-preview">
					<audio
						bind:this={audioElement}
						src={lastGeneration.result.url}
						onplay={handleAudioPlay}
						onpause={handleAudioPause}
						onended={handleAudioEnded}
						class="hidden"
					></audio>
					<button class="audio-play-btn" onclick={toggleAudioPlayback}>
						{#if isPlaying}
							<Pause size={24} />
						{:else}
							<Play size={24} />
						{/if}
					</button>
					<span class="audio-label">Generated Audio</span>
					<button class="audio-download-btn" onclick={downloadResult} title="Download">
						<Download size={16} />
					</button>
				</div>
			{:else if sttTranscript}
				<div class="transcript-preview">
					<p class="transcript-text">{sttTranscript}</p>
				</div>
			{:else if error}
				<div class="preview-error">
					<span>{error}</span>
				</div>
			{:else}
				<div class="preview-placeholder">
					{#if activeMode === 'image'}
						<Image size={40} strokeWidth={1} />
					{:else if activeMode === 'video'}
						<Film size={40} strokeWidth={1} />
					{:else if activeMode === 'tts'}
						<Volume2 size={40} strokeWidth={1} />
					{:else}
						<Mic size={40} strokeWidth={1} />
					{/if}
					<span>
						{#if activeMode === 'image'}
							Generated image will appear here
						{:else if activeMode === 'video'}
							Generated video will appear here
						{:else if activeMode === 'tts'}
							Generated audio will appear here
						{:else}
							Transcription will appear here
						{/if}
					</span>
				</div>
			{/if}
		</div>

		<!-- Mode Tabs -->
		<div class="mode-tabs">
			<button
				class="mode-tab"
				class:active={activeMode === 'image'}
				onclick={() => setMode('image')}
			>
				<Image size={14} />
				<span>Image</span>
			</button>
			<button
				class="mode-tab"
				class:active={activeMode === 'video'}
				onclick={() => setMode('video')}
			>
				<Film size={14} />
				<span>Video</span>
			</button>
			<button
				class="mode-tab"
				class:active={activeMode === 'tts'}
				onclick={() => setMode('tts')}
			>
				<Volume2 size={14} />
				<span>TTS</span>
			</button>
			<button
				class="mode-tab"
				class:active={activeMode === 'stt'}
				onclick={() => setMode('stt')}
			>
				<Mic size={14} />
				<span>STT</span>
			</button>
		</div>

		<!-- Controls Area -->
		<div class="controls-area">
			{#if activeMode === 'image'}
				<!-- Image Controls -->
				<textarea
					bind:value={imagePrompt}
					placeholder="Describe the image..."
					rows="2"
					class="prompt-input"
					disabled={isGenerating}
				></textarea>

				<div class="controls-row">
					<select
						class="model-select"
						value={selectedImageModel}
						onchange={handleImageModelChange}
						disabled={isGenerating}
					>
						{#each imageModelOptions as option}
							<option value={option.value}>{option.label}</option>
						{/each}
					</select>

					<div class="aspect-buttons">
						{#each availableImageAspectRatios.slice(0, 3) as ratio}
							<button
								class="aspect-btn"
								class:selected={selectedImageAspectRatio === ratio.value}
								onclick={() => (selectedImageAspectRatio = ratio.value)}
								disabled={isGenerating}
								title={ratio.value}
							>
								<div
									class="aspect-icon"
									style:width="{ratio.width}px"
									style:height="{ratio.height}px"
								></div>
							</button>
						{/each}
					</div>
				</div>

				<button
					class="generate-btn"
					onclick={handleImageGenerate}
					disabled={!imagePrompt.trim() || isGenerating}
				>
					{#if isGenerating}
						<Loader2 size={16} class="animate-spin" />
					{:else}
						<Sparkles size={16} />
					{/if}
					<span>{isGenerating ? 'Generating...' : 'Generate'}</span>
				</button>

			{:else if activeMode === 'video'}
				<!-- Video Controls -->
				<textarea
					bind:value={videoPrompt}
					placeholder="Describe the video..."
					rows="2"
					class="prompt-input"
					disabled={isGenerating}
				></textarea>

				<div class="controls-row">
					<select
						class="model-select"
						value={selectedVideoModel}
						onchange={handleVideoModelChange}
						disabled={isGenerating}
					>
						{#each videoModelOptions as option}
							<option value={option.value}>{option.label}</option>
						{/each}
					</select>

					<div class="duration-buttons">
						{#each availableVideoDurations as duration}
							<button
								class="duration-btn"
								class:selected={selectedVideoDuration === duration}
								onclick={() => (selectedVideoDuration = duration)}
								disabled={isGenerating}
							>
								{duration}s
							</button>
						{/each}
					</div>
				</div>

				<button
					class="generate-btn"
					onclick={handleVideoGenerate}
					disabled={!videoPrompt.trim() || isGenerating}
				>
					{#if isGenerating}
						<Loader2 size={16} class="animate-spin" />
					{:else}
						<Sparkles size={16} />
					{/if}
					<span>{isGenerating ? 'Generating...' : 'Generate'}</span>
				</button>

			{:else if activeMode === 'tts'}
				<!-- TTS Controls -->
				<textarea
					bind:value={ttsText}
					placeholder="Enter text to convert to speech..."
					rows="3"
					class="prompt-input"
					disabled={isGenerating}
				></textarea>

				<div class="controls-row">
					<select
						class="model-select small"
						value={selectedTTSModel}
						onchange={handleTTSModelChange}
						disabled={isGenerating}
					>
						{#each ttsModelOptions as option}
							<option value={option.value}>{option.label}</option>
						{/each}
					</select>

					<select
						class="voice-select"
						value={selectedTTSVoice}
						onchange={handleTTSVoiceChange}
						disabled={isGenerating}
					>
						{#each availableTTSVoices as voice}
							<option value={voice.id}>{voice.name}</option>
						{/each}
					</select>
				</div>

				<div class="speed-row">
					<span class="speed-label">Speed: {ttsSpeed.toFixed(1)}x</span>
					<input
						type="range"
						min="0.5"
						max="2"
						step="0.1"
						bind:value={ttsSpeed}
						disabled={isGenerating}
						class="speed-slider"
					/>
				</div>

				<button
					class="generate-btn"
					onclick={handleTTSGenerate}
					disabled={!ttsText.trim() || isGenerating}
				>
					{#if isGenerating}
						<Loader2 size={16} class="animate-spin" />
					{:else}
						<Volume2 size={16} />
					{/if}
					<span>{isGenerating ? 'Generating...' : 'Generate Speech'}</span>
				</button>

			{:else if activeMode === 'stt'}
				<!-- STT Controls -->
				{#if sttFile}
					<div class="file-preview">
						<Mic size={16} />
						<span class="file-name">{sttFile.name}</span>
						<button class="file-remove" onclick={clearSTTFile} disabled={isTranscribing}>
							<X size={14} />
						</button>
					</div>
				{:else}
					<label class="file-upload">
						<Upload size={20} />
						<span>Drop audio file or click to upload</span>
						<input
							type="file"
							accept="audio/*,video/mp4,video/webm"
							onchange={handleSTTFileSelect}
							class="hidden"
						/>
					</label>
				{/if}

				<div class="controls-row">
					<select
						class="model-select"
						value={selectedSTTModel}
						onchange={handleSTTModelChange}
						disabled={isTranscribing}
					>
						{#each sttModelOptions as option}
							<option value={option.value}>{option.label}</option>
						{/each}
					</select>

					<select
						class="language-select"
						bind:value={sttLanguage}
						disabled={isTranscribing}
					>
						{#each STT_LANGUAGES as lang}
							<option value={lang.code}>{lang.name}</option>
						{/each}
					</select>
				</div>

				<button
					class="generate-btn"
					onclick={handleSTTTranscribe}
					disabled={!sttFile || isTranscribing}
				>
					{#if isTranscribing}
						<Loader2 size={16} class="animate-spin" />
					{:else}
						<Mic size={16} />
					{/if}
					<span>{isTranscribing ? 'Transcribing...' : 'Transcribe'}</span>
				</button>
			{/if}
		</div>
	</div>
{:else}
	<!-- Desktop: Full BaseCard with all features -->
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="studio-content">
			<!-- Preview Area -->
			<div class="preview-area">
				{#if isGenerating || isTranscribing}
					<div class="preview-loading">
						<Loader2 size={32} class="animate-spin" />
						<span class="loading-text">
							{isTranscribing ? 'Transcribing...' : 'Generating...'}
						</span>
					</div>
				{:else if lastGeneration?.result?.url && lastGeneration.type === 'image'}
					<img src={lastGeneration.result.url} alt="Generated" class="preview-image" />
				{:else if lastGeneration?.result?.url && lastGeneration.type === 'video'}
					<video src={lastGeneration.result.url} controls class="preview-video">
						<track kind="captions" />
					</video>
				{:else if lastGeneration?.result?.url && lastGeneration.type === 'tts'}
					<div class="audio-preview">
						<audio
							bind:this={audioElement}
							src={lastGeneration.result.url}
							onplay={handleAudioPlay}
							onpause={handleAudioPause}
							onended={handleAudioEnded}
							class="hidden"
						></audio>
						<button class="audio-play-btn" onclick={toggleAudioPlayback}>
							{#if isPlaying}
								<Pause size={24} />
							{:else}
								<Play size={24} />
							{/if}
						</button>
						<span class="audio-label">Generated Audio</span>
						<button class="audio-download-btn" onclick={downloadResult} title="Download">
							<Download size={16} />
						</button>
					</div>
				{:else if sttTranscript}
					<div class="transcript-preview">
						<p class="transcript-text">{sttTranscript}</p>
					</div>
				{:else if error}
					<div class="preview-error">
						<span>{error}</span>
					</div>
				{:else}
					<div class="preview-placeholder">
						{#if activeMode === 'image'}
							<Image size={40} strokeWidth={1} />
						{:else if activeMode === 'video'}
							<Film size={40} strokeWidth={1} />
						{:else if activeMode === 'tts'}
							<Volume2 size={40} strokeWidth={1} />
						{:else}
							<Mic size={40} strokeWidth={1} />
						{/if}
						<span>
							{#if activeMode === 'image'}
								Generated image will appear here
							{:else if activeMode === 'video'}
								Generated video will appear here
							{:else if activeMode === 'tts'}
								Generated audio will appear here
							{:else}
								Transcription will appear here
							{/if}
						</span>
					</div>
				{/if}
			</div>

			<!-- Mode Tabs -->
			<div class="mode-tabs">
				<button
					class="mode-tab"
					class:active={activeMode === 'image'}
					onclick={() => setMode('image')}
				>
					<Image size={14} />
					<span>Image</span>
				</button>
				<button
					class="mode-tab"
					class:active={activeMode === 'video'}
					onclick={() => setMode('video')}
				>
					<Film size={14} />
					<span>Video</span>
				</button>
				<button
					class="mode-tab"
					class:active={activeMode === 'tts'}
					onclick={() => setMode('tts')}
				>
					<Volume2 size={14} />
					<span>TTS</span>
				</button>
				<button
					class="mode-tab"
					class:active={activeMode === 'stt'}
					onclick={() => setMode('stt')}
				>
					<Mic size={14} />
					<span>STT</span>
				</button>
			</div>

			<!-- Controls Area -->
			<div class="controls-area">
				{#if activeMode === 'image'}
					<!-- Image Controls -->
					<textarea
						bind:value={imagePrompt}
						placeholder="Describe the image..."
						rows="2"
						class="prompt-input"
						disabled={isGenerating}
					></textarea>

					<div class="controls-row">
						<select
							class="model-select"
							value={selectedImageModel}
							onchange={handleImageModelChange}
							disabled={isGenerating}
						>
							{#each imageModelOptions as option}
								<option value={option.value}>{option.label}</option>
							{/each}
						</select>

						<div class="aspect-buttons">
							{#each availableImageAspectRatios.slice(0, 3) as ratio}
								<button
									class="aspect-btn"
									class:selected={selectedImageAspectRatio === ratio.value}
									onclick={() => (selectedImageAspectRatio = ratio.value)}
									disabled={isGenerating}
									title={ratio.value}
								>
									<div
										class="aspect-icon"
										style:width="{ratio.width}px"
										style:height="{ratio.height}px"
									></div>
								</button>
							{/each}
						</div>
					</div>

					<button
						class="generate-btn"
						onclick={handleImageGenerate}
						disabled={!imagePrompt.trim() || isGenerating}
					>
						{#if isGenerating}
							<Loader2 size={16} class="animate-spin" />
						{:else}
							<Sparkles size={16} />
						{/if}
						<span>{isGenerating ? 'Generating...' : 'Generate'}</span>
					</button>

				{:else if activeMode === 'video'}
					<!-- Video Controls -->
					<textarea
						bind:value={videoPrompt}
						placeholder="Describe the video..."
						rows="2"
						class="prompt-input"
						disabled={isGenerating}
					></textarea>

					<div class="controls-row">
						<select
							class="model-select"
							value={selectedVideoModel}
							onchange={handleVideoModelChange}
							disabled={isGenerating}
						>
							{#each videoModelOptions as option}
								<option value={option.value}>{option.label}</option>
							{/each}
						</select>

						<div class="duration-buttons">
							{#each availableVideoDurations as duration}
								<button
									class="duration-btn"
									class:selected={selectedVideoDuration === duration}
									onclick={() => (selectedVideoDuration = duration)}
									disabled={isGenerating}
								>
									{duration}s
								</button>
							{/each}
						</div>
					</div>

					<button
						class="generate-btn"
						onclick={handleVideoGenerate}
						disabled={!videoPrompt.trim() || isGenerating}
					>
						{#if isGenerating}
							<Loader2 size={16} class="animate-spin" />
						{:else}
							<Sparkles size={16} />
						{/if}
						<span>{isGenerating ? 'Generating...' : 'Generate'}</span>
					</button>

				{:else if activeMode === 'tts'}
					<!-- TTS Controls -->
					<textarea
						bind:value={ttsText}
						placeholder="Enter text to convert to speech..."
						rows="3"
						class="prompt-input"
						disabled={isGenerating}
					></textarea>

					<div class="controls-row">
						<select
							class="model-select small"
							value={selectedTTSModel}
							onchange={handleTTSModelChange}
							disabled={isGenerating}
						>
							{#each ttsModelOptions as option}
								<option value={option.value}>{option.label}</option>
							{/each}
						</select>

						<select
							class="voice-select"
							value={selectedTTSVoice}
							onchange={handleTTSVoiceChange}
							disabled={isGenerating}
						>
							{#each availableTTSVoices as voice}
								<option value={voice.id}>{voice.name}</option>
							{/each}
						</select>
					</div>

					<div class="speed-row">
						<span class="speed-label">Speed: {ttsSpeed.toFixed(1)}x</span>
						<input
							type="range"
							min="0.5"
							max="2"
							step="0.1"
							bind:value={ttsSpeed}
							disabled={isGenerating}
							class="speed-slider"
						/>
					</div>

					<button
						class="generate-btn"
						onclick={handleTTSGenerate}
						disabled={!ttsText.trim() || isGenerating}
					>
						{#if isGenerating}
							<Loader2 size={16} class="animate-spin" />
						{:else}
							<Volume2 size={16} />
						{/if}
						<span>{isGenerating ? 'Generating...' : 'Generate Speech'}</span>
					</button>

				{:else if activeMode === 'stt'}
					<!-- STT Controls -->
					{#if sttFile}
						<div class="file-preview">
							<Mic size={16} />
							<span class="file-name">{sttFile.name}</span>
							<button class="file-remove" onclick={clearSTTFile} disabled={isTranscribing}>
								<X size={14} />
							</button>
						</div>
					{:else}
						<label class="file-upload">
							<Upload size={20} />
							<span>Drop audio file or click to upload</span>
							<input
								type="file"
								accept="audio/*,video/mp4,video/webm"
								onchange={handleSTTFileSelect}
								class="hidden"
							/>
						</label>
					{/if}

					<div class="controls-row">
						<select
							class="model-select"
							value={selectedSTTModel}
							onchange={handleSTTModelChange}
							disabled={isTranscribing}
						>
							{#each sttModelOptions as option}
								<option value={option.value}>{option.label}</option>
							{/each}
						</select>

						<select
							class="language-select"
							bind:value={sttLanguage}
							disabled={isTranscribing}
						>
							{#each STT_LANGUAGES as lang}
								<option value={lang.code}>{lang.name}</option>
							{/each}
						</select>
					</div>

					<button
						class="generate-btn"
						onclick={handleSTTTranscribe}
						disabled={!sttFile || isTranscribing}
					>
						{#if isTranscribing}
							<Loader2 size={16} class="animate-spin" />
						{:else}
							<Mic size={16} />
						{/if}
						<span>{isTranscribing ? 'Transcribing...' : 'Transcribe'}</span>
					</button>
				{/if}
			</div>
		</div>
	</BaseCard>
{/if}

<style>
	.studio-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
		background: var(--card);
		border-radius: 0 0 12px 12px;
	}

	.studio-content.mobile {
		height: 100%;
		border-radius: 0;
	}

	/* Preview Area */
	.preview-area {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--secondary);
		border-bottom: 1px solid var(--border);
		min-height: 150px;
		overflow: hidden;
		position: relative;
		box-shadow: inset 0 2px 8px oklch(0 0 0 / 0.1);
	}

	:global(.dark) .preview-area {
		background: oklch(0.12 0.01 260);
		box-shadow: inset 0 2px 8px oklch(0 0 0 / 0.2);
	}

	.preview-placeholder {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 8px;
		color: var(--muted-foreground);
		text-align: center;
		padding: 16px;
	}

	.preview-placeholder span {
		font-size: 0.75rem;
	}

	.preview-loading {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 8px;
		color: var(--primary);
	}

	.loading-text {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.preview-image,
	.preview-video {
		max-width: 100%;
		max-height: 100%;
		object-fit: contain;
		border-radius: 4px;
		box-shadow: var(--shadow-m);
	}

	.preview-error {
		color: var(--destructive);
		font-size: 0.75rem;
		padding: 16px;
		text-align: center;
		background: oklch(from var(--destructive) l c h / 0.1);
		border-radius: 8px;
		margin: 16px;
	}

	.audio-preview {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 16px;
		background: var(--secondary);
		border-radius: 12px;
		box-shadow: var(--shadow-s);
	}

	.audio-play-btn {
		width: 48px;
		height: 48px;
		border-radius: 50%;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		transition: all 0.2s;
		box-shadow: var(--shadow-m);
	}

	.audio-play-btn:hover {
		transform: scale(1.05);
		box-shadow: var(--shadow-l);
	}

	.audio-play-btn:active {
		transform: scale(0.98);
	}

	.audio-label {
		font-size: 0.8125rem;
		color: var(--foreground);
		font-weight: 500;
	}

	.audio-download-btn {
		width: 32px;
		height: 32px;
		border-radius: 6px;
		background: var(--muted);
		color: var(--muted-foreground);
		border: 1px solid var(--border);
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		transition: all 0.15s;
	}

	.audio-download-btn:hover {
		background: var(--accent);
		color: var(--foreground);
		border-color: var(--primary);
		box-shadow: var(--shadow-s);
	}

	.transcript-preview {
		padding: 16px;
		overflow-y: auto;
		max-height: 100%;
		background: var(--secondary);
		border-radius: 8px;
		margin: 12px;
		box-shadow: var(--shadow-s);
	}

	.transcript-text {
		font-size: 0.8125rem;
		line-height: 1.6;
		color: var(--foreground);
		white-space: pre-wrap;
	}

	/* Mode Tabs */
	.mode-tabs {
		display: flex;
		border-bottom: 1px solid var(--border);
		background: var(--secondary);
		flex-shrink: 0;
	}

	.mode-tab {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 4px;
		padding: 10px 4px;
		background: transparent;
		border: none;
		color: var(--muted-foreground);
		font-size: 0.6875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s;
		border-bottom: 2px solid transparent;
		position: relative;
	}

	.mode-tab:hover {
		color: var(--foreground);
		background: var(--accent);
	}

	.mode-tab.active {
		color: var(--primary);
		border-bottom-color: var(--primary);
		background: oklch(from var(--primary) l c h / 0.08);
	}

	:global(.light) .mode-tab.active {
		background: oklch(from var(--primary) l c h / 0.1);
	}

	/* Controls Area */
	.controls-area {
		padding: 12px;
		display: flex;
		flex-direction: column;
		gap: 10px;
		flex-shrink: 0;
		background: var(--card);
	}

	.prompt-input {
		width: 100%;
		padding: 10px 12px;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: 8px;
		font-size: 0.8125rem;
		color: var(--foreground);
		resize: none;
		outline: none;
		transition: all 0.2s;
		box-shadow: var(--shadow-s);
	}

	:global(.dark) .prompt-input {
		background: oklch(0.12 0.008 260);
	}

	.prompt-input::placeholder {
		color: var(--muted-foreground);
	}

	.prompt-input:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 2px oklch(from var(--primary) l c h / 0.2);
	}

	.prompt-input:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.controls-row {
		display: flex;
		gap: 8px;
		align-items: center;
	}

	.model-select,
	.voice-select,
	.language-select {
		flex: 1;
		padding: 8px 10px;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: 6px;
		font-size: 0.75rem;
		color: var(--foreground);
		cursor: pointer;
		outline: none;
		transition: all 0.15s;
	}

	:global(.dark) .model-select,
	:global(.dark) .voice-select,
	:global(.dark) .language-select {
		background: oklch(0.12 0.008 260);
	}

	.model-select:hover:not(:disabled),
	.voice-select:hover:not(:disabled),
	.language-select:hover:not(:disabled) {
		border-color: var(--primary);
	}

	.model-select:focus,
	.voice-select:focus,
	.language-select:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 2px oklch(from var(--primary) l c h / 0.15);
	}

	.model-select.small {
		flex: 0.6;
	}

	.model-select:disabled,
	.voice-select:disabled,
	.language-select:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.aspect-buttons,
	.duration-buttons {
		display: flex;
		gap: 4px;
	}

	.aspect-btn,
	.duration-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 6px;
		background: var(--secondary);
		border: 1px solid var(--border);
		border-radius: 6px;
		cursor: pointer;
		transition: all 0.15s;
	}

	.aspect-btn {
		width: 34px;
		height: 34px;
	}

	.duration-btn {
		padding: 6px 10px;
		font-size: 0.75rem;
		color: var(--foreground);
		font-weight: 500;
	}

	.aspect-btn:hover:not(:disabled),
	.duration-btn:hover:not(:disabled) {
		background: var(--accent);
		border-color: var(--muted-foreground);
		box-shadow: var(--shadow-s);
	}

	.aspect-btn.selected,
	.duration-btn.selected {
		border-color: var(--primary);
		background: oklch(from var(--primary) l c h / 0.15);
		box-shadow: 0 0 0 1px var(--primary);
	}

	.aspect-btn:disabled,
	.duration-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.aspect-icon {
		background: var(--muted-foreground);
		border-radius: 2px;
		opacity: 0.6;
	}

	.aspect-btn.selected .aspect-icon {
		background: var(--primary);
		opacity: 1;
	}

	.speed-row {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.speed-label {
		font-size: 0.75rem;
		color: var(--foreground);
		min-width: 70px;
		font-weight: 500;
	}

	.speed-slider {
		flex: 1;
		height: 6px;
		accent-color: var(--primary);
		border-radius: 3px;
	}

	/* File Upload */
	.file-upload {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 20px;
		border: 2px dashed var(--border);
		border-radius: 10px;
		cursor: pointer;
		color: var(--muted-foreground);
		font-size: 0.75rem;
		transition: all 0.2s;
		background: var(--secondary);
	}

	.file-upload:hover {
		border-color: var(--primary);
		background: oklch(from var(--primary) l c h / 0.05);
		color: var(--foreground);
		box-shadow: var(--shadow-s);
	}

	.file-preview {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 12px;
		background: var(--secondary);
		border: 1px solid var(--border);
		border-radius: 8px;
		box-shadow: var(--shadow-s);
	}

	.file-name {
		flex: 1;
		font-size: 0.75rem;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 500;
	}

	.file-remove {
		width: 24px;
		height: 24px;
		border-radius: 6px;
		background: transparent;
		border: 1px solid transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		transition: all 0.15s;
	}

	.file-remove:hover:not(:disabled) {
		background: oklch(from var(--destructive) l c h / 0.15);
		border-color: var(--destructive);
		color: var(--destructive);
	}

	.file-remove:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Generate Button */
	.generate-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 10px 18px;
		background: var(--primary);
		border: none;
		border-radius: 8px;
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--primary-foreground);
		cursor: pointer;
		transition: all 0.2s;
		box-shadow: var(--shadow-m);
	}

	.generate-btn:hover:not(:disabled) {
		transform: translateY(-1px);
		box-shadow: var(--shadow-l);
		filter: brightness(1.05);
	}

	.generate-btn:active:not(:disabled) {
		transform: translateY(0);
		box-shadow: var(--shadow-s);
	}

	.generate-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
		box-shadow: none;
	}

	/* Utilities */
	.hidden {
		display: none;
	}

	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}
</style>

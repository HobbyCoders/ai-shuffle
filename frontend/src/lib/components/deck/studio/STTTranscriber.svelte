<script lang="ts">
	import { Mic, Upload, X, Copy, Check, Languages, Users, Clock, Globe, Zap } from 'lucide-svelte';
	import { ALL_STT_MODELS, getSTTModel, type STTModel } from '$lib/types/ai-models';
	import { studio, sttModel, currentSTTModelInfo } from '$lib/stores/studio';
	import { api } from '$lib/api/client';

	// Props
	interface Props {
		onStartTranscription?: (config: TranscriptionConfig) => void;
	}

	let { onStartTranscription }: Props = $props();

	// Types
	interface TranscriptionConfig {
		audioUrl: string;
		model: string;
		language: string;
		diarization: boolean;
		translate: boolean;
		timestampGranularity: 'word' | 'segment';
	}

	interface TranscriptResult {
		text: string;
		speakers?: { id: string; segments: { start: number; end: number; text: string }[] }[];
	}

	// Common languages for the dropdown
	const COMMON_LANGUAGES = [
		{ code: 'auto', name: 'Auto-detect' },
		{ code: 'en', name: 'English' },
		{ code: 'es', name: 'Spanish' },
		{ code: 'fr', name: 'French' },
		{ code: 'de', name: 'German' },
		{ code: 'it', name: 'Italian' },
		{ code: 'pt', name: 'Portuguese' },
		{ code: 'zh', name: 'Chinese' },
		{ code: 'ja', name: 'Japanese' },
		{ code: 'ko', name: 'Korean' },
		{ code: 'ar', name: 'Arabic' },
		{ code: 'hi', name: 'Hindi' },
		{ code: 'ru', name: 'Russian' },
		{ code: 'nl', name: 'Dutch' },
		{ code: 'pl', name: 'Polish' },
		{ code: 'tr', name: 'Turkish' },
		{ code: 'vi', name: 'Vietnamese' },
		{ code: 'th', name: 'Thai' },
		{ code: 'id', name: 'Indonesian' },
		{ code: 'sv', name: 'Swedish' }
	];

	// Group models by provider
	const PROVIDER_GROUPS = [
		{
			id: 'google-stt',
			name: 'Google Cloud STT',
			models: ALL_STT_MODELS.filter(m => m.provider === 'google-stt')
		},
		{
			id: 'openai-stt',
			name: 'OpenAI Whisper',
			models: ALL_STT_MODELS.filter(m => m.provider === 'openai-stt')
		}
	];

	// State
	let audioFile: File | null = $state(null);
	let audioUrl: string | null = $state(null);
	let selectedLanguage = $state('auto');
	let enableDiarization = $state(false);
	let enableTranslation = $state(false);
	let timestampGranularity: 'word' | 'segment' = $state('segment');
	let isTranscribing = $state(false);
	let isUploading = $state(false);
	let transcriptResult: TranscriptResult | null = $state(null);
	let error: string | null = $state(null);
	let copied = $state(false);
	let isDragOver = $state(false);
	let fileInputElement: HTMLInputElement | null = $state(null);

	// Derived
	let currentModel = $derived($currentSTTModelInfo);
	let canTranscribe = $derived(audioFile !== null && !isTranscribing && !isUploading);
	let supportedFormats = $derived(currentModel?.inputFormats.join(', ').toUpperCase() || 'WAV, MP3, FLAC');
	let maxFileSizeMB = $derived(currentModel?.maxFileSizeMB || 25);
	let showDiarization = $derived(currentModel?.capabilities.diarization ?? false);
	let showTranslation = $derived(currentModel?.capabilities.translation ?? false);
	let showTimestamps = $derived(currentModel?.capabilities.timestamps ?? false);

	// Handlers
	function handleModelChange(modelId: string) {
		studio.setSTTModel(modelId);
		// Reset options that may not be supported
		const model = getSTTModel(modelId);
		if (model && !model.capabilities.diarization) {
			enableDiarization = false;
		}
		if (model && !model.capabilities.translation) {
			enableTranslation = false;
		}
	}

	function handleLanguageChange(event: Event) {
		const select = event.target as HTMLSelectElement;
		selectedLanguage = select.value;
	}

	function handleTimestampChange(event: Event) {
		const select = event.target as HTMLSelectElement;
		timestampGranularity = select.value as 'word' | 'segment';
	}

	function handleFileSelect(event: Event) {
		const input = event.target as HTMLInputElement;
		if (!input.files?.length) return;
		processFile(input.files[0]);
		input.value = '';
	}

	function handleDrop(event: DragEvent) {
		event.preventDefault();
		isDragOver = false;

		const file = event.dataTransfer?.files[0];
		if (file) {
			processFile(file);
		}
	}

	function handleDragOver(event: DragEvent) {
		event.preventDefault();
		isDragOver = true;
	}

	function handleDragLeave(event: DragEvent) {
		event.preventDefault();
		isDragOver = false;
	}

	function processFile(file: File) {
		// Validate file type
		const validTypes = ['audio/wav', 'audio/mp3', 'audio/mpeg', 'audio/flac', 'audio/ogg', 'audio/webm', 'audio/m4a', 'audio/aac', 'audio/x-m4a', 'audio/mp4', 'video/mp4', 'video/webm'];
		const extension = file.name.split('.').pop()?.toLowerCase();
		const validExtensions = ['wav', 'mp3', 'flac', 'ogg', 'webm', 'm4a', 'aac', 'mp4', 'mpeg', 'mpga'];

		if (!validTypes.includes(file.type) && !validExtensions.includes(extension || '')) {
			error = `Unsupported file type. Please use: ${supportedFormats}`;
			return;
		}

		// Validate file size
		const fileSizeMB = file.size / (1024 * 1024);
		if (fileSizeMB > maxFileSizeMB) {
			error = `File too large. Maximum size is ${maxFileSizeMB}MB`;
			return;
		}

		// Clear previous state
		if (audioUrl) {
			URL.revokeObjectURL(audioUrl);
		}
		error = null;
		transcriptResult = null;

		audioFile = file;
		audioUrl = URL.createObjectURL(file);
	}

	function clearAudioFile() {
		if (audioUrl) {
			URL.revokeObjectURL(audioUrl);
		}
		audioFile = null;
		audioUrl = null;
		transcriptResult = null;
		error = null;
	}

	function triggerFileUpload() {
		fileInputElement?.click();
	}

	async function handleTranscribe() {
		if (!canTranscribe || !audioFile) return;

		isUploading = true;
		error = null;

		try {
			// First upload the audio file
			const uploadResponse = await api.uploadFile('/files/audio', audioFile);
			const uploadedUrl = uploadResponse.full_path;

			isUploading = false;
			isTranscribing = true;

			// Then call transcription
			const result = await studio.transcribeAudio(uploadedUrl, {
				model: $sttModel,
				language: selectedLanguage !== 'auto' ? selectedLanguage : undefined,
				diarization: showDiarization ? enableDiarization : undefined,
				translate: showTranslation ? enableTranslation : undefined,
				timestampGranularity: showTimestamps ? timestampGranularity : undefined
			});

			if (result?.result) {
				transcriptResult = {
					text: result.result.transcript || '',
					speakers: result.result.speakers
				};
			}

			onStartTranscription?.({
				audioUrl: uploadedUrl,
				model: $sttModel,
				language: selectedLanguage,
				diarization: enableDiarization,
				translate: enableTranslation,
				timestampGranularity
			});
		} catch (e) {
			error = e instanceof Error ? e.message : 'Transcription failed';
		} finally {
			isUploading = false;
			isTranscribing = false;
		}
	}

	async function copyToClipboard() {
		if (!transcriptResult?.text) return;

		try {
			await navigator.clipboard.writeText(transcriptResult.text);
			copied = true;
			setTimeout(() => {
				copied = false;
			}, 2000);
		} catch (e) {
			console.error('Failed to copy:', e);
		}
	}

	function formatSpeakerLabel(index: number): string {
		return `Speaker ${String.fromCharCode(65 + index)}`;
	}

	function getCapabilityBadges(model: STTModel): { label: string; icon: typeof Zap }[] {
		const badges: { label: string; icon: typeof Zap }[] = [];
		if (model.capabilities.realtime) badges.push({ label: 'Realtime', icon: Zap });
		if (model.capabilities.diarization) badges.push({ label: 'Diarization', icon: Users });
		if (model.capabilities.translation) badges.push({ label: 'Translation', icon: Globe });
		if (model.capabilities.timestamps) badges.push({ label: 'Timestamps', icon: Clock });
		if (model.capabilities.customVocabulary) badges.push({ label: 'Custom Vocabulary', icon: Languages });
		return badges;
	}
</script>

<div class="p-4 space-y-6">
	<!-- Audio File Upload -->
	<div>
		<label class="block text-sm font-medium text-foreground mb-2">
			Audio File
		</label>

		{#if audioFile && audioUrl}
			<!-- File Preview -->
			<div class="relative bg-muted rounded-xl p-4 border border-border">
				<div class="flex items-center gap-3">
					<div class="shrink-0 w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center">
						<Mic class="w-5 h-5 text-primary" />
					</div>
					<div class="flex-1 min-w-0">
						<div class="text-sm font-medium text-foreground truncate">{audioFile.name}</div>
						<div class="text-xs text-muted-foreground">
							{(audioFile.size / (1024 * 1024)).toFixed(2)} MB
						</div>
					</div>
					<button
						type="button"
						onclick={clearAudioFile}
						disabled={isTranscribing || isUploading}
						class="shrink-0 w-8 h-8 rounded-lg hover:bg-destructive/10 flex items-center justify-center text-muted-foreground hover:text-destructive transition-colors disabled:opacity-50"
						aria-label="Remove audio file"
					>
						<X class="w-4 h-4" />
					</button>
				</div>

				<!-- Audio Player -->
				<audio
					src={audioUrl}
					controls
					class="w-full mt-3 h-10"
				></audio>
			</div>
		{:else}
			<!-- Drop Zone -->
			<button
				type="button"
				onclick={triggerFileUpload}
				ondrop={handleDrop}
				ondragover={handleDragOver}
				ondragleave={handleDragLeave}
				class="w-full min-h-[120px] rounded-xl border-2 border-dashed transition-colors flex flex-col items-center justify-center gap-2 cursor-pointer {isDragOver ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/50 hover:bg-muted'}"
				aria-label="Upload audio file"
			>
				<div class="w-12 h-12 rounded-full bg-muted flex items-center justify-center {isDragOver ? 'bg-primary/10' : ''}">
					<Upload class="w-6 h-6 text-muted-foreground {isDragOver ? 'text-primary' : ''}" />
				</div>
				<div class="text-sm text-foreground">
					Drop audio file here or click to upload
				</div>
				<div class="text-xs text-muted-foreground">
					Supports: {supportedFormats}
				</div>
				<div class="text-xs text-muted-foreground">
					Max file size: {maxFileSizeMB}MB
				</div>
			</button>

			<input
				bind:this={fileInputElement}
				type="file"
				accept="audio/*,video/mp4,video/webm"
				onchange={handleFileSelect}
				class="hidden"
				aria-hidden="true"
			/>
		{/if}
	</div>

	<!-- Model Selector -->
	<div>
		<label class="block text-xs text-muted-foreground mb-2">Model</label>
		<div class="space-y-4">
			{#each PROVIDER_GROUPS as group}
				<div>
					<div class="text-xs font-medium text-muted-foreground mb-2">{group.name}</div>
					<div class="space-y-2">
						{#each group.models as model}
							{@const badges = getCapabilityBadges(model)}
							<button
								type="button"
								onclick={() => handleModelChange(model.id)}
								disabled={isTranscribing || isUploading}
								class="w-full flex flex-col gap-2 p-3 rounded-lg border transition-colors text-left {$sttModel === model.id ? 'border-primary bg-primary/5' : 'border-border hover:border-muted-foreground/50 hover:bg-muted'}"
								aria-pressed={$sttModel === model.id}
							>
								<div class="flex items-start gap-3">
									<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {$sttModel === model.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
										<Mic class="w-4 h-4" />
									</div>
									<div class="flex-1 min-w-0">
										<div class="text-sm font-medium text-foreground">{model.displayName}</div>
										<div class="text-xs text-muted-foreground">{model.description}</div>
									</div>
									{#if $sttModel === model.id}
										<div class="shrink-0 w-2 h-2 rounded-full bg-primary mt-2"></div>
									{/if}
								</div>

								<!-- Capability Badges -->
								{#if badges.length > 0}
									<div class="flex flex-wrap gap-1.5 ml-11">
										{#each badges as badge}
											{@const BadgeIcon = badge.icon}
											<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs {$sttModel === model.id ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'}">
												<BadgeIcon class="w-3 h-3" />
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

	<!-- Language Selector -->
	<div>
		<label for="stt-language" class="block text-xs text-muted-foreground mb-2">
			Language
		</label>
		<div class="relative">
			<Languages class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
			<select
				id="stt-language"
				value={selectedLanguage}
				onchange={handleLanguageChange}
				disabled={isTranscribing || isUploading}
				class="w-full bg-muted border border-border rounded-lg pl-10 pr-4 py-2.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 appearance-none cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
			>
				{#each COMMON_LANGUAGES as lang}
					<option value={lang.code}>{lang.name}</option>
				{/each}
			</select>
			<div class="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none">
				<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
				</svg>
			</div>
		</div>
	</div>

	<!-- Speaker Diarization Toggle -->
	{#if showDiarization}
		<div>
			<button
				type="button"
				onclick={() => enableDiarization = !enableDiarization}
				disabled={isTranscribing || isUploading}
				class="flex items-center gap-3 px-4 py-3 rounded-lg border transition-colors w-full {enableDiarization ? 'border-primary bg-primary/5' : 'border-border hover:bg-muted'} disabled:opacity-50 disabled:cursor-not-allowed"
				aria-pressed={enableDiarization}
			>
				<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {enableDiarization ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
					<Users class="w-4 h-4" />
				</div>
				<div class="flex-1 text-left">
					<div class="text-sm font-medium text-foreground">
						Enable Speaker Diarization
					</div>
					<div class="text-xs text-muted-foreground">
						Identify and label different speakers in the audio
					</div>
				</div>
				<div class="shrink-0 w-10 h-6 rounded-full transition-colors {enableDiarization ? 'bg-primary' : 'bg-muted-foreground/30'}">
					<div class="w-5 h-5 mt-0.5 rounded-full bg-white shadow-sm transition-transform {enableDiarization ? 'translate-x-4' : 'translate-x-0.5'}"></div>
				</div>
			</button>
		</div>
	{/if}

	<!-- Translation Toggle (Whisper only) -->
	{#if showTranslation}
		<div>
			<button
				type="button"
				onclick={() => enableTranslation = !enableTranslation}
				disabled={isTranscribing || isUploading}
				class="flex items-center gap-3 px-4 py-3 rounded-lg border transition-colors w-full {enableTranslation ? 'border-primary bg-primary/5' : 'border-border hover:bg-muted'} disabled:opacity-50 disabled:cursor-not-allowed"
				aria-pressed={enableTranslation}
			>
				<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {enableTranslation ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
					<Globe class="w-4 h-4" />
				</div>
				<div class="flex-1 text-left">
					<div class="text-sm font-medium text-foreground">
						Translate to English
					</div>
					<div class="text-xs text-muted-foreground">
						Translate non-English audio to English text
					</div>
				</div>
				<div class="shrink-0 w-10 h-6 rounded-full transition-colors {enableTranslation ? 'bg-primary' : 'bg-muted-foreground/30'}">
					<div class="w-5 h-5 mt-0.5 rounded-full bg-white shadow-sm transition-transform {enableTranslation ? 'translate-x-4' : 'translate-x-0.5'}"></div>
				</div>
			</button>
		</div>
	{/if}

	<!-- Timestamp Granularity -->
	{#if showTimestamps}
		<div>
			<label for="timestamp-granularity" class="block text-xs text-muted-foreground mb-2">
				Timestamp Granularity
			</label>
			<div class="relative">
				<Clock class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
				<select
					id="timestamp-granularity"
					value={timestampGranularity}
					onchange={handleTimestampChange}
					disabled={isTranscribing || isUploading}
					class="w-full bg-muted border border-border rounded-lg pl-10 pr-4 py-2.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 appearance-none cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
				>
					<option value="segment">Segment (sentences/phrases)</option>
					<option value="word">Word-level</option>
				</select>
				<div class="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none">
					<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</div>
			</div>
		</div>
	{/if}

	<!-- Error Display -->
	{#if error}
		<div class="bg-destructive/10 text-destructive text-sm px-4 py-3 rounded-lg">
			{error}
		</div>
	{/if}

	<!-- Transcribe Button -->
	<div class="pt-4 border-t border-border">
		<button
			type="button"
			onclick={handleTranscribe}
			disabled={!canTranscribe}
			class="w-full px-6 py-3 text-sm font-medium bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
		>
			{#if isUploading}
				<div class="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin"></div>
				Uploading...
			{:else if isTranscribing}
				<div class="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin"></div>
				Transcribing...
			{:else}
				<Mic class="w-4 h-4" />
				Transcribe
			{/if}
		</button>
	</div>

	<!-- Transcript Result -->
	{#if transcriptResult}
		<div class="border-t border-border pt-6">
			<div class="flex items-center justify-between mb-3">
				<label class="text-sm font-medium text-foreground">Transcript</label>
				<button
					type="button"
					onclick={copyToClipboard}
					class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-muted hover:bg-muted/80 text-foreground transition-colors"
					aria-label="Copy transcript to clipboard"
				>
					{#if copied}
						<Check class="w-3.5 h-3.5 text-green-500" />
						Copied!
					{:else}
						<Copy class="w-3.5 h-3.5" />
						Copy
					{/if}
				</button>
			</div>

			<div class="bg-muted rounded-xl p-4 max-h-[400px] overflow-y-auto">
				{#if transcriptResult.speakers && transcriptResult.speakers.length > 0 && enableDiarization}
					<!-- Diarized View -->
					<div class="space-y-3">
						{#each transcriptResult.speakers as speaker, index}
							{#each speaker.segments as segment}
								<div class="flex gap-3">
									<div class="shrink-0">
										<span class="inline-flex items-center justify-center w-8 h-8 rounded-full bg-primary/10 text-primary text-xs font-medium">
											{String.fromCharCode(65 + index)}
										</span>
									</div>
									<div class="flex-1">
										<div class="text-xs text-muted-foreground mb-1">
											{formatSpeakerLabel(index)} ({segment.start.toFixed(1)}s - {segment.end.toFixed(1)}s)
										</div>
										<div class="text-sm text-foreground">{segment.text}</div>
									</div>
								</div>
							{/each}
						{/each}
					</div>
				{:else}
					<!-- Plain Text View -->
					<div class="text-sm text-foreground whitespace-pre-wrap">
						{transcriptResult.text}
					</div>
				{/if}
			</div>
		</div>
	{/if}
</div>

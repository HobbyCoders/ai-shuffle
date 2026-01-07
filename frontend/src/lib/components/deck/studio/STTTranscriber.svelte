<script lang="ts">
	import { Mic, Upload, X, Copy, Check, Languages, Users, Clock, Globe, FileAudio, Download } from 'lucide-svelte';
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
			name: 'Google Cloud',
			models: ALL_STT_MODELS.filter(m => m.provider === 'google-stt')
		},
		{
			id: 'openai-stt',
			name: 'Whisper',
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
	let supportedFormats = $derived(currentModel?.inputFormats.join(', ').toUpperCase() || 'MP3, WAV, M4A, FLAC');
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

	function downloadTxt() {
		if (!transcriptResult?.text) return;
		const blob = new Blob([transcriptResult.text], { type: 'text/plain' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = `transcript-${Date.now()}.txt`;
		document.body.appendChild(a);
		a.click();
		document.body.removeChild(a);
		URL.revokeObjectURL(url);
	}

	function downloadSrt() {
		if (!transcriptResult?.speakers) return;
		// Generate SRT format from speakers
		let srt = '';
		let index = 1;
		transcriptResult.speakers.forEach((speaker) => {
			speaker.segments.forEach((segment) => {
				srt += `${index}\n`;
				srt += `${formatSrtTime(segment.start)} --> ${formatSrtTime(segment.end)}\n`;
				srt += `${segment.text}\n\n`;
				index++;
			});
		});
		const blob = new Blob([srt], { type: 'text/plain' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = `transcript-${Date.now()}.srt`;
		document.body.appendChild(a);
		a.click();
		document.body.removeChild(a);
		URL.revokeObjectURL(url);
	}

	function formatSrtTime(seconds: number): string {
		const hours = Math.floor(seconds / 3600);
		const minutes = Math.floor((seconds % 3600) / 60);
		const secs = Math.floor(seconds % 60);
		const ms = Math.floor((seconds % 1) * 1000);
		return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')},${ms.toString().padStart(3, '0')}`;
	}

	function formatSpeakerLabel(index: number): string {
		return `Speaker ${String.fromCharCode(65 + index)}`;
	}

	function getSpeakerColor(index: number): string {
		const colors = [
			'var(--primary)',
			'oklch(0.65 0.15 240)', // blue
			'oklch(0.65 0.18 145)', // green
			'oklch(0.75 0.15 85)',  // yellow
			'oklch(0.65 0.15 300)', // purple
			'oklch(0.70 0.18 350)'  // pink
		];
		return colors[index % colors.length];
	}
</script>

<div class="flex flex-col h-full overflow-hidden">
	<!-- Audio Input Section - 35% -->
	<div class="h-[35%] border-b border-border/50 p-6 bg-gradient-to-b from-card via-card to-background/50">
		<div class="text-xs text-muted-foreground mb-4 uppercase tracking-wider font-medium">Audio Input</div>

		{#if audioFile && audioUrl}
			<!-- File Preview with Waveform -->
			<div class="space-y-4">
				<!-- Waveform Display -->
				<div class="h-16 flex items-center justify-center gap-1 px-4 rounded-lg bg-card/50 backdrop-blur-sm border border-border/30">
					{#each Array(48) as _, i}
						<div
							class="w-1 bg-primary/40 rounded-full"
							style="height: {20 + Math.sin(i * 0.5) * 30 + Math.random() * 20}%;"
						></div>
					{/each}
				</div>

				<!-- File Info -->
				<div class="relative bg-card rounded-xl p-4 border border-border/50">
					<div class="flex items-center gap-3 mb-3">
						<div class="shrink-0 w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center">
							<FileAudio class="w-5 h-5 text-primary" />
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
						class="w-full h-9 rounded-lg"
					></audio>
				</div>
			</div>
		{:else}
			<!-- Drop Zone -->
			<button
				type="button"
				onclick={triggerFileUpload}
				ondrop={handleDrop}
				ondragover={handleDragOver}
				ondragleave={handleDragLeave}
				class="w-full h-full min-h-[160px] rounded-xl border-2 border-dashed transition-all flex flex-col items-center justify-center gap-3 cursor-pointer {isDragOver ? 'border-primary bg-primary/5 scale-[1.02]' : 'border-border/50 hover:border-primary/50 hover:bg-card/50'}"
				aria-label="Upload audio file"
			>
				<div class="w-16 h-16 rounded-full flex items-center justify-center {isDragOver ? 'bg-primary/10 animate-pulse' : 'bg-muted'} transition-all">
					<Upload class="w-8 h-8 {isDragOver ? 'text-primary' : 'text-muted-foreground'} transition-colors" />
				</div>
				<div class="text-center">
					<div class="text-sm font-medium text-foreground mb-1">
						Drop audio file here or click to browse
					</div>
					<div class="text-xs text-muted-foreground">
						Supports: {supportedFormats} (Max {maxFileSizeMB}MB)
					</div>
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

	<!-- Transcript Output Section - 40% -->
	{#if transcriptResult}
		<div class="h-[40%] border-b border-border/50 p-6 bg-gradient-to-b from-background/50 via-card to-card/50">
			<div class="flex items-center justify-between mb-4">
				<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium">Transcript</div>
				<div class="flex gap-2">
					<button
						type="button"
						onclick={copyToClipboard}
						class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-card border border-border/50 hover:border-primary/50 text-foreground transition-all"
						aria-label="Copy transcript to clipboard"
					>
						{#if copied}
							<Check class="w-3.5 h-3.5 text-green-500" />
							Copied
						{:else}
							<Copy class="w-3.5 h-3.5" />
							Copy
						{/if}
					</button>
					<button
						type="button"
						onclick={downloadTxt}
						class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-card border border-border/50 hover:border-primary/50 text-foreground transition-all"
						aria-label="Download as TXT"
					>
						<Download class="w-3.5 h-3.5" />
						TXT
					</button>
					{#if transcriptResult.speakers && transcriptResult.speakers.length > 0}
						<button
							type="button"
							onclick={downloadSrt}
							class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-card border border-border/50 hover:border-primary/50 text-foreground transition-all"
							aria-label="Download as SRT"
						>
							<Download class="w-3.5 h-3.5" />
							SRT
						</button>
					{/if}
				</div>
			</div>

			<div class="bg-card/50 backdrop-blur-sm rounded-xl p-4 h-[calc(100%-3rem)] overflow-y-auto border border-border/30 custom-scrollbar">
				{#if transcriptResult.speakers && transcriptResult.speakers.length > 0 && enableDiarization}
					<!-- Diarized View with Speaker Labels -->
					<div class="space-y-4">
						{#each transcriptResult.speakers as speaker, index}
							{#each speaker.segments as segment}
								<div class="flex gap-3 group">
									<div class="shrink-0">
										<div class="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold bg-primary/10" style="color: {getSpeakerColor(index)}">
											{String.fromCharCode(65 + index)}
										</div>
									</div>
									<div class="flex-1">
										<div class="text-[10px] text-muted-foreground mb-1 font-mono">
											{formatSpeakerLabel(index)} • {segment.start.toFixed(1)}s - {segment.end.toFixed(1)}s
										</div>
										<div class="text-sm text-foreground leading-relaxed">{segment.text}</div>
									</div>
								</div>
							{/each}
						{/each}
					</div>
				{:else}
					<!-- Plain Text View -->
					<div class="text-sm text-foreground whitespace-pre-wrap leading-relaxed">
						{transcriptResult.text}
					</div>
				{/if}
			</div>
		</div>
	{/if}

	<!-- Options Section - 25% -->
	<div class="flex-1 overflow-y-auto p-6 space-y-5 custom-scrollbar">
		<!-- Provider Selection -->
		<div>
			<label class="block text-xs text-muted-foreground mb-3 uppercase tracking-wider font-medium">
				Provider
			</label>
			<div class="flex gap-2">
				{#each PROVIDER_GROUPS as group}
					{#if group.models.length > 0}
						{@const model = group.models[0]}
						<button
							type="button"
							onclick={() => handleModelChange(model.id)}
							disabled={isTranscribing || isUploading}
							class="flex-1 flex items-center gap-2 p-3 rounded-lg border transition-all {$sttModel === model.id ? 'border-primary bg-primary/5' : 'border-border/50 hover:border-primary/30 hover:bg-card/50'} disabled:opacity-50"
						>
							<div class="w-8 h-8 rounded-lg flex items-center justify-center {$sttModel === model.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
								<Mic class="w-4 h-4" />
							</div>
							<div class="flex-1 text-left">
								<div class="text-xs font-medium text-foreground">{group.name}</div>
							</div>
						</button>
					{/if}
				{/each}
			</div>
		</div>

		<!-- Language Selector -->
		<div>
			<label for="stt-language" class="block text-xs text-muted-foreground mb-2 uppercase tracking-wider font-medium">
				Language
			</label>
			<div class="relative">
				<Languages class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
				<select
					id="stt-language"
					value={selectedLanguage}
					onchange={handleLanguageChange}
					disabled={isTranscribing || isUploading}
					class="w-full bg-card border border-border/50 rounded-lg pl-10 pr-4 py-2.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 appearance-none cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
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

		<!-- Options -->
		<div class="space-y-3">
			<div class="text-xs text-muted-foreground uppercase tracking-wider font-medium">Options</div>

			<!-- Speaker Diarization Toggle -->
			{#if showDiarization}
				<label class="flex items-center gap-3 px-4 py-3 rounded-lg border transition-all cursor-pointer {enableDiarization ? 'border-primary bg-primary/5' : 'border-border/50 hover:bg-card/50'}">
					<input
						type="checkbox"
						bind:checked={enableDiarization}
						disabled={isTranscribing || isUploading}
						class="sr-only"
					/>
					<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {enableDiarization ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
						<Users class="w-4 h-4" />
					</div>
					<div class="flex-1 text-left">
						<div class="text-xs font-medium text-foreground">
							Speaker Diarization
						</div>
						<div class="text-[10px] text-muted-foreground">
							Identify different speakers
						</div>
					</div>
					<div class="shrink-0 w-9 h-5 rounded-full transition-colors {enableDiarization ? 'bg-primary' : 'bg-muted-foreground/30'}">
						<div class="w-4 h-4 mt-0.5 rounded-full bg-white shadow-sm transition-transform {enableDiarization ? 'translate-x-4' : 'translate-x-0.5'}"></div>
					</div>
				</label>
			{/if}

			<!-- Translation Toggle -->
			{#if showTranslation}
				<label class="flex items-center gap-3 px-4 py-3 rounded-lg border transition-all cursor-pointer {enableTranslation ? 'border-primary bg-primary/5' : 'border-border/50 hover:bg-card/50'}">
					<input
						type="checkbox"
						bind:checked={enableTranslation}
						disabled={isTranscribing || isUploading}
						class="sr-only"
					/>
					<div class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center {enableTranslation ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
						<Globe class="w-4 h-4" />
					</div>
					<div class="flex-1 text-left">
						<div class="text-xs font-medium text-foreground">
							Translate to English
						</div>
						<div class="text-[10px] text-muted-foreground">
							Translate non-English audio
						</div>
					</div>
					<div class="shrink-0 w-9 h-5 rounded-full transition-colors {enableTranslation ? 'bg-primary' : 'bg-muted-foreground/30'}">
						<div class="w-4 h-4 mt-0.5 rounded-full bg-white shadow-sm transition-transform {enableTranslation ? 'translate-x-4' : 'translate-x-0.5'}"></div>
					</div>
				</label>
			{/if}

			<!-- Timestamp Granularity -->
			{#if showTimestamps}
				<div>
					<label for="timestamp-granularity" class="block text-xs text-muted-foreground mb-2">
						Timestamps
					</label>
					<div class="relative">
						<Clock class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
						<select
							id="timestamp-granularity"
							value={timestampGranularity}
							onchange={handleTimestampChange}
							disabled={isTranscribing || isUploading}
							class="w-full bg-card border border-border/50 rounded-lg pl-10 pr-4 py-2.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 appearance-none cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
						>
							<option value="segment">Segment Level</option>
							<option value="word">Word Level</option>
						</select>
						<div class="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none">
							<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
							</svg>
						</div>
					</div>
				</div>
			{/if}
		</div>

		<!-- Error Display -->
		{#if error}
			<div class="bg-destructive/10 text-destructive text-xs px-4 py-3 rounded-lg border border-destructive/20">
				{error}
			</div>
		{/if}

		<!-- Transcribe Button -->
		<button
			type="button"
			onclick={handleTranscribe}
			disabled={!canTranscribe}
			class="w-full px-6 py-4 text-sm font-medium bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2 shadow-lg shadow-primary/25"
		>
			{#if isUploading}
				<div class="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin"></div>
				Uploading...
			{:else if isTranscribing}
				<div class="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin"></div>
				Transcribing...
			{:else}
				<Mic class="w-5 h-5" />
				Transcribe
			{/if}
		</button>
	</div>
</div>


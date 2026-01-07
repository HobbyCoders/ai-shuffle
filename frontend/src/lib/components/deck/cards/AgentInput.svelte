<script lang="ts">
	/**
	 * AgentInput - Input island for launching background agents
	 *
	 * Minimal input component for typing prompts to launch background agents.
	 * Transplanted from ChatInput but without:
	 * - Context bar (profile/project display)
	 * - Settings button
	 * - Context percentage display
	 *
	 * Includes:
	 * - Textarea with auto-resize
	 * - File attachment button
	 * - Voice recording button
	 * - Send button
	 */
	import { tick } from 'svelte';
	import { api, type FileUploadResponse } from '$lib/api/client';

	interface Props {
		projectId?: string | null;
		disabled?: boolean;
		placeholder?: string;
		onSubmit?: (prompt: string, files: FileUploadResponse[]) => void;
	}

	let {
		projectId = null,
		disabled = false,
		placeholder = 'Describe the task for the background agent...',
		onSubmit
	}: Props = $props();

	// Input state
	let inputValue = $state('');
	let uploadedFiles = $state<FileUploadResponse[]>([]);
	let textareaRef = $state<HTMLTextAreaElement | null>(null);
	let fileInputRef = $state<HTMLInputElement | null>(null);
	let isUploading = $state(false);

	// Voice recording state
	let isRecording = $state(false);
	let isTranscribing = $state(false);
	let mediaRecorder = $state<MediaRecorder | null>(null);
	let audioChunks = $state<Blob[]>([]);
	let recordingError = $state('');

	// Handle form submission
	async function handleSubmit() {
		if (!inputValue.trim() || disabled || isUploading) return;

		// Append file references to the end of the message
		let finalPrompt = inputValue;
		if (uploadedFiles.length > 0) {
			const fileRefs = uploadedFiles.map(f => `@${f.path}`).join(' ');
			finalPrompt = inputValue.trim() + '\n\n' + fileRefs;
		}

		const files = [...uploadedFiles];
		inputValue = '';
		uploadedFiles = [];

		// Reset textarea height
		if (textareaRef) {
			textareaRef.style.height = '';
		}

		onSubmit?.(finalPrompt, files);
	}

	// Handle keydown for textarea
	function handleKeyDown(e: KeyboardEvent) {
		// Check for mobile device
		const isTouchDevice = window.matchMedia('(pointer: coarse)').matches;
		const hasNoFinePointer = !window.matchMedia('(any-pointer: fine)').matches;
		const isMobile = isTouchDevice || (hasNoFinePointer && ('ontouchstart' in window));

		// On desktop, Enter sends; on mobile, let Enter create newlines
		if (e.key === 'Enter' && !e.shiftKey && !isMobile) {
			e.preventDefault();
			handleSubmit();
		}
	}

	// File upload
	function triggerFileUpload() {
		if (!projectId) {
			alert('Please select a project first to upload files.');
			return;
		}
		fileInputRef?.click();
	}

	async function handleFileUpload(event: Event) {
		const input = event.target as HTMLInputElement;
		const files = input.files;
		if (!files || files.length === 0 || !projectId) return;

		isUploading = true;
		try {
			for (const file of Array.from(files)) {
				const result = await api.uploadFile(`/projects/${projectId}/upload`, file);
				uploadedFiles = [...uploadedFiles, result];
			}
		} catch (error: unknown) {
			console.error('Upload failed:', error);
			const err = error as { detail?: string };
			alert(`Upload failed: ${err.detail || 'Unknown error'}`);
		} finally {
			isUploading = false;
			input.value = '';
		}
	}

	function removeUploadedFile(index: number) {
		uploadedFiles = uploadedFiles.filter((_, i) => i !== index);
	}

	// Voice recording
	async function startRecording() {
		recordingError = '';
		try {
			const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
			const recorder = new MediaRecorder(stream, {
				mimeType: MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : 'audio/mp4'
			});
			audioChunks = [];

			recorder.ondataavailable = (event) => {
				if (event.data.size > 0) {
					audioChunks = [...audioChunks, event.data];
				}
			};

			recorder.onstop = async () => {
				stream.getTracks().forEach(track => track.stop());

				if (audioChunks.length === 0) {
					recordingError = 'No audio recorded';
					return;
				}

				const audioBlob = new Blob(audioChunks, { type: recorder.mimeType });
				await transcribeAudio(audioBlob);
			};

			recorder.start();
			mediaRecorder = recorder;
			isRecording = true;
		} catch (error: unknown) {
			console.error('Failed to start recording:', error);
			const err = error as { name?: string; message?: string };
			if (err.name === 'NotAllowedError') {
				recordingError = 'Microphone access denied. Please allow microphone access in your browser settings.';
			} else if (err.name === 'NotFoundError') {
				recordingError = 'No microphone found. Please connect a microphone and try again.';
			} else {
				recordingError = `Failed to start recording: ${err.message || 'Unknown error'}`;
			}
		}
	}

	function stopRecording() {
		if (mediaRecorder && mediaRecorder.state !== 'inactive') {
			mediaRecorder.stop();
		}
		isRecording = false;
	}

	async function transcribeAudio(audioBlob: Blob) {
		isTranscribing = true;
		recordingError = '';

		try {
			const formData = new FormData();
			formData.append('file', audioBlob, 'recording.webm');

			const response = await fetch('/api/v1/settings/transcribe', {
				method: 'POST',
				credentials: 'include',
				body: formData
			});

			if (!response.ok) {
				const error = await response.json().catch(() => ({ detail: 'Transcription failed' }));
				throw new Error(error.detail || 'Transcription failed');
			}

			const result = await response.json();
			const transcribedText = result.text?.trim();

			if (transcribedText) {
				inputValue = inputValue ? `${inputValue} ${transcribedText}` : transcribedText;
				await tick();
				textareaRef?.focus();
			}
		} catch (error: unknown) {
			console.error('Transcription failed:', error);
			const err = error as { message?: string };
			recordingError = err.message || 'Failed to transcribe audio';
		} finally {
			isTranscribing = false;
		}
	}

	function toggleRecording() {
		if (isRecording) {
			stopRecording();
		} else {
			startRecording();
		}
	}
</script>

<div class="agent-input-wrapper">
	<!-- Hidden file input -->
	<input
		type="file"
		bind:this={fileInputRef}
		onchange={handleFileUpload}
		class="hidden"
		multiple
		accept="*/*"
	/>

	<!-- Recording Error Message -->
	{#if recordingError}
		<div class="error-banner">
			<span>{recordingError}</span>
			<button type="button" onclick={() => recordingError = ''} class="error-close">&times;</button>
		</div>
	{/if}

	<form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
		<!-- Main Island -->
		<div class="agent-island">
			<!-- Uploaded Files (if any) -->
			{#if uploadedFiles.length > 0}
				<div class="uploaded-files">
					{#each uploadedFiles as file, index}
						<div class="file-chip">
							<svg class="w-3 h-3 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
							</svg>
							<span class="file-name" title={file.path}>{file.filename}</span>
							<button
								type="button"
								onclick={() => removeUploadedFile(index)}
								class="file-remove"
								aria-label="Remove file"
							>
								<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
								</svg>
							</button>
						</div>
					{/each}
				</div>
			{/if}

			<!-- Input Row -->
			<div class="input-row">
				<!-- Textarea -->
				<div class="textarea-wrapper">
					<textarea
						bind:this={textareaRef}
						bind:value={inputValue}
						onkeydown={handleKeyDown}
						{placeholder}
						class="agent-textarea"
						rows="1"
						{disabled}
					></textarea>
				</div>

				<!-- Right side action buttons -->
				<div class="action-buttons">
					<button
						type="button"
						onclick={triggerFileUpload}
						disabled={disabled || isUploading}
						class="action-btn"
						title={isUploading ? 'Uploading...' : projectId ? 'Attach file' : 'Select a project to attach files'}
					>
						{#if isUploading}
							<svg class="w-[18px] h-[18px] animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
								<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
							</svg>
						{:else}
							<svg class="w-[18px] h-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
							</svg>
						{/if}
					</button>

					<button
						type="button"
						onclick={toggleRecording}
						disabled={disabled || isUploading || isTranscribing}
						class="action-btn {isRecording ? 'recording' : ''} {isTranscribing ? 'transcribing' : ''}"
						title={isRecording ? 'Stop recording' : isTranscribing ? 'Transcribing...' : 'Voice input'}
					>
						{#if isTranscribing}
							<svg class="w-[18px] h-[18px] animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
							</svg>
						{:else}
							<svg class="w-[18px] h-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
							</svg>
						{/if}
					</button>

					<button
						type="submit"
						class="send-btn"
						disabled={!inputValue.trim() || disabled || isUploading || isRecording || isTranscribing}
						title={isUploading ? "Uploading files..." : isRecording ? "Recording..." : isTranscribing ? "Transcribing..." : "Launch background agent"}
					>
						<svg class="w-[18px] h-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M12 5l7 7-7 7" />
						</svg>
					</button>
				</div>
			</div>
		</div>
	</form>
</div>

<style>
	/* Wrapper */
	.agent-input-wrapper {
		padding: 0.75rem;
		flex-shrink: 0;
	}

	/* Hidden file input */
	.hidden {
		display: none;
	}

	/* Error banner */
	.error-banner {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		margin-bottom: 0.5rem;
		padding: 0.5rem 0.75rem;
		background: color-mix(in srgb, var(--destructive) 10%, transparent);
		border: 1px solid color-mix(in srgb, var(--destructive) 30%, transparent);
		border-radius: 0.75rem;
		font-size: 0.8125rem;
		color: var(--destructive);
	}

	.error-close {
		font-size: 1.25rem;
		line-height: 1;
		opacity: 0.7;
		transition: opacity 0.15s;
	}

	.error-close:hover {
		opacity: 1;
	}

	/* Main Island */
	.agent-island {
		display: flex;
		flex-direction: column;
		background: color-mix(in srgb, var(--card) 95%, transparent);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border: 1px solid var(--border);
		border-radius: 1rem;
		box-shadow: 0 2px 12px -2px rgba(0, 0, 0, 0.08);
		overflow: visible;
		transition: border-color 0.2s, box-shadow 0.2s;
	}

	.agent-island:focus-within {
		border-color: color-mix(in srgb, var(--primary) 50%, var(--border));
		box-shadow: 0 2px 16px -2px rgba(0, 0, 0, 0.1), 0 0 0 3px color-mix(in srgb, var(--primary) 10%, transparent);
	}

	:global(.dark) .agent-island {
		background: color-mix(in srgb, oklch(0.16 0.01 260) 95%, transparent);
		border-color: oklch(0.28 0.01 260);
	}

	:global(.light) .agent-island {
		background: white;
		border-color: oklch(0.88 0.005 260);
		box-shadow: 0 2px 12px -2px rgba(0, 0, 0, 0.06), 0 1px 3px rgba(0, 0, 0, 0.04);
	}

	/* Uploaded files section */
	.uploaded-files {
		display: flex;
		flex-wrap: wrap;
		gap: 0.375rem;
		padding: 0.5rem 0.75rem 0;
	}

	.file-chip {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.25rem 0.5rem;
		background: color-mix(in srgb, var(--accent) 60%, transparent);
		border-radius: 0.5rem;
		font-size: 0.75rem;
	}

	.file-name {
		max-width: 100px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--foreground);
	}

	.file-remove {
		opacity: 0.5;
		transition: opacity 0.15s, color 0.15s;
	}

	.file-remove:hover {
		opacity: 1;
		color: var(--destructive);
	}

	/* Input row */
	.input-row {
		display: flex;
		align-items: flex-end;
		gap: 0.5rem;
		padding: 0.5rem 0.625rem;
	}

	/* Textarea wrapper */
	.textarea-wrapper {
		flex: 1;
		min-width: 0;
		position: relative;
		background: transparent;
		border-radius: 0.75rem;
		transition: background-color 0.15s;
	}

	/* Textarea */
	.agent-textarea {
		width: 100%;
		min-height: 38px;
		max-height: 140px;
		padding: 0.5rem 0.75rem;
		background: transparent;
		border: none;
		outline: none;
		resize: none;
		font-size: 0.875rem;
		line-height: 1.5;
		color: var(--foreground);
		field-sizing: content;
	}

	.agent-textarea::placeholder {
		color: var(--muted-foreground);
		opacity: 0.6;
	}

	.agent-textarea:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Action buttons */
	.action-buttons {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		flex-shrink: 0;
		padding-bottom: 0.125rem;
	}

	.action-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 34px;
		height: 34px;
		border-radius: 0.625rem;
		color: var(--muted-foreground);
		transition: background-color 0.15s, color 0.15s;
	}

	.action-btn:hover:not(:disabled) {
		background: var(--accent);
		color: var(--foreground);
	}

	.action-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	.action-btn.recording {
		background: color-mix(in srgb, var(--destructive) 20%, transparent);
		color: var(--destructive);
		animation: pulse 1.5s ease-in-out infinite;
	}

	.action-btn.transcribing {
		background: color-mix(in srgb, var(--primary) 15%, transparent);
		color: var(--primary);
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.6; }
	}

	.send-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 34px;
		height: 34px;
		background: var(--primary);
		color: var(--primary-foreground);
		border-radius: 0.625rem;
		transition: background-color 0.15s, opacity 0.15s;
	}

	.send-btn:hover:not(:disabled) {
		background: color-mix(in srgb, var(--primary) 90%, black);
	}

	.send-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	/* Utility classes */
	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>

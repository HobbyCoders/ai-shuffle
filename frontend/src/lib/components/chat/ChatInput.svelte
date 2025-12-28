<script lang="ts">
	/**
	 * ChatInput - Modern chat input island with progressive disclosure
	 *
	 * Layer 1: Minimal - textarea + essential buttons
	 * Layer 2: Context Bar - compact profile/project display (cosmetic) + settings gear
	 */
	import { tick, onDestroy } from 'svelte';
	import { tabs, profiles, projects, type ChatTab } from '$lib/stores/tabs';
	import { claudeAuthenticated, apiUser } from '$lib/stores/auth';
	import { api, type FileUploadResponse } from '$lib/api/client';
	import CommandAutocomplete from '$lib/components/CommandAutocomplete.svelte';
	import FileAutocomplete, { type FileItem } from '$lib/components/FileAutocomplete.svelte';

	interface Command {
		name: string;
		display: string;
		description: string;
		argument_hint?: string;
		type: 'custom' | 'interactive' | 'sdk_builtin';
	}

	interface Props {
		tab: ChatTab;
		compact?: boolean;
		onOpenTerminalModal?: (tabId: string, command: string) => void;
		onOpenSettings?: () => void;
	}

	let { tab, compact = false, onOpenTerminalModal, onOpenSettings }: Props = $props();

	// Context usage calculation
	function formatTokenCount(count: number): string {
		if (count >= 1000000) {
			return `${(count / 1000000).toFixed(1)}M`;
		}
		if (count >= 1000) {
			return `${(count / 1000).toFixed(1)}k`;
		}
		return count.toString();
	}

	// Context usage: use real-time tracked value, or calculate from token counts for resumed sessions
	const contextUsed = $derived(tab.contextUsed ?? (tab.totalTokensIn + tab.totalCacheCreationTokens + tab.totalCacheReadTokens));
	const contextMax = 200000;
	const contextPercent = $derived(Math.min((contextUsed / contextMax) * 100, 100));


	// Input state - use tab.draft for persistence across card switches
	let inputValue = $state(tab.draft || '');
	let uploadedFiles = $state<FileUploadResponse[]>([]);
	let previousTabId = $state(tab.id);
	let draftSaveTimeout: ReturnType<typeof setTimeout> | null = null;

	// Helper to save draft immediately (used on unmount and before tab switch)
	function saveDraftImmediately(tabId: string, value: string) {
		if (draftSaveTimeout) {
			clearTimeout(draftSaveTimeout);
			draftSaveTimeout = null;
		}
		tabs.setTabDraft(tabId, value);
	}

	// Sync input with tab draft when tab changes
	$effect(() => {
		const currentTabId = tab.id;

		// Detect actual tab change (not just re-render)
		if (previousTabId !== currentTabId) {
			// Save draft to old tab before switching
			saveDraftImmediately(previousTabId, inputValue);
			// Restore draft from new tab
			inputValue = tab.draft || '';
			previousTabId = currentTabId;
		}
	});

	// Save draft to tab store when input changes (debounced)
	$effect(() => {
		// Track inputValue for reactivity
		const currentValue = inputValue;
		const currentTabId = tab.id;

		// Debounce draft saves to avoid excessive updates
		if (draftSaveTimeout) {
			clearTimeout(draftSaveTimeout);
		}
		draftSaveTimeout = setTimeout(() => {
			if (currentValue !== tab.draft) {
				tabs.setTabDraft(currentTabId, currentValue);
			}
			draftSaveTimeout = null;
		}, 100);
	});

	// Save draft immediately when component unmounts (critical for mode switches)
	onDestroy(() => {
		saveDraftImmediately(tab.id, inputValue);
	});
	let textareaRef = $state<HTMLTextAreaElement | null>(null);
	let fileInputRef = $state<HTMLInputElement | null>(null);
	let isUploading = $state(false);

	// Voice recording state
	let isRecording = $state(false);
	let isTranscribing = $state(false);
	let mediaRecorder = $state<MediaRecorder | null>(null);
	let audioChunks = $state<Blob[]>([]);
	let recordingError = $state('');

	// Autocomplete state
	let showCommandAutocomplete = $state(false);
	let showFileAutocomplete = $state(false);
	let commandAutocompleteRef = $state<CommandAutocomplete | null>(null);
	let fileAutocompleteRef = $state<FileAutocomplete | null>(null);

	// Island ref
	let islandRef = $state<HTMLDivElement | null>(null);

	// Get selected profile/project names for display (cosmetic only)
	const selectedProfileName = $derived($profiles.find(p => p.id === tab.profile)?.name || 'Profile');
	const selectedProjectName = $derived($projects.find(p => p.id === tab.project)?.name || 'Project');

	// Open settings in Activity Panel
	function openSettings() {
		onOpenSettings?.();
	}

	// Check if input contains an active @ mention
	function hasActiveAtMention(input: string): boolean {
		for (let i = input.length - 1; i >= 0; i--) {
			if (input[i] === '@') {
				if (i === 0 || /\s/.test(input[i - 1])) {
					return true;
				}
			}
		}
		return false;
	}

	// Find the last @ position
	function findLastAtIndex(input: string): number {
		for (let i = input.length - 1; i >= 0; i--) {
			if (input[i] === '@') {
				if (i === 0 || /\s/.test(input[i - 1])) {
					return i;
				}
			}
		}
		return -1;
	}

	// Handle input changes for autocomplete
	function handleInputChange() {
		showCommandAutocomplete = inputValue.startsWith('/') && inputValue.length > 0;
		showFileAutocomplete = hasActiveAtMention(inputValue) && !!tab.project;
	}

	// Handle form submission
	async function handleSubmit() {
		if (!inputValue.trim() || !tab || isUploading) return;

		// Require profile and project to be selected
		if (!tab.profile) {
			tabs.setTabError(tab.id, 'Please select a profile before starting a chat');
			return;
		}
		if (!tab.project) {
			tabs.setTabError(tab.id, 'Please select a project before starting a chat');
			return;
		}

		// Append file references to the end of the message
		let finalPrompt = inputValue;
		if (uploadedFiles.length > 0) {
			const fileRefs = uploadedFiles.map(f => `@${f.path}`).join(' ');
			finalPrompt = inputValue.trim() + '\n\n' + fileRefs;
		}

		inputValue = '';
		uploadedFiles = [];

		// Reset textarea height
		if (textareaRef) {
			textareaRef.style.height = '';
		}

		tabs.sendMessage(tab.id, finalPrompt);
	}

	// Handle keydown for textarea
	function handleKeyDown(e: KeyboardEvent) {
		// Let command autocomplete handle the event first
		if (commandAutocompleteRef && showCommandAutocomplete) {
			const handled = commandAutocompleteRef.handleKeyDown(e);
			if (handled) return;
		}

		// Let file autocomplete handle the event
		if (fileAutocompleteRef && showFileAutocomplete) {
			const handled = fileAutocompleteRef.handleKeyDown(e);
			if (handled) return;
		}

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

	// Handle command selection
	async function handleCommandSelect(command: Command) {
		showCommandAutocomplete = false;

		if (command.type === 'interactive' && onOpenTerminalModal) {
			onOpenTerminalModal(tab.id, `/${command.name}`);
		} else {
			inputValue = `/${command.name} `;
			await tick();
			textareaRef?.focus();
		}
	}

	// Handle file selection
	function handleFileSelect(file: FileItem) {
		const atStartIndex = findLastAtIndex(inputValue);
		if (atStartIndex === -1) {
			showFileAutocomplete = false;
			return;
		}

		// For directories, replace with path and keep autocomplete open
		if (file.type === 'directory') {
			inputValue = inputValue.substring(0, atStartIndex) + '@' + file.path;
			tick().then(() => textareaRef?.focus());
			return;
		}

		// For files, add to uploaded files list
		const fileRef: FileUploadResponse = {
			filename: file.name,
			path: file.path,
			full_path: file.path,
			size: file.size || 0
		};

		uploadedFiles = [...uploadedFiles, fileRef];

		// Remove the @query from input
		inputValue = inputValue.substring(0, atStartIndex).trimEnd();
		showFileAutocomplete = false;

		tick().then(() => textareaRef?.focus());
	}

	// File upload
	function triggerFileUpload() {
		if (!tab.project) {
			alert('Please select a project first to upload files.');
			return;
		}
		fileInputRef?.click();
	}

	async function handleFileUpload(event: Event) {
		const input = event.target as HTMLInputElement;
		const files = input.files;
		if (!files || files.length === 0 || !tab.project) return;

		isUploading = true;
		try {
			for (const file of Array.from(files)) {
				const result = await api.uploadFile(`/projects/${tab.project}/upload`, file);
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

	// Context color based on usage
	const contextColor = $derived(
		contextPercent > 80 ? 'text-red-500' :
		contextPercent > 60 ? 'text-amber-500' :
		'text-emerald-500'
	);
</script>

<div class="chat-input-wrapper">
	<div class="chat-input-container">
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
			<div class="chat-island" bind:this={islandRef}>
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
					<!-- Textarea with autocomplete -->
					<div class="textarea-wrapper">
						<CommandAutocomplete
							bind:this={commandAutocompleteRef}
							inputValue={inputValue}
							projectId={tab.project}
							visible={showCommandAutocomplete}
							onSelect={handleCommandSelect}
							onClose={() => showCommandAutocomplete = false}
						/>

						<FileAutocomplete
							bind:this={fileAutocompleteRef}
							inputValue={inputValue}
							projectId={tab.project}
							visible={showFileAutocomplete}
							onSelect={handleFileSelect}
							onClose={() => showFileAutocomplete = false}
						/>

						<textarea
							bind:this={textareaRef}
							bind:value={inputValue}
							oninput={handleInputChange}
							onkeydown={handleKeyDown}
							placeholder="Message Claude..."
							class="chat-textarea"
							rows="1"
							disabled={!$claudeAuthenticated}
						></textarea>
					</div>

					<!-- Right side action buttons -->
					<div class="action-buttons">
						<button
							type="button"
							onclick={triggerFileUpload}
							disabled={tab.isStreaming || !$claudeAuthenticated || isUploading}
							class="action-btn"
							title={isUploading ? 'Uploading...' : tab.project ? 'Attach file' : 'Select a project to attach files'}
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
							disabled={!$claudeAuthenticated || isUploading || isTranscribing}
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

						{#if tab.isStreaming}
							{#if inputValue.trim()}
								<button type="submit" class="send-btn" title="Send message (interrupts current response)">
									<svg class="w-[18px] h-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M12 5l7 7-7 7" />
									</svg>
								</button>
							{:else}
								<button
									type="button"
									onclick={() => tabs.stopGeneration(tab.id)}
									class="stop-btn"
									title="Stop generating"
								>
									<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
										<rect x="6" y="6" width="12" height="12" rx="2" />
									</svg>
								</button>
							{/if}
						{:else}
							<button
								type="submit"
								class="send-btn"
								disabled={!inputValue.trim() || !$claudeAuthenticated || isUploading || isRecording || isTranscribing}
								title={isUploading ? "Uploading files..." : isRecording ? "Recording..." : isTranscribing ? "Transcribing..." : "Send message"}
							>
								<svg class="w-[18px] h-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M12 5l7 7-7 7" />
								</svg>
							</button>
						{/if}
					</div>
				</div>

				<!-- Simplified Context Bar - cosmetic display only -->
				<div class="context-bar">
					<!-- Left: Profile & Project as display text (not interactive) -->
					<div class="context-selectors">
						<span class="context-selector {tab.profile ? '' : 'unset'}">
							<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
							</svg>
							<span>{selectedProfileName}</span>
						</span>

						<span class="context-separator">•</span>

						<span class="context-selector {tab.project ? '' : 'unset'}">
							<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
							</svg>
							<span>{selectedProjectName}</span>
						</span>
					</div>

					<!-- Right: Context % and Settings gear -->
					<div class="context-right">
						<span class="context-percent {contextColor}" title="{formatTokenCount(contextUsed)} / {formatTokenCount(contextMax)} tokens">
							{Math.round(contextPercent)}%
						</span>

						<button
							type="button"
							onclick={openSettings}
							class="settings-btn"
							title="Open settings"
						>
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
							</svg>
						</button>
					</div>
				</div>
			</div>
		</form>
	</div>
</div>

<style>
	/* Wrapper - provides safe spacing */
	.chat-input-wrapper {
		padding: 0.5rem 0.75rem 0.75rem;
		flex-shrink: 0; /* Prevents input from shrinking - stays fixed at bottom */
	}

	@media (min-width: 640px) {
		.chat-input-wrapper {
			padding: 0.75rem 1rem 1rem;
		}
	}

	/* Container with max-width */
	.chat-input-container {
		max-width: 56rem;
		margin: 0 auto;
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
	.chat-island {
		display: flex;
		flex-direction: column;
		background: color-mix(in srgb, var(--card) 95%, transparent);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border: 1px solid var(--border);
		border-radius: 1.25rem;
		box-shadow: 0 2px 12px -2px rgba(0, 0, 0, 0.08);
		overflow: visible;
		transition: border-color 0.2s, box-shadow 0.2s;
	}

	.chat-island:focus-within {
		border-color: color-mix(in srgb, var(--primary) 50%, var(--border));
		box-shadow: 0 2px 16px -2px rgba(0, 0, 0, 0.1), 0 0 0 3px color-mix(in srgb, var(--primary) 10%, transparent);
	}

	:global(.dark) .chat-island {
		background: color-mix(in srgb, oklch(0.16 0.01 260) 95%, transparent);
		border-color: oklch(0.28 0.01 260);
	}

	:global(.light) .chat-island {
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
		padding: 0.5rem;
	}

	@media (min-width: 640px) {
		.input-row {
			padding: 0.625rem 0.75rem;
		}
	}

	/* Textarea wrapper */
	.textarea-wrapper {
		flex: 1;
		min-width: 0;
		position: relative;
		background: transparent;
		border-radius: 1rem;
		transition: background-color 0.15s;
	}

	/* Textarea */
	.chat-textarea {
		width: 100%;
		min-height: 42px;
		max-height: 180px;
		padding: 0.625rem 0.875rem;
		background: transparent;
		border: none;
		outline: none;
		resize: none;
		font-size: 0.9375rem;
		line-height: 1.5;
		color: var(--foreground);
		field-sizing: content;
	}

	.chat-textarea::placeholder {
		color: var(--muted-foreground);
		opacity: 0.6;
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
		width: 36px;
		height: 36px;
		border-radius: 0.75rem;
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
		width: 36px;
		height: 36px;
		background: var(--primary);
		color: var(--primary-foreground);
		border-radius: 0.75rem;
		transition: background-color 0.15s, opacity 0.15s;
	}

	.send-btn:hover:not(:disabled) {
		background: color-mix(in srgb, var(--primary) 90%, black);
	}

	.send-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	.stop-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		background: color-mix(in srgb, var(--destructive) 15%, transparent);
		color: var(--destructive);
		border-radius: 0.75rem;
		transition: background-color 0.15s;
	}

	.stop-btn:hover {
		background: color-mix(in srgb, var(--destructive) 25%, transparent);
	}

	/* Simplified Context Bar */
	.context-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.375rem 0.625rem;
		border-top: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
	}

	@media (min-width: 640px) {
		.context-bar {
			padding: 0.5rem 0.75rem;
		}
	}

	.context-selectors {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		flex-wrap: wrap;
	}

	.context-selector {
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.25rem 0.5rem;
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--muted-foreground);
		border-radius: 0.5rem;
	}

	.context-selector.unset {
		color: var(--warning);
	}

	.context-selector span {
		max-width: 100px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	@media (min-width: 640px) {
		.context-selector span {
			max-width: 140px;
		}
	}

	.context-separator {
		color: var(--muted-foreground);
		opacity: 0.4;
		font-size: 0.75rem;
	}

	.context-right {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.context-percent {
		font-size: 0.6875rem;
		font-weight: 600;
	}

	.settings-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		border-radius: 0.5rem;
		color: var(--muted-foreground);
		transition: background-color 0.15s, color 0.15s;
	}

	.settings-btn:hover {
		background: var(--accent);
		color: var(--foreground);
	}
</style>

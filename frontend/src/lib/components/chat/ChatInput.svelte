<script lang="ts">
	/**
	 * ChatInput - Modern chat input island with progressive disclosure
	 *
	 * Layer 1: Minimal - textarea + essential buttons
	 * Layer 2: Context Bar - inline profile/project dropdown selectors
	 */
	import { tick, onDestroy, onMount } from 'svelte';
	import { tabs, profiles, projects, type ChatTab } from '$lib/stores/tabs';
	import { claudeAuthenticated, apiUser } from '$lib/stores/auth';
	import { api, type FileUploadResponse } from '$lib/api/client';
	import CommandAutocomplete from '$lib/components/CommandAutocomplete.svelte';
	import FileAutocomplete, { type FileItem } from '$lib/components/FileAutocomplete.svelte';
	import { AUTO_COMPACTION_BASE, CONTEXT_MAX } from '$lib/constants/chat';

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
	}

	let { tab, compact = false, onOpenTerminalModal }: Props = $props();

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

	// Get current profile to access env vars for auto-compaction reserve calculation
	const currentProfile = $derived($profiles.find(p => p.id === tab.profile));

	// Auto-compaction reserve: 13k base + CLAUDE_CODE_MAX_OUTPUT_TOKENS from profile env vars
	const maxOutputTokens = $derived(
		parseInt((currentProfile?.config as any)?.env?.CLAUDE_CODE_MAX_OUTPUT_TOKENS || '0', 10) || 0
	);
	const autoCompactionReserve = $derived(AUTO_COMPACTION_BASE + maxOutputTokens);

	// Context usage: use real-time tracked value, or calculate from token counts for resumed sessions
	// Add auto-compaction reserve to show effective context usage
	const baseContextUsed = $derived(tab.contextUsed ?? (tab.totalTokensIn + tab.totalCacheCreationTokens + tab.totalCacheReadTokens));
	const contextUsed = $derived(baseContextUsed + autoCompactionReserve);
	const contextPercent = $derived(Math.min((contextUsed / CONTEXT_MAX) * 100, 100));


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

	// Dropdown state for profile/project selection
	let showProfileDropdown = $state(false);
	let showProjectDropdown = $state(false);
	let profileDropdownRef = $state<HTMLDivElement | null>(null);
	let projectDropdownRef = $state<HTMLDivElement | null>(null);

	// Get selected profile/project names for display
	const selectedProfileName = $derived($profiles.find(p => p.id === tab.profile)?.name || 'Profile');
	const selectedProjectName = $derived($projects.find(p => p.id === tab.project)?.name || 'Project');

	// Toggle profile dropdown
	function handleProfileClick(e: MouseEvent) {
		e.stopPropagation();
		showProjectDropdown = false;
		showProfileDropdown = !showProfileDropdown;
	}

	// Toggle project dropdown
	function handleProjectClick(e: MouseEvent) {
		e.stopPropagation();
		showProfileDropdown = false;
		showProjectDropdown = !showProjectDropdown;
	}

	// Select a profile
	function selectProfile(profileId: string) {
		tabs.setTabProfile(tab.id, profileId);
		showProfileDropdown = false;
	}

	// Select a project
	function selectProject(projectId: string) {
		tabs.setTabProject(tab.id, projectId);
		showProjectDropdown = false;
	}

	// Close dropdowns when clicking outside
	function handleClickOutside(e: MouseEvent) {
		const target = e.target as Node;
		if (profileDropdownRef && !profileDropdownRef.contains(target)) {
			showProfileDropdown = false;
		}
		if (projectDropdownRef && !projectDropdownRef.contains(target)) {
			showProjectDropdown = false;
		}
	}

	// Close dropdowns on escape
	function handleEscape(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			showProfileDropdown = false;
			showProjectDropdown = false;
		}
	}

	onMount(() => {
		document.addEventListener('click', handleClickOutside);
		document.addEventListener('keydown', handleEscape);
		return () => {
			document.removeEventListener('click', handleClickOutside);
			document.removeEventListener('keydown', handleEscape);
		};
	});

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

		// Clear draft immediately (not debounced) - important because
		// the component may be destroyed/recreated when isEmptyState changes
		saveDraftImmediately(tab.id, '');

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
				<!-- Input Wrapper - The Hero (outer container with border) -->
				<div class="input-wrapper">
					<!-- Uploaded Files (if any) - inside wrapper so focus glow includes them -->
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
					<!-- Input Inner - Dark background container -->
					<div class="input-inner">
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

						<!-- Send button only -->
						<div class="send-button-wrapper">
							{#if tab.isStreaming}
								{#if inputValue.trim()}
									<button type="submit" class="send-btn" title="Send message (interrupts current response)">
										<svg class="w-[14px] h-[14px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 12h14M12 5l7 7-7 7" />
										</svg>
									</button>
								{:else}
									<button
										type="button"
										onclick={() => tabs.stopGeneration(tab.id)}
										class="stop-btn"
										title="Stop generating"
									>
										<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24">
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
									<svg class="w-[14px] h-[14px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 12h14M12 5l7 7-7 7" />
									</svg>
								</button>
							{/if}
						</div>
					</div>
				</div>

				<!-- Controls Dock - Three column layout -->
				<div class="controls-dock">
					<!-- Left: Usage Indicator -->
					<div class="controls-left">
						<div class="usage-indicator {contextColor}" title="{formatTokenCount(contextUsed)} / {formatTokenCount(CONTEXT_MAX)} tokens">
							<div class="usage-bar">
								<div class="usage-fill" style="width: {Math.min(contextPercent, 100)}%"></div>
							</div>
							<span class="usage-text">{Math.round(contextPercent)}%</span>
						</div>
					</div>

					<!-- Center: Profile & Project selectors -->
					<div class="controls-center">
						<!-- Profile Selector -->
						<div class="selector-wrapper" bind:this={profileDropdownRef}>
							<button
								type="button"
								class="context-selector-btn {tab.profile ? '' : 'unset'}"
								onclick={handleProfileClick}
								title="Change profile"
							>
								<div class="selector-icon">
									<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
									</svg>
								</div>
								<span class="selector-text">{selectedProfileName}</span>
								<svg class="w-3 h-3 opacity-50 chevron" class:open={showProfileDropdown} fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>

							{#if showProfileDropdown}
								<div class="selector-dropdown">
									{#if $profiles.length === 0}
										<div class="dropdown-empty">No profiles available</div>
									{:else}
										{#each $profiles as profile}
											<button
												type="button"
												class="dropdown-item"
												class:selected={profile.id === tab.profile}
												onclick={() => selectProfile(profile.id)}
											>
												<svg class="w-3.5 h-3.5 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
												</svg>
												<span>{profile.name}</span>
												{#if profile.id === tab.profile}
													<svg class="w-3.5 h-3.5 ml-auto text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
													</svg>
												{/if}
											</button>
										{/each}
									{/if}
								</div>
							{/if}
						</div>

						<!-- Project Selector -->
						<div class="selector-wrapper" bind:this={projectDropdownRef}>
							<button
								type="button"
								class="context-selector-btn {tab.project ? '' : 'unset'}"
								onclick={handleProjectClick}
								title="Change project"
							>
								<div class="selector-icon">
									<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
									</svg>
								</div>
								<span class="selector-text">{selectedProjectName}</span>
								<svg class="w-3 h-3 opacity-50 chevron" class:open={showProjectDropdown} fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>

							{#if showProjectDropdown}
								<div class="selector-dropdown">
									{#if $projects.length === 0}
										<div class="dropdown-empty">No projects available</div>
									{:else}
										{#each $projects as project}
											<button
												type="button"
												class="dropdown-item"
												class:selected={project.id === tab.project}
												onclick={() => selectProject(project.id)}
											>
												<svg class="w-3.5 h-3.5 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
												</svg>
												<span>{project.name}</span>
												{#if project.id === tab.project}
													<svg class="w-3.5 h-3.5 ml-auto text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
													</svg>
												{/if}
											</button>
										{/each}
									{/if}
								</div>
							{/if}
						</div>
					</div>

					<!-- Right: Attach & Mic buttons -->
					<div class="controls-right">
						<button
							type="button"
							onclick={triggerFileUpload}
							disabled={tab.isStreaming || !$claudeAuthenticated || isUploading}
							class="dock-action-btn"
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
							class="dock-action-btn {isRecording ? 'recording' : ''} {isTranscribing ? 'transcribing' : ''}"
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
		flex-shrink: 0;
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

	/* Main Island - Two tier structure */
	.chat-island {
		display: flex;
		flex-direction: column;
		overflow: visible;
	}

	/* Uploaded files section - now inside input-wrapper */
	.uploaded-files {
		display: flex;
		flex-wrap: wrap;
		gap: 0.375rem;
		padding: 8px 12px;
		margin-bottom: 4px;
		background: transparent;
	}

	.file-chip {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.25rem 0.5rem;
		background: rgba(45, 212, 191, 0.15);
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

	/* ===== INPUT WRAPPER - THE HERO (glass outer frame) ===== */
	.input-wrapper {
		position: relative;
		background: rgba(255, 255, 255, 0.03);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-radius: 16px 16px 0 0;
		padding: 4px;
		transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
	}

	:global(.light) .input-wrapper {
		background: rgba(255, 255, 255, 0.7);
		border-color: rgba(0, 0, 0, 0.08);
	}


	/* Focus glow effect */
	.input-wrapper:focus-within {
		border-color: rgba(45, 212, 191, 0.5);
		box-shadow:
			0 0 0 1px rgba(45, 212, 191, 0.5),
			0 0 40px -10px rgba(45, 212, 191, 0.15),
			inset 0 1px 0 rgba(255, 255, 255, 0.05);
	}

	:global(.light) .input-wrapper:focus-within {
		border-color: rgba(20, 184, 166, 0.5);
		box-shadow:
			0 0 0 1px rgba(20, 184, 166, 0.4),
			0 0 40px -10px rgba(20, 184, 166, 0.2);
	}

	/* ===== INPUT INNER - Glass rounded bubble inside ===== */
	.input-inner {
		display: flex;
		align-items: flex-end;
		gap: 8px;
		background: rgba(0, 0, 0, 0.3);
		border-radius: 12px;
		padding: 12px 16px;
	}

	:global(.light) .input-inner {
		background: rgba(255, 255, 255, 0.8);
	}

	/* Textarea wrapper */
	.textarea-wrapper {
		flex: 1;
		min-width: 0;
		position: relative;
		background: transparent;
	}

	/* Textarea */
	.chat-textarea {
		width: 100%;
		min-height: 24px;
		max-height: 300px;
		padding: 0;
		background: transparent !important;
		background-color: transparent !important;
		border: none !important;
		outline: none !important;
		box-shadow: none !important;
		resize: none;
		font-family: 'Outfit', -apple-system, sans-serif;
		font-size: 15px;
		font-weight: 400;
		line-height: 1.5;
		color: #f4f4f5;
		field-sizing: content;
		-webkit-appearance: none;
		-moz-appearance: none;
		appearance: none;
	}

	.chat-textarea:focus {
		outline: none !important;
		border: none !important;
		box-shadow: none !important;
	}

	:global(.light) .chat-textarea {
		color: #18181b;
		background: transparent !important;
		background-color: transparent !important;
	}

	:global(.light) .chat-textarea:focus {
		outline: none !important;
		border: none !important;
		box-shadow: none !important;
	}

	.chat-textarea::placeholder {
		color: rgba(255, 255, 255, 0.3);
		font-weight: 300;
	}

	:global(.light) .chat-textarea::placeholder {
		color: rgba(0, 0, 0, 0.35);
	}

	.chat-textarea:focus::placeholder {
		color: rgba(255, 255, 255, 0.5);
	}

	:global(.light) .chat-textarea:focus::placeholder {
		color: rgba(0, 0, 0, 0.5);
	}

	/* Send button wrapper */
	.send-button-wrapper {
		display: flex;
		align-items: center;
		flex-shrink: 0;
	}

	/* ===== SEND BUTTON - COMPACT ===== */
	.send-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		background: linear-gradient(135deg, #2dd4bf, #14b8a6);
		border: none;
		border-radius: 8px;
		cursor: pointer;
		transition: all 0.2s ease;
		flex-shrink: 0;
		position: relative;
		overflow: hidden;
	}

	.send-btn::before {
		content: '';
		position: absolute;
		inset: 0;
		background: linear-gradient(135deg, rgba(255,255,255,0.2), transparent);
		opacity: 0;
		transition: opacity 0.2s ease;
	}

	.send-btn:hover:not(:disabled) {
		transform: translateY(-1px);
		box-shadow: 0 4px 20px rgba(45, 212, 191, 0.15);
	}

	.send-btn:hover:not(:disabled)::before {
		opacity: 1;
	}

	.send-btn:active:not(:disabled) {
		transform: translateY(0) scale(0.96);
	}

	.send-btn svg {
		color: #0a0a0b;
		position: relative;
		z-index: 1;
	}

	.send-btn:disabled {
		background: #1a1a1d;
		cursor: not-allowed;
	}

	:global(.light) .send-btn:disabled {
		background: #e4e4e7;
	}

	.send-btn:disabled svg {
		color: rgba(255, 255, 255, 0.3);
	}

	:global(.light) .send-btn:disabled svg {
		color: rgba(0, 0, 0, 0.3);
	}

	.stop-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		background: rgba(239, 68, 68, 0.15);
		color: #ef4444;
		border-radius: 8px;
		transition: background-color 0.15s;
	}

	.stop-btn:hover {
		background: rgba(239, 68, 68, 0.25);
	}

	/* ===== CONTROLS DOCK ===== */
	.controls-dock {
		display: flex;
		align-items: center;
		justify-content: space-between;
		background: rgba(255, 255, 255, 0.02);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border: 1px solid rgba(255, 255, 255, 0.06);
		border-top: none;
		border-radius: 0 0 16px 16px;
		padding: 8px 12px;
	}

	:global(.light) .controls-dock {
		background: rgba(255, 255, 255, 0.6);
		border-color: rgba(0, 0, 0, 0.06);
	}

	.controls-left {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1;
	}

	.controls-center {
		display: flex;
		align-items: center;
		gap: 4px;
		justify-content: center;
		flex: 2;
	}

	.controls-right {
		display: flex;
		align-items: center;
		gap: 4px;
		justify-content: flex-end;
		flex: 1;
	}

	/* ===== USAGE INDICATOR ===== */
	.usage-indicator {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 4px 8px;
		border-radius: 6px;
		background: rgba(34, 197, 94, 0.08);
		border: 1px solid rgba(34, 197, 94, 0.15);
	}

	.usage-indicator.text-amber-500 {
		background: rgba(245, 158, 11, 0.08);
		border-color: rgba(245, 158, 11, 0.15);
	}

	.usage-indicator.text-red-500 {
		background: rgba(239, 68, 68, 0.08);
		border-color: rgba(239, 68, 68, 0.15);
	}

	.usage-bar {
		width: 40px;
		height: 4px;
		background: #131315;
		border-radius: 2px;
		overflow: hidden;
	}

	:global(.light) .usage-bar {
		background: #e4e4e7;
	}

	.usage-fill {
		height: 100%;
		background: linear-gradient(90deg, #22c55e, #4ade80);
		border-radius: 2px;
		transition: width 0.3s ease;
	}

	.usage-indicator.text-amber-500 .usage-fill {
		background: linear-gradient(90deg, #f59e0b, #fbbf24);
	}

	.usage-indicator.text-red-500 .usage-fill {
		background: linear-gradient(90deg, #ef4444, #f87171);
	}

	.usage-text {
		font-family: ui-monospace, 'Monaco', 'Menlo', monospace;
		font-size: 11px;
		font-weight: 500;
		color: #22c55e;
		letter-spacing: 0.02em;
	}

	.usage-indicator.text-amber-500 .usage-text {
		color: #f59e0b;
	}

	.usage-indicator.text-red-500 .usage-text {
		color: #ef4444;
	}

	/* ===== SELECTORS (Agent/Project) ===== */
	.selector-wrapper {
		position: relative;
	}

	.context-selector-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 4px 10px 4px 8px;
		background: transparent;
		border: 1px solid transparent;
		border-radius: 6px;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.context-selector-btn:hover {
		background: rgba(255, 255, 255, 0.03);
		border-color: rgba(255, 255, 255, 0.06);
	}

	:global(.light) .context-selector-btn:hover {
		background: rgba(0, 0, 0, 0.03);
		border-color: rgba(0, 0, 0, 0.08);
	}

	.context-selector-btn.unset {
		color: #f59e0b;
	}

	.context-selector-btn.unset:hover {
		background: rgba(245, 158, 11, 0.1);
		border-color: rgba(245, 158, 11, 0.2);
	}

	.selector-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
		border-radius: 5px;
		background: #131315;
		border: 1px solid rgba(255, 255, 255, 0.06);
	}

	:global(.light) .selector-icon {
		background: #e4e4e7;
		border-color: rgba(0, 0, 0, 0.08);
	}

	.selector-icon svg {
		color: rgba(255, 255, 255, 0.5);
	}

	:global(.light) .selector-icon svg {
		color: rgba(0, 0, 0, 0.5);
	}

	.selector-text {
		font-size: 12px;
		font-weight: 500;
		color: rgba(255, 255, 255, 0.5);
		letter-spacing: 0.01em;
		max-width: 80px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	:global(.light) .selector-text {
		color: rgba(0, 0, 0, 0.5);
	}

	@media (min-width: 640px) {
		.selector-text {
			max-width: 120px;
		}
	}

	/* Chevron */
	.chevron {
		color: rgba(255, 255, 255, 0.3);
		transition: transform 0.2s ease;
	}

	:global(.light) .chevron {
		color: rgba(0, 0, 0, 0.3);
	}

	.chevron.open {
		transform: rotate(180deg);
	}

	/* ===== ACTION BUTTONS ===== */
	.dock-action-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		background: transparent;
		border: none;
		border-radius: 8px;
		cursor: pointer;
		color: rgba(255, 255, 255, 0.3);
		transition: all 0.15s ease;
	}

	:global(.light) .dock-action-btn {
		color: rgba(0, 0, 0, 0.35);
	}

	.dock-action-btn:hover:not(:disabled) {
		background: rgba(255, 255, 255, 0.05);
		color: rgba(255, 255, 255, 0.5);
	}

	:global(.light) .dock-action-btn:hover:not(:disabled) {
		background: rgba(0, 0, 0, 0.05);
		color: rgba(0, 0, 0, 0.6);
	}

	.dock-action-btn:active:not(:disabled) {
		transform: scale(0.94);
	}

	.dock-action-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	.dock-action-btn.recording {
		background: rgba(239, 68, 68, 0.15);
		color: #ef4444;
		animation: pulse 1.5s ease-in-out infinite;
	}

	.dock-action-btn.transcribing {
		background: rgba(45, 212, 191, 0.15);
		color: #2dd4bf;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.6; }
	}

	/* ===== DROPDOWN MENU ===== */
	.selector-dropdown {
		position: absolute;
		bottom: calc(100% + 4px);
		left: 50%;
		transform: translateX(-50%);
		min-width: 180px;
		max-width: 280px;
		max-height: 240px;
		overflow-y: auto;
		background: #1a1a1d;
		border: 1px solid rgba(255, 255, 255, 0.1);
		border-radius: 12px;
		box-shadow: 0 4px 16px -2px rgba(0, 0, 0, 0.25), 0 2px 8px -2px rgba(0, 0, 0, 0.15);
		z-index: 50;
		padding: 6px;
		animation: dropdown-fade-in 0.15s ease;
	}

	:global(.light) .selector-dropdown {
		background: #ffffff;
		border-color: rgba(0, 0, 0, 0.1);
		box-shadow: 0 4px 16px -2px rgba(0, 0, 0, 0.1), 0 2px 8px -2px rgba(0, 0, 0, 0.08);
	}

	@keyframes dropdown-fade-in {
		from {
			opacity: 0;
			transform: translateX(-50%) translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateX(-50%) translateY(0);
		}
	}

	.dropdown-empty {
		padding: 12px 16px;
		font-size: 13px;
		color: rgba(255, 255, 255, 0.5);
		text-align: center;
	}

	:global(.light) .dropdown-empty {
		color: rgba(0, 0, 0, 0.5);
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 8px 12px;
		font-size: 13px;
		color: #f4f4f5;
		background: transparent;
		border: none;
		border-radius: 8px;
		cursor: pointer;
		transition: background-color 0.15s;
		text-align: left;
	}

	:global(.light) .dropdown-item {
		color: #18181b;
	}

	.dropdown-item:hover {
		background: rgba(255, 255, 255, 0.05);
	}

	:global(.light) .dropdown-item:hover {
		background: rgba(0, 0, 0, 0.05);
	}

	.dropdown-item.selected {
		background: rgba(45, 212, 191, 0.12);
	}

	.dropdown-item span {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Scrollbar styling for dropdown */
	.selector-dropdown::-webkit-scrollbar {
		width: 6px;
	}

	.selector-dropdown::-webkit-scrollbar-track {
		background: transparent;
	}

	.selector-dropdown::-webkit-scrollbar-thumb {
		background: rgba(255, 255, 255, 0.1);
		border-radius: 3px;
	}

	:global(.light) .selector-dropdown::-webkit-scrollbar-thumb {
		background: rgba(0, 0, 0, 0.15);
	}

	.selector-dropdown::-webkit-scrollbar-thumb:hover {
		background: rgba(255, 255, 255, 0.2);
	}

	:global(.light) .selector-dropdown::-webkit-scrollbar-thumb:hover {
		background: rgba(0, 0, 0, 0.25);
	}

	/* Responsive: hide selector text on mobile */
	@media (max-width: 500px) {
		.selector-text {
			display: none;
		}

		.context-selector-btn {
			padding: 4px;
		}
	}
</style>

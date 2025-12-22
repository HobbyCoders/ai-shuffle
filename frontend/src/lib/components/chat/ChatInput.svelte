<script lang="ts">
	/**
	 * ChatInput - Full input section with file upload, autocomplete, and voice recording
	 */
	import { tick, onDestroy } from 'svelte';
	import { tabs, profiles, projects, type ChatTab } from '$lib/stores/tabs';
	import { claudeAuthenticated, isAdmin, apiUser } from '$lib/stores/auth';
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
	}

	let { tab, compact = false, onOpenTerminalModal }: Props = $props();

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

	// Override popup state
	let showModelPopup = $state(false);
	let showModePopup = $state(false);
	let showProfilePopup = $state(false);
	let showProjectPopup = $state(false);

	// Get current profile settings
	const currentProfile = $derived($profiles.find(p => p.id === tab.profile));
	const profileModel = $derived(currentProfile?.config?.model || 'sonnet');
	const profilePermissionMode = $derived(currentProfile?.config?.permission_mode || 'default');
	const effectiveModel = $derived(tab.modelOverride || profileModel);
	const effectiveMode = $derived(tab.permissionModeOverride || profilePermissionMode);

	const modelLabels: Record<string, string> = {
		sonnet: 'Sonnet',
		'sonnet-1m': 'Sonnet 1M',
		opus: 'Opus',
		haiku: 'Haiku'
	};

	const modeLabels: Record<string, string> = {
		default: 'Ask',
		acceptEdits: 'Auto-Accept',
		plan: 'Plan',
		bypassPermissions: 'Bypass'
	};

	// Get selected profile/project names for display
	const selectedProfileName = $derived($profiles.find(p => p.id === tab.profile)?.name || 'Profile');
	const selectedProjectName = $derived($projects.find(p => p.id === tab.project)?.name || 'Project');
	const isProfileLocked = $derived(!!$apiUser?.profile_id);
	const isProjectLocked = $derived(!!$apiUser?.project_id || !!tab.sessionId);

	// Close all popups helper
	function closeAllPopups() {
		showModelPopup = false;
		showModePopup = false;
		showProfilePopup = false;
		showProjectPopup = false;
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
</script>

<div class="px-3 py-1.5 sm:p-4 {compact ? 'py-2' : ''}">
	<div class="max-w-4xl mx-auto">
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
			<div class="mb-2 px-3 py-2 bg-destructive/10 border border-destructive/30 rounded-lg text-sm text-destructive flex items-center justify-between floating-panel">
				<span>{recordingError}</span>
				<button type="button" onclick={() => recordingError = ''} class="ml-2 hover:opacity-70">&times;</button>
			</div>
		{/if}

		<form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }} class="relative">
			<!-- Top Controls Row (Pills + Attach) -->
			<div class="mb-2 flex flex-wrap items-center justify-center gap-1.5">
				<!-- Profile Selector Pill (always visible, mobile-friendly) -->
				{#if !isProfileLocked}
					<div class="relative">
						<button
							type="button"
							onclick={() => { closeAllPopups(); showProfilePopup = !showProfilePopup; }}
							class="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all disabled:opacity-40 {tab.profile ? 'bg-accent/50 text-foreground border border-transparent hover:border-border/50' : 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-500/30'}"
							disabled={tab.isStreaming}
						>
							<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
							</svg>
							<span class="max-w-[80px] truncate">{selectedProfileName}</span>
							<svg class="w-2.5 h-2.5 opacity-60 transition-transform {showProfilePopup ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7" />
							</svg>
						</button>
						{#if showProfilePopup}
							<button class="fixed inset-0 z-40" onclick={() => showProfilePopup = false} aria-label="Close"></button>
							<div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 bg-card border border-border rounded-xl shadow-xl overflow-hidden z-50 min-w-[140px] max-h-[240px] overflow-y-auto">
								{#if $profiles.length === 0}
									<div class="px-3 py-2 text-xs text-muted-foreground">No profiles</div>
								{:else}
									{#each $profiles as profile}
										<button
											type="button"
											onclick={() => {
												tabs.setTabProfile(tab.id, profile.id);
												showProfilePopup = false;
											}}
											class="w-full px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors flex items-center justify-between gap-2 {tab.profile === profile.id ? 'bg-accent/50 text-foreground font-medium' : 'text-muted-foreground'}"
										>
											<span class="truncate">{profile.name}</span>
											{#if tab.profile === profile.id}
												<svg class="w-3 h-3 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
												</svg>
											{/if}
										</button>
									{/each}
								{/if}
							</div>
						{/if}
					</div>
				{:else}
					<!-- Locked profile indicator -->
					<div class="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full bg-accent/30 text-muted-foreground">
						<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
						</svg>
						<span class="max-w-[80px] truncate">{selectedProfileName}</span>
						<svg class="w-2.5 h-2.5 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
						</svg>
					</div>
				{/if}

				<!-- Project Selector Pill (always visible, mobile-friendly) -->
				{#if !isProjectLocked}
					<div class="relative">
						<button
							type="button"
							onclick={() => { closeAllPopups(); showProjectPopup = !showProjectPopup; }}
							class="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all disabled:opacity-40 {tab.project ? 'bg-accent/50 text-foreground border border-transparent hover:border-border/50' : 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-500/30'}"
							disabled={tab.isStreaming}
						>
							<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
							</svg>
							<span class="max-w-[80px] truncate">{selectedProjectName}</span>
							<svg class="w-2.5 h-2.5 opacity-60 transition-transform {showProjectPopup ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7" />
							</svg>
						</button>
						{#if showProjectPopup}
							<button class="fixed inset-0 z-40" onclick={() => showProjectPopup = false} aria-label="Close"></button>
							<div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 bg-card border border-border rounded-xl shadow-xl overflow-hidden z-50 min-w-[140px] max-h-[240px] overflow-y-auto">
								{#if $projects.length === 0}
									<div class="px-3 py-2 text-xs text-muted-foreground">No projects</div>
								{:else}
									{#each $projects as project}
										<button
											type="button"
											onclick={() => {
												tabs.setTabProject(tab.id, project.id);
												showProjectPopup = false;
											}}
											class="w-full px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors flex items-center justify-between gap-2 {tab.project === project.id ? 'bg-accent/50 text-foreground font-medium' : 'text-muted-foreground'}"
										>
											<span class="truncate">{project.name}</span>
											{#if tab.project === project.id}
												<svg class="w-3 h-3 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
												</svg>
											{/if}
										</button>
									{/each}
								{/if}
							</div>
						{/if}
					</div>
				{:else}
					<!-- Locked project indicator -->
					<div class="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full bg-accent/30 text-muted-foreground">
						<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
						</svg>
						<span class="max-w-[80px] truncate">{selectedProjectName}</span>
						<svg class="w-2.5 h-2.5 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
						</svg>
					</div>
				{/if}

				<!-- Model/Mode Pills (Admin only) -->
				{#if $isAdmin}
					<!-- Model Selector Pill -->
					<div class="relative">
						<button
							type="button"
							onclick={() => { closeAllPopups(); showModelPopup = !showModelPopup; }}
							class="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all disabled:opacity-40 {tab.modelOverride ? 'bg-primary/15 text-primary border border-primary/30' : 'bg-accent/50 text-muted-foreground hover:text-foreground border border-transparent hover:border-border/50'}"
							disabled={tab.isStreaming}
						>
							<span>{modelLabels[effectiveModel] || effectiveModel}</span>
							<svg class="w-2.5 h-2.5 opacity-60 transition-transform rotate-180 {showModelPopup ? 'rotate-0' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7" />
							</svg>
						</button>
						{#if showModelPopup}
							<button class="fixed inset-0 z-40" onclick={() => showModelPopup = false} aria-label="Close"></button>
							<div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 bg-card border border-border rounded-xl shadow-xl overflow-hidden z-50 min-w-[90px]">
								{#each [['sonnet', 'Sonnet'], ['sonnet-1m', 'Sonnet 1M'], ['opus', 'Opus'], ['haiku', 'Haiku']] as [value, label]}
									<button
										type="button"
										onclick={() => {
											if (value === profileModel) {
												tabs.setTabModelOverride(tab.id, null);
											} else {
												tabs.setTabModelOverride(tab.id, value);
											}
											showModelPopup = false;
										}}
										class="w-full px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors flex items-center justify-between gap-3 {effectiveModel === value ? 'bg-accent/50 text-foreground font-medium' : 'text-muted-foreground'}"
									>
										<span>{label}</span>
										{#if effectiveModel === value}
											<svg class="w-3 h-3 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
											</svg>
										{/if}
									</button>
								{/each}
							</div>
						{/if}
					</div>

					<!-- Permission Mode Selector Pill -->
					<div class="relative">
						<button
							type="button"
							onclick={() => { closeAllPopups(); showModePopup = !showModePopup; }}
							class="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-full transition-all disabled:opacity-40 {tab.permissionModeOverride ? 'bg-primary/15 text-primary border border-primary/30' : 'bg-accent/50 text-muted-foreground hover:text-foreground border border-transparent hover:border-border/50'}"
							disabled={tab.isStreaming}
						>
							<span>{modeLabels[effectiveMode] || effectiveMode}</span>
							<svg class="w-2.5 h-2.5 opacity-60 transition-transform rotate-180 {showModePopup ? 'rotate-0' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7" />
							</svg>
						</button>
						{#if showModePopup}
							<button class="fixed inset-0 z-40" onclick={() => showModePopup = false} aria-label="Close"></button>
							<div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 bg-card border border-border rounded-xl shadow-xl overflow-hidden z-50 min-w-[100px]">
								{#each [['default', 'Ask'], ['acceptEdits', 'Auto-Accept'], ['plan', 'Plan'], ['bypassPermissions', 'Bypass']] as [value, label]}
									<button
										type="button"
										onclick={() => {
											if (value === profilePermissionMode) {
												tabs.setTabPermissionModeOverride(tab.id, null);
											} else {
												tabs.setTabPermissionModeOverride(tab.id, value);
											}
											showModePopup = false;
										}}
										class="w-full px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors flex items-center justify-between gap-3 {effectiveMode === value ? 'bg-accent/50 text-foreground font-medium' : 'text-muted-foreground'}"
									>
										<span>{label}</span>
										{#if effectiveMode === value}
											<svg class="w-3 h-3 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
											</svg>
										{/if}
									</button>
								{/each}
							</div>
						{/if}
					</div>
				{/if}

				<!-- Attach File Button -->
				<button
					type="button"
					onclick={triggerFileUpload}
					class="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium rounded-full bg-accent/50 text-muted-foreground hover:text-foreground border border-transparent hover:border-border/50 transition-all disabled:opacity-40"
					disabled={tab.isStreaming || !$claudeAuthenticated || isUploading}
					title={tab.project ? 'Attach file' : 'Select a project to attach files'}
				>
					{#if isUploading}
						<svg class="w-3 h-3 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
					{:else}
						<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
						</svg>
					{/if}
					<span>Attach</span>
				</button>

				<!-- Reset Button (Admin only) -->
				{#if $isAdmin && (tab.modelOverride || tab.permissionModeOverride)}
					<button
						type="button"
						onclick={() => {
							tabs.setTabModelOverride(tab.id, null);
							tabs.setTabPermissionModeOverride(tab.id, null);
						}}
						class="inline-flex items-center gap-1 px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground rounded-full hover:bg-accent/50 transition-all disabled:opacity-40"
						title="Reset to defaults"
						disabled={tab.isStreaming}
					>
						<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
						</svg>
						<span class="hidden sm:inline">Reset</span>
					</button>
				{/if}
			</div>

			<!-- Floating Island Input Container -->
			<div class="floating-island transition-all duration-200 focus-within:border-primary/40 focus-within:shadow-primary/10 focus-within:shadow-xl">

				<!-- Uploaded Files (inside container) -->
				{#if uploadedFiles.length > 0}
					<div class="px-3 pt-3 pb-0 flex flex-wrap gap-1.5">
						{#each uploadedFiles as file, index}
							<div class="flex items-center gap-1.5 bg-accent/50 text-xs px-2 py-1 rounded-lg group">
								<svg class="w-3.5 h-3.5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
								</svg>
								<span class="text-foreground truncate max-w-[100px]" title={file.path}>{file.filename}</span>
								<button
									type="button"
									onclick={() => removeUploadedFile(index)}
									class="text-muted-foreground hover:text-destructive opacity-60 group-hover:opacity-100 transition-opacity"
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

				<!-- Main Input Row -->
				<div class="flex items-center gap-1 p-2 sm:p-2.5">
					<!-- Textarea Container -->
					<div class="flex-1 relative min-w-0">
						<!-- Command Autocomplete -->
						<CommandAutocomplete
							bind:this={commandAutocompleteRef}
							inputValue={inputValue}
							projectId={tab.project}
							visible={showCommandAutocomplete}
							onSelect={handleCommandSelect}
							onClose={() => showCommandAutocomplete = false}
						/>

						<!-- File Autocomplete (@ mentions) -->
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
							class="w-full bg-transparent border-0 px-2 sm:px-3 py-2 text-foreground placeholder-muted-foreground/60 resize-none focus:outline-none focus:ring-0 min-h-[44px] max-h-[200px] leading-relaxed text-sm sm:text-base"
							rows="1"
							disabled={!$claudeAuthenticated}
						></textarea>
					</div>

					<!-- Voice/Send/Stop Buttons -->
					<div class="flex items-center gap-1">
						<!-- Voice Recording Button -->
						<button
							type="button"
							onclick={toggleRecording}
							disabled={!$claudeAuthenticated || isUploading || isTranscribing}
							class="w-9 h-9 sm:w-8 sm:h-8 flex items-center justify-center rounded-xl transition-all disabled:opacity-30 disabled:cursor-not-allowed {isRecording ? 'bg-destructive/20 text-destructive animate-pulse' : isTranscribing ? 'bg-primary/15 text-primary' : 'hover:bg-accent text-muted-foreground hover:text-foreground'}"
							title={isRecording ? 'Stop recording' : isTranscribing ? 'Transcribing...' : 'Voice input'}
						>
							{#if isTranscribing}
								<svg class="w-4 h-4 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
								</svg>
							{:else}
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
								</svg>
							{/if}
						</button>

						{#if tab.isStreaming}
							<!-- When streaming: show send button if text entered (interrupts then sends), otherwise show stop button -->
							{#if inputValue.trim()}
								<button
									type="submit"
									class="w-9 h-9 sm:w-8 sm:h-8 flex items-center justify-center bg-primary hover:bg-primary/90 text-primary-foreground rounded-xl transition-all"
									title="Send message (interrupts current response)"
								>
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 12h14M12 5l7 7-7 7" />
									</svg>
								</button>
							{:else}
								<button
									type="button"
									onclick={() => tabs.stopGeneration(tab.id)}
									class="w-9 h-9 sm:w-8 sm:h-8 flex items-center justify-center bg-destructive/15 text-destructive hover:bg-destructive/25 rounded-xl transition-all"
									title="Stop generating"
								>
									<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
										<rect x="6" y="6" width="12" height="12" rx="2" />
									</svg>
								</button>
							{/if}
						{:else}
							<!-- Send Button -->
							<button
								type="submit"
								class="w-9 h-9 sm:w-8 sm:h-8 flex items-center justify-center bg-primary hover:bg-primary/90 text-primary-foreground rounded-xl transition-all disabled:opacity-30 disabled:cursor-not-allowed"
								disabled={!inputValue.trim() || !$claudeAuthenticated || isUploading || isRecording || isTranscribing}
								title={isUploading ? "Uploading files..." : isRecording ? "Recording..." : isTranscribing ? "Transcribing..." : "Send message"}
							>
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 12h14M12 5l7 7-7 7" />
								</svg>
							</button>
						{/if}
					</div>
				</div>
			</div>
		</form>
	</div>
</div>

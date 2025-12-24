<script lang="ts">
	/**
	 * ChatInput - Modern chat input island with dropdowns and file upload
	 */
	import { tick, onDestroy, createEventDispatcher, onMount } from 'svelte';
	import { tabs, profiles, projects, type ChatTab, type Profile, type Project } from '$lib/stores/tabs';
	import { claudeAuthenticated, isAdmin, apiUser } from '$lib/stores/auth';
	import { api, type FileUploadResponse } from '$lib/api/client';
	import { groups, organizeByGroups } from '$lib/stores/groups';
	import CommandAutocomplete from '$lib/components/CommandAutocomplete.svelte';
	import FileAutocomplete, { type FileItem } from '$lib/components/FileAutocomplete.svelte';
	import DropdownContextMenu from './DropdownContextMenu.svelte';

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
		onOpenProfileCard?: (editId?: string) => void;
		onOpenProjectCard?: (editId?: string) => void;
	}

	let { tab, compact = false, onOpenTerminalModal, onOpenProfileCard, onOpenProjectCard }: Props = $props();

	const dispatch = createEventDispatcher<{
		openProfileCard: { editId?: string };
		openProjectCard: { editId?: string };
	}>();

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

	const autocompactBuffer = 45000;
	const contextUsed = $derived((tab.contextUsed ?? (tab.totalTokensIn + tab.totalCacheCreationTokens + tab.totalCacheReadTokens)) + autocompactBuffer);
	const contextMax = 200000;
	const contextPercent = $derived(Math.min((contextUsed / contextMax) * 100, 100));

	// Organize profiles and projects by groups
	const profilesOrganized = $derived(organizeByGroups($profiles, 'profiles', $groups));
	const hasProfileGroups = $derived(profilesOrganized.groupOrder.length > 0);
	const projectsOrganized = $derived(organizeByGroups($projects, 'projects', $groups));
	const hasProjectGroups = $derived(projectsOrganized.groupOrder.length > 0);

	// Context menu state for profiles
	let profileContextMenu = $state<{
		show: boolean;
		x: number;
		y: number;
		profile: Profile | null;
	}>({ show: false, x: 0, y: 0, profile: null });

	// Context menu state for projects
	let projectContextMenu = $state<{
		show: boolean;
		x: number;
		y: number;
		project: Project | null;
	}>({ show: false, x: 0, y: 0, project: null });

	function handleProfileContextMenu(e: MouseEvent, profile: Profile) {
		e.preventDefault();
		e.stopPropagation();
		profileContextMenu = {
			show: true,
			x: e.clientX,
			y: e.clientY,
			profile
		};
	}

	function handleProjectContextMenu(e: MouseEvent, project: Project) {
		e.preventDefault();
		e.stopPropagation();
		projectContextMenu = {
			show: true,
			x: e.clientX,
			y: e.clientY,
			project
		};
	}

	function closeProfileContextMenu() {
		profileContextMenu = { show: false, x: 0, y: 0, profile: null };
	}

	function closeProjectContextMenu() {
		projectContextMenu = { show: false, x: 0, y: 0, project: null };
	}

	// Profile context menu handlers
	function handleProfileSelect(e: CustomEvent<{ id: string }>) {
		tabs.setTabProfile(tab.id, e.detail.id);
	}

	function handleProfileEdit(e: CustomEvent<{ id: string }>) {
		onOpenProfileCard?.(e.detail.id);
		dispatch('openProfileCard', { editId: e.detail.id });
	}

	async function handleProfileDuplicate(e: CustomEvent<{ id: string }>) {
		const profile = $profiles.find(p => p.id === e.detail.id);
		if (!profile) return;

		const newId = `${profile.id}-copy-${Date.now().toString(36)}`;
		const newName = `${profile.name} (Copy)`;

		try {
			await tabs.createProfile({
				id: newId,
				name: newName,
				description: profile.description,
				config: profile.config || {}
			});
		} catch (err) {
			console.error('Failed to duplicate profile:', err);
		}
	}

	async function handleProfileDelete(e: CustomEvent<{ id: string }>) {
		if (confirm(`Delete profile "${$profiles.find(p => p.id === e.detail.id)?.name}"? This cannot be undone.`)) {
			await tabs.deleteProfile(e.detail.id);
		}
	}

	function handleProfileOpenCard(e: CustomEvent<{ type: 'profile' | 'project'; editId?: string }>) {
		onOpenProfileCard?.(e.detail.editId);
		dispatch('openProfileCard', { editId: e.detail.editId });
	}

	// Project context menu handlers
	function handleProjectSelect(e: CustomEvent<{ id: string }>) {
		tabs.setTabProject(tab.id, e.detail.id);
	}

	function handleProjectEdit(e: CustomEvent<{ id: string }>) {
		onOpenProjectCard?.(e.detail.id);
		dispatch('openProjectCard', { editId: e.detail.id });
	}

	async function handleProjectDelete(e: CustomEvent<{ id: string }>) {
		if (confirm(`Delete project "${$projects.find(p => p.id === e.detail.id)?.name}"? This cannot be undone.`)) {
			await tabs.deleteProject(e.detail.id);
		}
	}

	function handleProjectOpenCard(e: CustomEvent<{ type: 'profile' | 'project'; editId?: string }>) {
		onOpenProjectCard?.(e.detail.editId);
		dispatch('openProjectCard', { editId: e.detail.editId });
	}

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

	// Dropdown state - which dropdown is open
	let activeDropdown = $state<'profile' | 'project' | 'model' | 'mode' | 'context' | null>(null);

	// Dropdown container refs for positioning
	let profileContainerRef = $state<HTMLDivElement | null>(null);
	let projectContainerRef = $state<HTMLDivElement | null>(null);
	let modelContainerRef = $state<HTMLDivElement | null>(null);
	let modeContainerRef = $state<HTMLDivElement | null>(null);
	let contextContainerRef = $state<HTMLDivElement | null>(null);
	let islandRef = $state<HTMLDivElement | null>(null);

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

	// Toggle dropdown
	function toggleDropdown(dropdown: typeof activeDropdown) {
		activeDropdown = activeDropdown === dropdown ? null : dropdown;
	}

	// Close all dropdowns
	function closeAllDropdowns() {
		activeDropdown = null;
	}

	// Handle click outside to close dropdowns
	function handleClickOutside(e: MouseEvent) {
		if (!activeDropdown) return;

		const target = e.target as HTMLElement;
		const containers = [profileContainerRef, projectContainerRef, modelContainerRef, modeContainerRef, contextContainerRef];
		const isInsideDropdown = containers.some(ref => ref?.contains(target));

		if (!isInsideDropdown) {
			closeAllDropdowns();
		}
	}

	onMount(() => {
		document.addEventListener('click', handleClickOutside, true);
		return () => document.removeEventListener('click', handleClickOutside, true);
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

				<!-- Bottom Controls Bar -->
				<div class="controls-bar">
					<!-- Left: Context indicator -->
					<div class="controls-left">
						<div class="dropdown-container" bind:this={contextContainerRef}>
							<button
								type="button"
								class="control-pill context-pill"
								onclick={() => toggleDropdown('context')}
								title="Context usage"
							>
								<svg class="w-3.5 h-3.5 -rotate-90 {contextColor}" viewBox="0 0 20 20">
									<circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" stroke-width="2" opacity="0.2" />
									<circle
										cx="10" cy="10" r="8" fill="none"
										stroke="currentColor"
										stroke-width="2"
										stroke-dasharray={2 * Math.PI * 8}
										stroke-dashoffset={2 * Math.PI * 8 * (1 - contextPercent / 100)}
										stroke-linecap="round"
									/>
								</svg>
								<span class="hidden sm:inline">{Math.round(contextPercent)}%</span>
							</button>

							{#if activeDropdown === 'context'}
								<div class="dropdown-menu dropdown-up context-dropdown">
									<div class="dropdown-header">
										<span>Context Window</span>
										<span class="dropdown-header-value">{formatTokenCount(contextUsed)} / {formatTokenCount(contextMax)}</span>
									</div>
									<div class="dropdown-divider"></div>
									<div class="context-stats">
										<div class="stat-row">
											<span class="stat-label">
												<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16V4m0 0L3 8m4-4l4 4" />
												</svg>
												Input
											</span>
											<span class="stat-value">{formatTokenCount(tab.totalTokensIn)}</span>
										</div>
										<div class="stat-row">
											<span class="stat-label">
												<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 8v12m0 0l4-4m-4 4l-4-4" />
												</svg>
												Output
											</span>
											<span class="stat-value">{formatTokenCount(tab.totalTokensOut)}</span>
										</div>
										{#if tab.totalCacheCreationTokens > 0}
											<div class="stat-row">
												<span class="stat-label">
													<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
													</svg>
													Cache Create
												</span>
												<span class="stat-value">{formatTokenCount(tab.totalCacheCreationTokens)}</span>
											</div>
										{/if}
										{#if tab.totalCacheReadTokens > 0}
											<div class="stat-row">
												<span class="stat-label text-blue-400">
													<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
													</svg>
													Cache Read
												</span>
												<span class="stat-value text-blue-400">{formatTokenCount(tab.totalCacheReadTokens)}</span>
											</div>
										{/if}
									</div>
								</div>
							{/if}
						</div>
					</div>

					<!-- Center: Profile & Project selectors -->
					<div class="controls-center">
						<!-- Profile Selector -->
						{#if !isProfileLocked}
							<div class="dropdown-container" bind:this={profileContainerRef}>
								<button
									type="button"
									onclick={() => toggleDropdown('profile')}
									class="control-pill {tab.profile ? '' : 'unset'}"
									disabled={tab.isStreaming}
								>
									<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
									</svg>
									<span class="pill-text">{selectedProfileName}</span>
									<svg class="chevron {activeDropdown === 'profile' ? 'open' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
									</svg>
								</button>

								{#if activeDropdown === 'profile'}
									<div class="dropdown-menu dropdown-up">
										{#if $profiles.length === 0}
											<div class="dropdown-empty">No profiles</div>
										{:else}
											{#each profilesOrganized.groupOrder as groupItem}
												{@const groupProfiles = profilesOrganized.grouped.get(groupItem.name) || []}
												{#if groupProfiles.length > 0}
													<button
														type="button"
														class="dropdown-group-header"
														onclick={() => groups.toggleGroupCollapsed('profiles', groupItem.name)}
													>
														<svg class="w-3 h-3 transition-transform {groupItem.collapsed ? '-rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
														</svg>
														<span>{groupItem.name}</span>
														<span class="group-count">({groupProfiles.length})</span>
													</button>
													{#if !groupItem.collapsed}
														{#each groupProfiles as profile}
															<button
																type="button"
																onclick={() => {
																	tabs.setTabProfile(tab.id, profile.id);
																	closeAllDropdowns();
																}}
																oncontextmenu={(e) => handleProfileContextMenu(e, profile)}
																class="dropdown-item {tab.profile === profile.id ? 'active' : ''}"
															>
																<span class="item-text">{profile.name}</span>
																{#if tab.profile === profile.id}
																	<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																		<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
																	</svg>
																{/if}
															</button>
														{/each}
													{/if}
												{/if}
											{/each}
											{#if profilesOrganized.ungrouped.length > 0}
												{#if hasProfileGroups}
													<div class="dropdown-group-header static">
														<span>Other</span>
													</div>
												{/if}
												{#each profilesOrganized.ungrouped as profile}
													<button
														type="button"
														onclick={() => {
															tabs.setTabProfile(tab.id, profile.id);
															closeAllDropdowns();
														}}
														oncontextmenu={(e) => handleProfileContextMenu(e, profile)}
														class="dropdown-item {tab.profile === profile.id ? 'active' : ''}"
													>
														<span class="item-text">{profile.name}</span>
														{#if tab.profile === profile.id}
															<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
															</svg>
														{/if}
													</button>
												{/each}
											{/if}
										{/if}
									</div>
								{/if}
							</div>
						{:else}
							<div class="control-pill locked">
								<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
								</svg>
								<span class="pill-text">{selectedProfileName}</span>
								<svg class="w-3 h-3 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
								</svg>
							</div>
						{/if}

						<!-- Project Selector -->
						{#if !isProjectLocked}
							<div class="dropdown-container" bind:this={projectContainerRef}>
								<button
									type="button"
									onclick={() => toggleDropdown('project')}
									class="control-pill {tab.project ? '' : 'unset'}"
									disabled={tab.isStreaming}
								>
									<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
									</svg>
									<span class="pill-text">{selectedProjectName}</span>
									<svg class="chevron {activeDropdown === 'project' ? 'open' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
									</svg>
								</button>

								{#if activeDropdown === 'project'}
									<div class="dropdown-menu dropdown-up">
										{#if $projects.length === 0}
											<div class="dropdown-empty">No projects</div>
										{:else}
											{#each projectsOrganized.groupOrder as groupItem}
												{@const groupProjects = projectsOrganized.grouped.get(groupItem.name) || []}
												{#if groupProjects.length > 0}
													<button
														type="button"
														class="dropdown-group-header"
														onclick={() => groups.toggleGroupCollapsed('projects', groupItem.name)}
													>
														<svg class="w-3 h-3 transition-transform {groupItem.collapsed ? '-rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
														</svg>
														<span>{groupItem.name}</span>
														<span class="group-count">({groupProjects.length})</span>
													</button>
													{#if !groupItem.collapsed}
														{#each groupProjects as project}
															<button
																type="button"
																onclick={() => {
																	tabs.setTabProject(tab.id, project.id);
																	closeAllDropdowns();
																}}
																oncontextmenu={(e) => handleProjectContextMenu(e, project)}
																class="dropdown-item {tab.project === project.id ? 'active' : ''}"
															>
																<span class="item-text">{project.name}</span>
																{#if tab.project === project.id}
																	<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																		<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
																	</svg>
																{/if}
															</button>
														{/each}
													{/if}
												{/if}
											{/each}
											{#if projectsOrganized.ungrouped.length > 0}
												{#if hasProjectGroups}
													<div class="dropdown-group-header static">
														<span>Other</span>
													</div>
												{/if}
												{#each projectsOrganized.ungrouped as project}
													<button
														type="button"
														onclick={() => {
															tabs.setTabProject(tab.id, project.id);
															closeAllDropdowns();
														}}
														oncontextmenu={(e) => handleProjectContextMenu(e, project)}
														class="dropdown-item {tab.project === project.id ? 'active' : ''}"
													>
														<span class="item-text">{project.name}</span>
														{#if tab.project === project.id}
															<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
															</svg>
														{/if}
													</button>
												{/each}
											{/if}
										{/if}
									</div>
								{/if}
							</div>
						{:else}
							<div class="control-pill locked">
								<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
								</svg>
								<span class="pill-text">{selectedProjectName}</span>
								<svg class="w-3 h-3 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
								</svg>
							</div>
						{/if}
					</div>

					<!-- Right: Model & Mode (Admin only) -->
					<div class="controls-right">
						{#if $isAdmin}
							<!-- Model Selector -->
							<div class="dropdown-container" bind:this={modelContainerRef}>
								<button
									type="button"
									onclick={() => toggleDropdown('model')}
									class="control-pill compact {tab.modelOverride ? 'override' : ''}"
									disabled={tab.isStreaming}
								>
									<span>{modelLabels[effectiveModel] || effectiveModel}</span>
									<svg class="chevron {activeDropdown === 'model' ? 'open' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
									</svg>
								</button>

								{#if activeDropdown === 'model'}
									<div class="dropdown-menu dropdown-up dropdown-right">
										{#each [['sonnet', 'Sonnet'], ['sonnet-1m', 'Sonnet 1M'], ['opus', 'Opus'], ['haiku', 'Haiku']] as [value, label]}
											<button
												type="button"
												onclick={() => {
													if (value === profileModel) {
														tabs.setTabModelOverride(tab.id, null);
													} else {
														tabs.setTabModelOverride(tab.id, value);
													}
													closeAllDropdowns();
												}}
												class="dropdown-item {effectiveModel === value ? 'active' : ''}"
											>
												<span class="item-text">{label}</span>
												{#if effectiveModel === value}
													<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
													</svg>
												{/if}
											</button>
										{/each}
									</div>
								{/if}
							</div>

							<!-- Mode Selector -->
							<div class="dropdown-container" bind:this={modeContainerRef}>
								<button
									type="button"
									onclick={() => toggleDropdown('mode')}
									class="control-pill compact {tab.permissionModeOverride ? 'override' : ''}"
									disabled={tab.isStreaming}
								>
									<span>{modeLabels[effectiveMode] || effectiveMode}</span>
									<svg class="chevron {activeDropdown === 'mode' ? 'open' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
									</svg>
								</button>

								{#if activeDropdown === 'mode'}
									<div class="dropdown-menu dropdown-up dropdown-right">
										{#each [['default', 'Ask'], ['acceptEdits', 'Auto-Accept'], ['plan', 'Plan'], ['bypassPermissions', 'Bypass']] as [value, label]}
											<button
												type="button"
												onclick={() => {
													if (value === profilePermissionMode) {
														tabs.setTabPermissionModeOverride(tab.id, null);
													} else {
														tabs.setTabPermissionModeOverride(tab.id, value);
													}
													closeAllDropdowns();
												}}
												class="dropdown-item {effectiveMode === value ? 'active' : ''}"
											>
												<span class="item-text">{label}</span>
												{#if effectiveMode === value}
													<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
													</svg>
												{/if}
											</button>
										{/each}
									</div>
								{/if}
							</div>

							<!-- Reset Button -->
							{#if tab.modelOverride || tab.permissionModeOverride}
								<button
									type="button"
									onclick={() => {
										tabs.setTabModelOverride(tab.id, null);
										tabs.setTabPermissionModeOverride(tab.id, null);
									}}
									class="reset-btn"
									title="Reset to defaults"
									disabled={tab.isStreaming}
								>
									<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
									</svg>
								</button>
							{/if}
						{/if}
					</div>
				</div>
			</div>
		</form>
	</div>
</div>

<!-- Profile Context Menu -->
{#if profileContextMenu.show && profileContextMenu.profile}
	<DropdownContextMenu
		show={profileContextMenu.show}
		x={profileContextMenu.x}
		y={profileContextMenu.y}
		itemId={profileContextMenu.profile.id}
		itemName={profileContextMenu.profile.name}
		itemType="profile"
		isSelected={tab.profile === profileContextMenu.profile.id}
		isBuiltin={profileContextMenu.profile.is_builtin}
		currentGroup={$groups.profiles.assignments[profileContextMenu.profile.id]}
		onClose={closeProfileContextMenu}
		on:select={handleProfileSelect}
		on:edit={handleProfileEdit}
		on:duplicate={handleProfileDuplicate}
		on:delete={handleProfileDelete}
		on:openCard={handleProfileOpenCard}
	/>
{/if}

<!-- Project Context Menu -->
{#if projectContextMenu.show && projectContextMenu.project}
	<DropdownContextMenu
		show={projectContextMenu.show}
		x={projectContextMenu.x}
		y={projectContextMenu.y}
		itemId={projectContextMenu.project.id}
		itemName={projectContextMenu.project.name}
		itemType="project"
		isSelected={tab.project === projectContextMenu.project.id}
		isBuiltin={false}
		currentGroup={$groups.projects.assignments[projectContextMenu.project.id]}
		onClose={closeProjectContextMenu}
		on:select={handleProjectSelect}
		on:edit={handleProjectEdit}
		on:delete={handleProjectDelete}
		on:openCard={handleProjectOpenCard}
	/>
{/if}

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

	/* Controls bar */
	.controls-bar {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.375rem 0.625rem;
		border-top: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
	}

	@media (min-width: 640px) {
		.controls-bar {
			padding: 0.5rem 0.75rem;
		}
	}

	.controls-left {
		flex-shrink: 0;
	}

	.controls-center {
		flex: 1;
		display: flex;
		justify-content: center;
		gap: 0.375rem;
		flex-wrap: wrap;
	}

	.controls-right {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		gap: 0.375rem;
	}

	/* Dropdown container */
	.dropdown-container {
		position: relative;
	}

	/* Control pill buttons */
	.control-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.3125rem 0.625rem;
		font-size: 0.6875rem;
		font-weight: 500;
		color: var(--muted-foreground);
		background: color-mix(in srgb, var(--accent) 50%, transparent);
		border: 1px solid transparent;
		border-radius: 9999px;
		transition: all 0.15s;
		white-space: nowrap;
	}

	.control-pill:hover:not(:disabled) {
		background: var(--accent);
		color: var(--foreground);
		border-color: color-mix(in srgb, var(--border) 50%, transparent);
	}

	.control-pill:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.control-pill.unset {
		background: color-mix(in srgb, var(--warning) 15%, transparent);
		color: var(--warning);
		border-color: color-mix(in srgb, var(--warning) 30%, transparent);
	}

	.control-pill.unset:hover:not(:disabled) {
		background: color-mix(in srgb, var(--warning) 25%, transparent);
	}

	.control-pill.override {
		background: color-mix(in srgb, var(--primary) 15%, transparent);
		color: var(--primary);
		border-color: color-mix(in srgb, var(--primary) 30%, transparent);
	}

	.control-pill.locked {
		opacity: 0.6;
		cursor: default;
	}

	.control-pill.compact {
		padding: 0.3125rem 0.5rem;
	}

	.control-pill.context-pill {
		gap: 0.25rem;
		padding: 0.3125rem 0.5rem;
	}

	.pill-text {
		max-width: 80px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	@media (min-width: 640px) {
		.pill-text {
			max-width: 100px;
		}
	}

	.chevron {
		width: 0.625rem;
		height: 0.625rem;
		opacity: 0.5;
		transition: transform 0.2s;
	}

	.chevron.open {
		transform: rotate(180deg);
	}

	/* Dropdown menu */
	.dropdown-menu {
		position: absolute;
		z-index: 100;
		min-width: 140px;
		max-height: 260px;
		overflow-y: auto;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 0.75rem;
		box-shadow: 0 4px 16px -2px rgba(0, 0, 0, 0.12), 0 2px 4px rgba(0, 0, 0, 0.06);
		animation: dropdownIn 0.15s ease-out;
	}

	.dropdown-menu.dropdown-up {
		bottom: calc(100% + 0.375rem);
		left: 50%;
		transform: translateX(-50%);
	}

	.dropdown-menu.dropdown-right {
		left: auto;
		right: 0;
		transform: none;
	}

	@keyframes dropdownIn {
		from {
			opacity: 0;
			transform: translateX(-50%) translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateX(-50%) translateY(0);
		}
	}

	.dropdown-menu.dropdown-right {
		animation-name: dropdownInRight;
	}

	@keyframes dropdownInRight {
		from {
			opacity: 0;
			transform: translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	:global(.dark) .dropdown-menu {
		background: oklch(0.18 0.01 260);
		border-color: oklch(0.3 0.01 260);
	}

	.dropdown-empty {
		padding: 0.625rem 0.75rem;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.dropdown-group-header {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		width: 100%;
		padding: 0.375rem 0.625rem;
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
		background: color-mix(in srgb, var(--muted) 30%, transparent);
		transition: color 0.15s;
	}

	.dropdown-group-header:hover {
		color: var(--foreground);
	}

	.dropdown-group-header.static {
		cursor: default;
	}

	.group-count {
		margin-left: auto;
		opacity: 0.6;
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		width: 100%;
		padding: 0.5rem 0.75rem;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		text-align: left;
		transition: background-color 0.1s, color 0.1s;
	}

	.dropdown-item:hover {
		background: var(--accent);
		color: var(--foreground);
	}

	.dropdown-item.active {
		background: color-mix(in srgb, var(--accent) 60%, transparent);
		color: var(--foreground);
		font-weight: 500;
	}

	.item-text {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.check-icon {
		flex-shrink: 0;
		width: 0.875rem;
		height: 0.875rem;
		color: var(--primary);
	}

	/* Context dropdown specific styles */
	.context-dropdown {
		min-width: 180px;
		padding: 0.5rem 0;
	}

	.dropdown-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.375rem 0.75rem;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.dropdown-header-value {
		font-weight: 500;
		color: var(--foreground);
	}

	.dropdown-divider {
		height: 1px;
		margin: 0.375rem 0;
		background: var(--border);
	}

	.context-stats {
		padding: 0.25rem 0.75rem;
	}

	.stat-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.25rem 0;
		font-size: 0.6875rem;
	}

	.stat-label {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		color: var(--muted-foreground);
	}

	.stat-value {
		font-weight: 500;
		color: var(--foreground);
	}

	/* Reset button */
	.reset-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		border-radius: 9999px;
		color: var(--muted-foreground);
		transition: background-color 0.15s, color 0.15s;
	}

	.reset-btn:hover:not(:disabled) {
		background: var(--accent);
		color: var(--foreground);
	}

	.reset-btn:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}
</style>

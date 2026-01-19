<script lang="ts">
	/**
	 * ChatInput - Modern chat input island with progressive disclosure
	 *
	 * Layer 1: Minimal - textarea + essential buttons
	 * Layer 2: Context Bar - inline profile/project dropdown selectors
	 */
	import { tick, onDestroy, onMount } from 'svelte';
	import { tabs, profiles, projects, allTabs, type ChatTab } from '$lib/stores/tabs';
	import { claudeAuthenticated, apiUser } from '$lib/stores/auth';
	import { canUseOpenAI, credentialsLoaded } from '$lib/stores/credentials';
	import { api, type FileUploadResponse } from '$lib/api/client';
	import CommandAutocomplete from '$lib/components/CommandAutocomplete.svelte';
	import FileAutocomplete, { type FileItem } from '$lib/components/FileAutocomplete.svelte';
	import { AUTO_COMPACTION_BASE, CONTEXT_MAX } from '$lib/constants/chat';
	import TiptapEditor from './TiptapEditor.svelte';
	import { hasAtMention, findLastAtMentionIndex } from './tiptap/serialization';

	interface Command {
		name: string;
		display: string;
		description: string;
		argument_hint?: string;
		type: 'custom' | 'interactive' | 'sdk_builtin' | 'plugin';
		source?: string;
		namespace?: string;
	}

	interface Props {
		tab: ChatTab;
		compact?: boolean;
		onOpenTerminalModal?: (tabId: string, command: string) => void;
	}

	let { tab, compact = false, onOpenTerminalModal }: Props = $props();

	// Get live tab data directly from store to ensure reactivity in mobile snippet context
	// The prop `tab` may not update properly when rendered via {@render} inside {#key} blocks
	// Use $state + $effect instead of $derived for more explicit reactivity tracking
	let liveTab = $state<ChatTab>(tab);

	$effect(() => {
		// Subscribe to allTabs and update liveTab when the store changes
		const foundTab = $allTabs.find(t => t.id === tab.id);
		if (foundTab) {
			liveTab = foundTab;
		}
	});

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
	const currentProfile = $derived($profiles.find(p => p.id === liveTab.profile));

	// Auto-compaction reserve: 13k base + CLAUDE_CODE_MAX_OUTPUT_TOKENS from profile env vars
	const maxOutputTokens = $derived(
		parseInt((currentProfile?.config as any)?.env?.CLAUDE_CODE_MAX_OUTPUT_TOKENS || '0', 10) || 0
	);
	const autoCompactionReserve = $derived(AUTO_COMPACTION_BASE + maxOutputTokens);

	// Context usage: use real-time tracked value, or calculate from token counts for resumed sessions
	// Add auto-compaction reserve to show effective context usage
	// Use liveTab for reactivity - ensures updates propagate in mobile context
	const baseContextUsed = $derived(liveTab.contextUsed ?? (liveTab.totalTokensIn + liveTab.totalCacheCreationTokens + liveTab.totalCacheReadTokens));
	const contextUsed = $derived(baseContextUsed + autoCompactionReserve);
	const contextPercent = $derived(Math.min((contextUsed / CONTEXT_MAX) * 100, 100));


	// Input state - use tab.draft for persistence across card switches
	let inputValue = $state(tab.draft || '');
	let plainTextValue = $state(''); // Plain text without chip serialization, for autocomplete
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
	let editorRef = $state<TiptapEditor | null>(null);
	let fileInputRef = $state<HTMLInputElement | null>(null);
	let isUploading = $state(false);

	// Voice recording state
	let isRecording = $state(false);
	let isTranscribing = $state(false);
	let isPreparing = $state(false); // New: getting mic access
	let mediaRecorder = $state<MediaRecorder | null>(null);
	let recordingError = $state('');
	let recordingStartTime = $state<number>(0);
	let audioLevel = $state(0); // For audio level visualization (0-1)
	let analyserNode = $state<AnalyserNode | null>(null);
	let audioContext = $state<AudioContext | null>(null);
	let animationFrameId = $state<number | null>(null);

	// Use a non-reactive array for audio chunks to avoid Svelte 5 reactivity issues
	// with rapid MediaRecorder ondataavailable events
	let audioChunksRef: Blob[] = [];

	// Check if recording is available based on OpenAI key policy
	// For API users, they must have OpenAI key configured (either their own or admin-provided)
	// For admins, we assume OpenAI is available (they control the keys)
	const canRecord = $derived(
		!$apiUser || // Admins can always record
		!$credentialsLoaded || // Before credentials load, allow (will fail gracefully if no key)
		$canUseOpenAI // API users need OpenAI key
	);

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

	// Override dropdown state
	let showThinkingDropdown = $state(false);
	let showPermissionDropdown = $state(false);
	let thinkingDropdownRef = $state<HTMLDivElement | null>(null);
	let permissionDropdownRef = $state<HTMLDivElement | null>(null);

	// Thinking mode options
	const thinkingModes = [
		{ value: null, label: 'Default', description: 'Use profile setting' },
		{ value: 'default', label: 'Off', description: 'No extended thinking' },
		{ value: 'think', label: 'Think', description: '~8k tokens' },
		{ value: 'think_harder', label: 'Think Harder', description: '~24k tokens' },
		{ value: 'ultra_think', label: 'Ultra Think', description: '~64k tokens' }
	];

	// Permission mode options
	const permissionModes = [
		{ value: null, label: 'Default', description: 'Use profile setting' },
		{ value: 'default', label: 'Ask', description: 'Confirm before tools' },
		{ value: 'acceptEdits', label: 'Accept Edits', description: 'Auto-approve edits' },
		{ value: 'plan', label: 'Plan Only', description: 'Plan without executing' },
		{ value: 'bypassPermissions', label: 'Bypass', description: 'Execute all tools' }
	];

	// Get display labels for current overrides
	const currentThinkingLabel = $derived(
		thinkingModes.find(m => m.value === liveTab.thinkingModeOverride)?.label || 'Default'
	);
	const currentPermissionLabel = $derived(
		permissionModes.find(m => m.value === liveTab.permissionModeOverride)?.label || 'Default'
	);

	// Get selected profile/project names for display
	const selectedProfileName = $derived($profiles.find(p => p.id === liveTab.profile)?.name || 'Profile');
	const selectedProjectName = $derived($projects.find(p => p.id === liveTab.project)?.name || 'Project');

	// Mobile-optimized names: show only first word to save space
	const selectedProfileNameMobile = $derived(selectedProfileName.split(' ')[0]);
	const selectedProjectNameMobile = $derived(selectedProjectName.split(' ')[0]);

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

	// Toggle override dropdowns
	function handleThinkingClick(e: MouseEvent) {
		e.stopPropagation();
		showPermissionDropdown = false;
		showProfileDropdown = false;
		showProjectDropdown = false;
		showThinkingDropdown = !showThinkingDropdown;
	}

	function handlePermissionClick(e: MouseEvent) {
		e.stopPropagation();
		showThinkingDropdown = false;
		showProfileDropdown = false;
		showProjectDropdown = false;
		showPermissionDropdown = !showPermissionDropdown;
	}

	// Select override values
	function selectThinkingMode(value: string | null) {
		tabs.setTabThinkingModeOverride(tab.id, value);
		showThinkingDropdown = false;
	}

	function selectPermissionMode(value: string | null) {
		tabs.setTabPermissionModeOverride(tab.id, value);
		showPermissionDropdown = false;
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
		if (thinkingDropdownRef && !thinkingDropdownRef.contains(target)) {
			showThinkingDropdown = false;
		}
		if (permissionDropdownRef && !permissionDropdownRef.contains(target)) {
			showPermissionDropdown = false;
		}
	}

	// Close dropdowns on escape
	function handleEscape(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			showProfileDropdown = false;
			showProjectDropdown = false;
			showThinkingDropdown = false;
			showPermissionDropdown = false;
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

	// Handle input changes for autocomplete
	function handleInputChange() {
		showCommandAutocomplete = inputValue.startsWith('/') && inputValue.length > 0;
		// Use plain text for @ mention detection to avoid false positives from chip @paths
		// inputValue contains serialized content where chips are @path, which would trigger autocomplete incorrectly
		showFileAutocomplete = hasAtMention(plainTextValue) && !!liveTab.project;
	}

	// Handle form submission
	async function handleSubmit() {
		// Get serialized message from editor (includes inline @path references)
		const finalPrompt = editorRef?.getMessage() ?? '';

		if (!finalPrompt.trim() || !liveTab || isUploading) return;

		// Require profile and project to be selected
		if (!liveTab.profile) {
			tabs.setTabError(liveTab.id, 'Please select a profile before starting a chat');
			return;
		}
		if (!liveTab.project) {
			tabs.setTabError(liveTab.id, 'Please select a project before starting a chat');
			return;
		}

		// Append any legacy uploaded files (from file upload button) to the end
		// Note: Files added via @ autocomplete are now inline chips, not in uploadedFiles
		let messageToSend = finalPrompt;
		if (uploadedFiles.length > 0) {
			const fileRefs = uploadedFiles.map(f => `@${f.path}`).join(' ');
			messageToSend = finalPrompt.trim() + '\n\n' + fileRefs;
		}

		// Send the message
		sendMessage(messageToSend);
	}

	// Actually send the message
	function sendMessage(finalPrompt: string) {
		inputValue = '';
		uploadedFiles = [];

		// Clear editor content
		editorRef?.clear();

		// Clear draft immediately (not debounced) - important because
		// the component may be destroyed/recreated when isEmptyState changes
		saveDraftImmediately(tab.id, '');

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
			editorRef?.setContent(`/${command.name} `);
			await tick();
			editorRef?.focus();
		}
	}

	// Handle file selection (including chat history and note items)
	function handleFileSelect(file: FileItem) {
		// For directories, replace @ mention with path (with trailing /) and keep autocomplete open
		if (file.type === 'directory') {
			// Handle empty path (going back to root) - just show @
			const newPath = file.path ? (file.path.endsWith('/') ? file.path : file.path + '/') : '';
			editorRef?.replaceAtMention(newPath);
			// Update inputValue to keep autocomplete state in sync
			const atStartIndex = findLastAtMentionIndex(inputValue);
			if (atStartIndex !== -1) {
				inputValue = inputValue.substring(0, atStartIndex) + '@' + newPath;
			}
			tick().then(() => editorRef?.focus());
			return;
		}

		// For files, chat history, notes, and subagents, insert as inline chip
		const chipType = file.type === 'chat_history' ? 'chat_history' :
						 file.type === 'note' ? 'note' :
						 file.type === 'subagent' ? 'subagent' : 'file';

		editorRef?.insertFileChipReplacingMention({
			path: file.path,
			filename: file.name,
			fullPath: file.path,
			size: file.size || 0,
			type: chipType
		});

		// Update inputValue to reflect text after chip insertion (for autocomplete state)
		inputValue = editorRef?.getText() ?? '';
		showFileAutocomplete = false;

		tick().then(() => editorRef?.focus());
	}


	// File upload
	function triggerFileUpload() {
		if (!liveTab.project) {
			alert('Please select a project first to upload files.');
			return;
		}
		fileInputRef?.click();
	}

	async function handleFileUpload(event: Event) {
		const input = event.target as HTMLInputElement;
		const files = input.files;
		if (!files || files.length === 0 || !liveTab.project) return;

		isUploading = true;
		try {
			for (const file of Array.from(files)) {
				const result = await api.uploadFile(`/projects/${liveTab.project}/upload`, file);
				// Insert as inline chip at cursor position
				editorRef?.insertFileChip({
					path: result.path,
					filename: result.filename,
					fullPath: result.full_path,
					size: result.size,
					type: 'file'
				});
			}
			// Update inputValue for state sync
			inputValue = editorRef?.getText() ?? '';
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
	// Minimum recording duration in milliseconds to avoid empty/too-short recordings
	const MIN_RECORDING_DURATION = 500;

	// Audio level monitoring for visualization
	// Returns a promise that resolves when the audio system is ready to capture
	// The returned function starts the visualization loop (call after isRecording = true)
	async function initAudioMonitoring(stream: MediaStream): Promise<(() => void) | null> {
		try {
			audioContext = new AudioContext();

			// If AudioContext is suspended (common on some browsers), resume it
			if (audioContext.state === 'suspended') {
				await audioContext.resume();
			}

			const source = audioContext.createMediaStreamSource(stream);
			analyserNode = audioContext.createAnalyser();
			analyserNode.fftSize = 256;
			analyserNode.smoothingTimeConstant = 0.5;
			source.connect(analyserNode);

			const dataArray = new Uint8Array(analyserNode.frequencyBinCount);

			// Wait for AudioContext to be in 'running' state
			// This is the proper API signal that the audio system is ready
			if (audioContext.state !== 'running') {
				const ctx = audioContext; // Capture reference for closure
				await new Promise<void>((resolve) => {
					const handleStateChange = () => {
						if (ctx.state === 'running') {
							ctx.removeEventListener('statechange', handleStateChange);
							resolve();
						}
					};
					ctx.addEventListener('statechange', handleStateChange);

					// Safety timeout - resolve after 1s even if state never changes
					// (shouldn't happen in practice, but prevents infinite hang)
					setTimeout(() => {
						ctx.removeEventListener('statechange', handleStateChange);
						resolve();
					}, 1000);
				});
			}

			// Return a function to start the visualization loop
			// This should be called AFTER isRecording is set to true
			return function startVisualization() {
				function updateLevel() {
					if (!analyserNode || !isRecording) {
						audioLevel = 0;
						return;
					}
					analyserNode.getByteFrequencyData(dataArray);
					// Calculate average level
					const average = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
					// Normalize to 0-1 range with some amplification for better visual feedback
					audioLevel = Math.min(1, average / 128);
					animationFrameId = requestAnimationFrame(updateLevel);
				}
				updateLevel();
			};
		} catch (e) {
			console.warn('Audio level monitoring not available:', e);
			return null;
		}
	}

	function stopAudioLevelMonitoring() {
		if (animationFrameId) {
			cancelAnimationFrame(animationFrameId);
			animationFrameId = null;
		}
		if (audioContext) {
			audioContext.close().catch(() => {});
			audioContext = null;
		}
		analyserNode = null;
		audioLevel = 0;
	}

	async function startRecording() {
		recordingError = '';
		audioChunksRef = []; // Reset chunks array
		isPreparing = true; // Show preparing state while getting mic and initializing audio

		try {
			const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

			// Determine best supported mime type
			const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
				? 'audio/webm;codecs=opus'
				: MediaRecorder.isTypeSupported('audio/webm')
					? 'audio/webm'
					: 'audio/mp4';

			const recorder = new MediaRecorder(stream, { mimeType });

			// Collect audio chunks - use non-reactive array to avoid issues
			recorder.ondataavailable = (event) => {
				if (event.data.size > 0) {
					audioChunksRef.push(event.data);
				}
			};

			// Start recording with timeslice for chunked data collection
			// This improves reliability by not buffering all audio at once
			recorder.start(1000); // 1 second chunks
			mediaRecorder = recorder;
			recordingStartTime = Date.now();

			// Initialize audio monitoring and wait for the audio system to be ready
			// This ensures the microphone is actively capturing before showing the recording UI
			// Prevents the issue where users start speaking before the system is ready
			const startVisualization = await initAudioMonitoring(stream);

			// Only now show the recording UI - system is ready to capture speech
			isPreparing = false;
			isRecording = true;

			// Start the visualization loop now that isRecording is true
			if (startVisualization) {
				startVisualization();
			}
		} catch (error: unknown) {
			isPreparing = false;
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

	async function stopRecording() {
		// Stop audio level monitoring first
		stopAudioLevelMonitoring();

		if (!mediaRecorder || mediaRecorder.state === 'inactive') {
			isRecording = false;
			return;
		}

		const recorder = mediaRecorder;
		const stream = recorder.stream;
		const mimeType = recorder.mimeType;

		// Check minimum recording duration
		const recordingDuration = Date.now() - recordingStartTime;
		if (recordingDuration < MIN_RECORDING_DURATION) {
			recordingError = 'Recording too short. Please hold the button longer.';
			recorder.stop();
			stream.getTracks().forEach(track => track.stop());
			isRecording = false;
			return;
		}

		isRecording = false;

		// Use a Promise-based approach to ensure all data is collected before processing
		// This fixes the race condition between ondataavailable and onstop
		try {
			const audioBlob = await new Promise<Blob>((resolve, reject) => {
				const handleDataAvailable = (event: BlobEvent) => {
					if (event.data.size > 0) {
						audioChunksRef.push(event.data);
					}
				};

				const handleStop = () => {
					// Clean up
					recorder.removeEventListener('dataavailable', handleDataAvailable);
					recorder.removeEventListener('stop', handleStop);
					stream.getTracks().forEach(track => track.stop());

					// Check if we got any audio
					if (audioChunksRef.length === 0) {
						reject(new Error('No audio recorded'));
						return;
					}

					// Create blob from all collected chunks
					resolve(new Blob(audioChunksRef, { type: mimeType }));
				};

				// Add listeners before stopping to catch final data
				recorder.addEventListener('dataavailable', handleDataAvailable);
				recorder.addEventListener('stop', handleStop);

				// Request final data and stop - this ensures ondataavailable fires
				// for any remaining buffered audio before onstop
				recorder.requestData();
				recorder.stop();
			});

			await transcribeAudio(audioBlob);
		} catch (error: unknown) {
			const err = error as { message?: string };
			recordingError = err.message || 'Failed to process recording';
		}
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
				// Append transcribed text to editor
				editorRef?.appendText(transcribedText);
				inputValue = editorRef?.getText() ?? '';
				await tick();
				editorRef?.focus();
			}
		} catch (error: unknown) {
			console.error('Transcription failed:', error);
			const err = error as { message?: string };
			recordingError = err.message || 'Failed to transcribe audio';
		} finally {
			isTranscribing = false;
		}
	}

	async function toggleRecording() {
		if (isRecording) {
			await stopRecording();
		} else {
			await startRecording();
		}
	}

	// Context color - always themed (no usage-based color changes)
	const contextColor = $derived('');
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

		<!-- Worktree Error Message -->
		{#if liveTab.worktreeError}
			<div class="error-banner worktree-error">
				<svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<circle cx="6" cy="6" r="2" stroke-width="2"/>
					<circle cx="12" cy="18" r="2" stroke-width="2"/>
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 8v4c0 2 2 4 6 4v2" />
				</svg>
				<span>{liveTab.worktreeError}</span>
				<button type="button" onclick={() => tabs.clearWorktreeError(tab.id)} class="error-close">&times;</button>
			</div>
		{/if}

		<form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
			<!-- Main Island -->
			<div class="chat-island" bind:this={islandRef}>
				<!-- Input Wrapper - The Hero (outer container with border) -->
				<div class="input-wrapper">
					<!-- Input Inner - Dark background container -->
					<div class="input-inner">
						<!-- Textarea with autocomplete -->
						<div class="textarea-wrapper">
							<CommandAutocomplete
								bind:this={commandAutocompleteRef}
								inputValue={inputValue}
								projectId={liveTab.project}
								visible={showCommandAutocomplete}
								onSelect={handleCommandSelect}
								onClose={() => showCommandAutocomplete = false}
							/>

							<FileAutocomplete
								bind:this={fileAutocompleteRef}
								inputValue={plainTextValue}
								projectId={liveTab.project}
								profileId={liveTab.profile}
								worktreeId={liveTab.executionMode === 'worktree' ? liveTab.worktreeId ?? undefined : undefined}
								visible={showFileAutocomplete}
								onSelect={handleFileSelect}
								onClose={() => showFileAutocomplete = false}
							/>

							<TiptapEditor
								bind:this={editorRef}
								placeholder="Message {selectedProfileName}..."
								initialContent={tab.draft || ''}
								disabled={!$claudeAuthenticated}
								onTextChange={(text) => {
									inputValue = text;
									// Also update plain text for autocomplete (without chip @path serialization)
									plainTextValue = editorRef?.getText() ?? '';
									handleInputChange();
								}}
								onKeyDown={handleKeyDown}
							/>
						</div>

						<!-- Send button only -->
						<div class="send-button-wrapper">
							{#if liveTab.isCreatingWorktree}
								<!-- Creating worktree - show spinner -->
								<button type="button" class="send-btn creating-worktree" disabled title="Creating worktree...">
									<svg class="w-[14px] h-[14px] animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
									</svg>
								</button>
							{:else if liveTab.isStreaming}
								{#if inputValue.trim()}
									<button type="submit" class="send-btn" title="Send message (interrupts current response)">
										<svg class="w-[14px] h-[14px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 12h14M12 5l7 7-7 7" />
										</svg>
									</button>
								{:else}
									<button
										type="button"
										onclick={() => tabs.stopGeneration(liveTab.id)}
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

					<!-- Center: Profile selector + Project selector OR Worktree badge -->
					<div class="controls-center">
						<!-- Profile Selector - always visible -->
						<div class="selector-wrapper" bind:this={profileDropdownRef}>
							<button
								type="button"
								class="context-selector-btn {liveTab.profile ? '' : 'unset'}"
								onclick={handleProfileClick}
								title="Change profile"
							>
								<div class="selector-icon">
									<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
									</svg>
								</div>
								<span class="selector-text desktop">{selectedProfileName}</span>
								<span class="selector-text mobile">{selectedProfileNameMobile}</span>
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
												class:selected={profile.id === liveTab.profile}
												onclick={() => selectProfile(profile.id)}
											>
												<svg class="w-3.5 h-3.5 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
												</svg>
												<span>{profile.name}</span>
												{#if profile.id === liveTab.profile}
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

						{#if liveTab.executionMode === 'worktree' && liveTab.messages.length > 0}
							<!-- Worktree Badge - replaces project selector after session starts -->
							<div class="worktree-badge" title="Working in git worktree">
								<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<circle cx="6" cy="6" r="2" stroke-width="2"/>
									<circle cx="18" cy="6" r="2" stroke-width="2"/>
									<circle cx="12" cy="18" r="2" stroke-width="2"/>
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 8v4c0 2 2 4 6 4m6-8v4c0 2-2 4-6 4" />
								</svg>
								<span class="worktree-branch">{liveTab.selectedWorktreeBranch || liveTab.worktreeBranch || 'worktree'}</span>
							</div>
						{:else}
							<!-- Project Selector -->
							<div class="selector-wrapper" bind:this={projectDropdownRef}>
								<button
									type="button"
									class="context-selector-btn {liveTab.project ? '' : 'unset'}"
									onclick={handleProjectClick}
									title="Change project"
								>
									<div class="selector-icon">
										<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
										</svg>
									</div>
									<span class="selector-text desktop">{selectedProjectName}</span>
									<span class="selector-text mobile">{selectedProjectNameMobile}</span>
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
													class:selected={project.id === liveTab.project}
													onclick={() => selectProject(project.id)}
												>
													<svg class="w-3.5 h-3.5 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
													</svg>
													<span>{project.name}</span>
													{#if project.id === liveTab.project}
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
						{/if}
					</div>

					<!-- Right: Override buttons, Attach & Mic buttons -->
					<div class="controls-right">
						<!-- Thinking Mode Override -->
						<div class="override-wrapper" bind:this={thinkingDropdownRef}>
							<button
								type="button"
								class="override-btn"
								class:active={liveTab.thinkingModeOverride !== null}
								onclick={handleThinkingClick}
								title="Thinking mode override (session only)"
							>
								<!-- Brain icon -->
								<svg class="w-[16px] h-[16px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
								</svg>
								{#if liveTab.thinkingModeOverride !== null}
									<span class="override-indicator"></span>
								{/if}
							</button>

							{#if showThinkingDropdown}
								<div class="override-dropdown">
									<div class="override-dropdown-header">Thinking Mode</div>
									{#each thinkingModes as mode}
										<button
											type="button"
											class="override-dropdown-item"
											class:selected={liveTab.thinkingModeOverride === mode.value}
											onclick={() => selectThinkingMode(mode.value)}
										>
											<span class="override-item-label">{mode.label}</span>
											{#if liveTab.thinkingModeOverride === mode.value}
												<svg class="w-3.5 h-3.5 check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
												</svg>
											{/if}
										</button>
									{/each}
								</div>
							{/if}
						</div>

						<!-- Permission Mode Override -->
						<div class="override-wrapper" bind:this={permissionDropdownRef}>
							<button
								type="button"
								class="override-btn"
								class:active={liveTab.permissionModeOverride !== null}
								onclick={handlePermissionClick}
								title="Permission mode override (session only)"
							>
								<!-- Shield icon -->
								<svg class="w-[16px] h-[16px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
								</svg>
								{#if liveTab.permissionModeOverride !== null}
									<span class="override-indicator"></span>
								{/if}
							</button>

							{#if showPermissionDropdown}
								<div class="override-dropdown">
									<div class="override-dropdown-header">Permission Mode</div>
									{#each permissionModes as mode}
										<button
											type="button"
											class="override-dropdown-item"
											class:selected={liveTab.permissionModeOverride === mode.value}
											onclick={() => selectPermissionMode(mode.value)}
										>
											<span class="override-item-label">{mode.label}</span>
											{#if liveTab.permissionModeOverride === mode.value}
												<svg class="w-3.5 h-3.5 check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
												</svg>
											{/if}
										</button>
									{/each}
								</div>
							{/if}
						</div>

						<!-- Attach File Button -->
						<button
							type="button"
							onclick={triggerFileUpload}
							disabled={liveTab.isStreaming || !$claudeAuthenticated || isUploading}
							class="dock-action-btn"
							title={isUploading ? 'Uploading...' : liveTab.project ? 'Attach file' : 'Select a project to attach files'}
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

						<!-- Voice Input Button with Rich Visual Feedback -->
						<!-- Wrapper contains overflow:hidden to clip animation rings without affecting sibling dropdowns -->
						<div class="voice-btn-wrapper">
							<button
								type="button"
								onclick={toggleRecording}
								disabled={!$claudeAuthenticated || isUploading || isTranscribing || isPreparing || !canRecord}
								class="voice-btn"
								class:preparing={isPreparing}
								class:recording={isRecording}
								class:transcribing={isTranscribing}
								title={!canRecord ? 'Configure OpenAI API key in Settings to use voice input' : isPreparing ? 'Preparing...' : isRecording ? 'Tap to stop' : isTranscribing ? 'Transcribing...' : 'Voice input'}
								style="--audio-level: {audioLevel}"
							>
								<!-- Audio level rings (visible when recording) -->
								{#if isRecording}
									<div class="voice-rings">
										<div class="voice-ring voice-ring-1" style="transform: scale({1 + audioLevel * 0.3}); opacity: {0.3 + audioLevel * 0.4}"></div>
										<div class="voice-ring voice-ring-2" style="transform: scale({1 + audioLevel * 0.5}); opacity: {0.2 + audioLevel * 0.3}"></div>
										<div class="voice-ring voice-ring-3" style="transform: scale({1 + audioLevel * 0.7}); opacity: {0.1 + audioLevel * 0.2}"></div>
									</div>
								{/if}

								<!-- Button content -->
								<div class="voice-btn-content">
									{#if isTranscribing}
										<!-- Processing: animated dots -->
										<div class="voice-processing">
											<span class="processing-dot"></span>
											<span class="processing-dot"></span>
											<span class="processing-dot"></span>
										</div>
									{:else if isPreparing}
										<!-- Preparing: pulsing mic -->
										<svg class="voice-icon voice-icon-preparing" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
										</svg>
									{:else if isRecording}
										<!-- Recording: live waveform indicator -->
										<div class="voice-waveform">
											<span class="waveform-bar" style="height: {20 + audioLevel * 60}%"></span>
											<span class="waveform-bar" style="height: {40 + audioLevel * 50}%"></span>
											<span class="waveform-bar" style="height: {30 + audioLevel * 70}%"></span>
											<span class="waveform-bar" style="height: {50 + audioLevel * 40}%"></span>
											<span class="waveform-bar" style="height: {25 + audioLevel * 55}%"></span>
										</div>
									{:else}
										<!-- Idle: mic icon -->
										<svg class="voice-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
										</svg>
									{/if}
								</div>
							</button>
						</div>
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
		border-radius: var(--radius, 0.75rem);
		font-size: 0.8125rem;
		color: var(--destructive);
	}

	.error-close {
		font-size: 1.25rem;
		line-height: 1;
		opacity: 0.7;
		transition: opacity 0.15s;
	}

	/* Worktree-specific error styling */
	.error-banner.worktree-error {
		background: color-mix(in srgb, var(--warning) 10%, transparent);
		border-color: color-mix(in srgb, var(--warning) 30%, transparent);
		color: var(--warning);
	}

	.error-banner.worktree-error svg {
		color: var(--warning);
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
		background: var(--chip-primary-bg);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		font-size: 0.75rem;
	}

	/* Chat history reference chip - purple theme */
	.file-chip.chat-history-chip {
		background: color-mix(in oklch, var(--feature-agents) 15%, transparent);
	}

	/* Note reference chip - green theme */
	.file-chip.note-chip {
		background: var(--status-success-bg);
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
		background: var(--glass-bg);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border: 1px solid var(--glass-border);
		border-radius: calc(var(--radius, 0.75rem) * 1.33) calc(var(--radius, 0.75rem) * 1.33) 0 0;
		padding: 4px;
		transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
	}

	/* Focus glow effect */
	.input-wrapper:focus-within {
		border-color: var(--ring);
		box-shadow:
			0 0 0 1px var(--ring),
			0 0 40px -10px var(--glow-color-soft),
			inset 0 1px 0 var(--hover-overlay);
	}

	/* ===== INPUT INNER - Glass rounded bubble inside ===== */
	.input-inner {
		display: flex;
		align-items: center;
		gap: 8px;
		background: var(--input-bg);
		border-radius: var(--radius, 0.75rem);
		padding: 12px 16px;
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
		color: var(--foreground);
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

	.chat-textarea::placeholder {
		color: var(--input-placeholder);
		font-weight: 300;
	}

	.chat-textarea:focus::placeholder {
		color: var(--muted-foreground);
	}

	/* Send button wrapper - aligns to bottom when text wraps to multiple lines */
	.send-button-wrapper {
		display: flex;
		align-items: center;
		align-self: flex-end;
		flex-shrink: 0;
	}

	/* ===== SEND BUTTON - COMPACT ===== */
	.send-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		background: var(--button-primary-gradient);
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
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
		background: linear-gradient(135deg, var(--hover-overlay), transparent);
		opacity: 0;
		transition: opacity 0.2s ease;
	}

	.send-btn:hover:not(:disabled) {
		transform: translateY(-1px);
		box-shadow: 0 4px 20px var(--glow-color-soft);
	}

	.send-btn:hover:not(:disabled)::before {
		opacity: 1;
	}

	.send-btn:active:not(:disabled) {
		transform: translateY(0) scale(0.96);
	}

	.send-btn svg {
		color: var(--primary-foreground);
		position: relative;
		z-index: 1;
	}

	.send-btn:disabled {
		background: var(--muted);
		cursor: not-allowed;
	}

	.send-btn:disabled svg {
		color: var(--muted-foreground);
	}

	/* Creating worktree state - show primary color with spinner */
	.send-btn.creating-worktree {
		background: var(--button-primary-gradient);
		cursor: wait;
	}

	.send-btn.creating-worktree svg {
		color: var(--primary-foreground);
	}

	.stop-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		background: var(--status-error-bg);
		color: var(--destructive);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		transition: background-color 0.15s;
	}

	.stop-btn:hover {
		background: color-mix(in oklch, var(--destructive) 25%, transparent);
	}

	/* ===== CONTROLS DOCK ===== */
	.controls-dock {
		display: flex;
		align-items: center;
		justify-content: space-between;
		background: color-mix(in oklch, var(--card) 60%, transparent);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border: 1px solid var(--border-subtle);
		border-top: none;
		border-radius: 0 0 calc(var(--radius, 0.75rem) * 1.33) calc(var(--radius, 0.75rem) * 1.33);
		padding: 8px 12px;
		/* Allow dropdowns to overflow - voice button wrapper handles its own clipping */
		overflow: visible;
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

	/* Worktree badge - shown when working in git worktree mode */
	.worktree-badge {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 4px 10px;
		background: color-mix(in srgb, var(--primary) 15%, transparent);
		border: 1px solid color-mix(in srgb, var(--primary) 30%, transparent);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		color: var(--primary);
		font-size: 12px;
		font-weight: 500;
	}

	.worktree-badge svg {
		flex-shrink: 0;
		color: var(--primary);
	}

	.worktree-branch {
		max-width: 120px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.controls-right {
		display: flex;
		align-items: center;
		gap: 4px;
		justify-content: flex-end;
		flex: 1;
	}

	/* ===== USAGE INDICATOR - Glass styling to match desktop ===== */
	.usage-indicator {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 4px 8px;
		background: var(--glass-bg);
		backdrop-filter: blur(var(--glass-blur, 12px));
		-webkit-backdrop-filter: blur(var(--glass-blur, 12px));
		border: 1px solid var(--glass-border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
	}

	.usage-bar {
		width: 40px;
		height: 4px;
		background: color-mix(in oklch, var(--muted) 50%, var(--background));
		border-radius: calc(var(--radius, 0.75rem) * 0.17);
		overflow: hidden;
	}

	.usage-fill {
		height: 100%;
		background: var(--primary);
		border-radius: calc(var(--radius, 0.75rem) * 0.17);
		transition: width 0.3s ease;
	}

	.usage-text {
		font-family: ui-monospace, 'Monaco', 'Menlo', monospace;
		font-size: 11px;
		font-weight: 500;
		color: var(--muted-foreground);
		letter-spacing: 0.02em;
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
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.context-selector-btn:hover {
		background: var(--hover-overlay);
		border-color: var(--border-subtle);
	}

	.context-selector-btn.unset {
		color: var(--warning);
	}

	.context-selector-btn.unset:hover {
		background: var(--status-warning-bg);
		border-color: var(--status-warning-border);
	}

	.selector-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
		border-radius: calc(var(--radius, 0.75rem) * 0.42);
		background: var(--muted);
		border: 1px solid var(--border-subtle);
	}

	.selector-icon svg {
		color: var(--muted-foreground);
	}

	.selector-text {
		font-size: 12px;
		font-weight: 500;
		color: var(--muted-foreground);
		letter-spacing: 0.01em;
		max-width: 80px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Mobile: hide text, show icons only */
	.selector-text.mobile,
	.selector-text.desktop {
		display: none;
	}

	/* Desktop: show full name */
	@media (min-width: 640px) {
		.selector-text {
			max-width: 120px;
		}

		.selector-text.mobile {
			display: none;
		}

		.selector-text.desktop {
			display: block;
		}
	}

	/* Chevron */
	.chevron {
		color: var(--muted-foreground);
		transition: transform 0.2s ease;
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
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.dock-action-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.dock-action-btn:active:not(:disabled) {
		transform: scale(0.94);
	}

	.dock-action-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	.dock-action-btn.recording {
		background: var(--status-error-bg);
		color: var(--destructive);
		animation: pulse 1.5s ease-in-out infinite;
	}

	.dock-action-btn.transcribing {
		background: var(--chip-primary-bg);
		color: var(--primary);
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
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		box-shadow: var(--shadow-l);
		z-index: 50;
		padding: 6px;
		animation: dropdown-fade-in 0.15s ease;
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
		color: var(--muted-foreground);
		text-align: center;
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 8px 12px;
		font-size: 13px;
		color: var(--popover-foreground);
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		cursor: pointer;
		transition: background-color 0.15s;
		text-align: left;
	}

	.dropdown-item:hover {
		background: var(--hover-overlay);
	}

	.dropdown-item.selected {
		background: var(--chip-primary-bg);
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
		background: var(--scrollbar-thumb);
		border-radius: calc(var(--radius, 0.75rem) * 0.25);
	}

	.selector-dropdown::-webkit-scrollbar-thumb:hover {
		background: var(--scrollbar-thumb-hover);
	}

	/* Responsive: mobile layout adjustments */
	@media (max-width: 639px) {
		/* Hide the usage bar on mobile, only show percentage */
		.usage-bar {
			display: none;
		}

		/* Make usage indicator more compact */
		.usage-indicator {
			padding: 4px 6px;
			gap: 0;
		}

		/* Show selector text on mobile (profile/project names) */
		.selector-text {
			display: block;
			max-width: 80px;
			font-size: 11px;
		}

		/* Make selectors more compact */
		.context-selector-btn {
			padding: 4px 6px 4px 4px;
			gap: 4px;
		}

		/* Smaller selector icon */
		.selector-icon {
			width: 18px;
			height: 18px;
		}

		/* Hide chevron on mobile to save space */
		.context-selector-btn .chevron {
			display: none;
		}

		/* Tighter gap in center */
		.controls-center {
			gap: 4px;
		}

		/* Smaller worktree badge on mobile */
		.worktree-branch {
			max-width: 80px;
			font-size: 11px;
		}
	}

	/* ===== VOICE BUTTON WITH RICH VISUAL FEEDBACK ===== */
	/* Wrapper clips animation rings without affecting sibling dropdown menus */
	.voice-btn-wrapper {
		position: relative;
		/* Clip voice animation rings within this wrapper only */
		overflow: hidden;
		/* Match voice button size plus a bit of padding for the glow */
		/* Use theme radius with max 50% for circular, 0 for square themes */
		border-radius: min(50%, calc(var(--radius, 0.75rem) * 2.67));
		/* Ensure the wrapper doesn't add extra size */
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.voice-btn {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		background: transparent;
		border: none;
		/* Use theme radius with max 50% for circular, 0 for square themes */
		border-radius: min(50%, calc(var(--radius, 0.75rem) * 2.67));
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
		overflow: visible;
	}

	.voice-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.voice-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	/* Preparing state: pulsing animation */
	.voice-btn.preparing {
		background: var(--status-warning-bg);
		color: var(--warning);
	}

	.voice-btn.preparing .voice-icon-preparing {
		animation: voice-pulse 1s ease-in-out infinite;
	}

	@keyframes voice-pulse {
		0%, 100% {
			transform: scale(1);
			opacity: 0.7;
		}
		50% {
			transform: scale(1.1);
			opacity: 1;
		}
	}

	/* Recording state: vibrant red glow */
	.voice-btn.recording {
		background: var(--status-error-bg);
		color: var(--destructive);
		box-shadow: 0 0 0 2px var(--status-error-border);
	}

	/* Transcribing state: teal processing */
	.voice-btn.transcribing {
		background: var(--chip-primary-bg);
		color: var(--primary);
	}

	/* Voice button content container */
	.voice-btn-content {
		position: relative;
		z-index: 2;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 100%;
		height: 100%;
	}

	/* Mic icon */
	.voice-icon {
		width: 18px;
		height: 18px;
		transition: transform 0.2s ease;
	}

	/* Audio level rings (expand outward when speaking) */
	.voice-rings {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		pointer-events: none;
		z-index: 1;
		/* Contain rings within visible area */
		overflow: visible;
	}

	.voice-ring {
		position: absolute;
		/* Smaller base size so expanded rings stay more contained */
		width: 80%;
		height: 80%;
		/* Use theme radius with max 50% for circular, 0 for square themes */
		border-radius: min(50%, calc(var(--radius, 0.75rem) * 2.67));
		border: 2px solid var(--status-error-border);
		transition: transform 0.1s ease-out, opacity 0.1s ease-out;
	}

	.voice-ring-1 { animation-delay: 0s; }
	.voice-ring-2 { animation-delay: 0.1s; }
	.voice-ring-3 { animation-delay: 0.2s; }

	/* Waveform visualization */
	.voice-waveform {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 2px;
		height: 18px;
	}

	.waveform-bar {
		width: 3px;
		min-height: 4px;
		background: currentColor;
		border-radius: 1.5px;
		transition: height 0.08s ease-out;
	}

	/* Processing dots animation */
	.voice-processing {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 4px;
	}

	.processing-dot {
		width: 4px;
		height: 4px;
		background: currentColor;
		border-radius: 50%;
		animation: processing-bounce 1.2s infinite ease-in-out;
	}

	.processing-dot:nth-child(1) { animation-delay: 0s; }
	.processing-dot:nth-child(2) { animation-delay: 0.2s; }
	.processing-dot:nth-child(3) { animation-delay: 0.4s; }

	@keyframes processing-bounce {
		0%, 80%, 100% {
			transform: scale(0.8);
			opacity: 0.5;
		}
		40% {
			transform: scale(1.2);
			opacity: 1;
		}
	}

	/* Active/pressed state */
	.voice-btn:active:not(:disabled) {
		transform: scale(0.95);
	}

	/* ===== OVERRIDE BUTTONS ===== */
	.override-wrapper {
		position: relative;
	}

	.override-btn {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.override-btn:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.override-btn.active {
		background: var(--chip-primary-bg);
		color: var(--primary);
	}

	.override-btn.active:hover {
		background: color-mix(in oklch, var(--primary) 20%, transparent);
	}

	/* Active indicator dot */
	.override-indicator {
		position: absolute;
		top: 2px;
		right: 2px;
		width: 6px;
		height: 6px;
		background: var(--primary);
		border-radius: 50%;
		border: 1px solid var(--background);
	}

	/* Override dropdown */
	.override-dropdown {
		position: absolute;
		bottom: calc(100% + 6px);
		right: 0;
		min-width: 140px;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		box-shadow: var(--shadow-l);
		z-index: 50;
		padding: 4px;
		animation: override-dropdown-fade-in 0.15s ease;
	}

	@keyframes override-dropdown-fade-in {
		from {
			opacity: 0;
			transform: translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.override-dropdown-header {
		padding: 6px 10px 4px;
		font-size: 10px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
	}

	.override-dropdown-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 8px 10px;
		font-size: 13px;
		color: var(--popover-foreground);
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		cursor: pointer;
		transition: background-color 0.15s;
		text-align: left;
	}

	.override-dropdown-item:hover {
		background: var(--hover-overlay);
	}

	.override-dropdown-item.selected {
		background: var(--chip-primary-bg);
	}

	.override-item-label {
		font-weight: 500;
		flex: 1;
	}

	.override-dropdown-item .check-icon {
		flex-shrink: 0;
		color: var(--primary);
		margin-left: auto;
	}

	/* Mobile: slightly more compact */
	@media (max-width: 639px) {
		.override-btn {
			width: 26px;
			height: 26px;
		}

		.override-btn svg {
			width: 14px;
			height: 14px;
		}

		.override-dropdown {
			min-width: 120px;
		}
	}
</style>

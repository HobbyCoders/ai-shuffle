<script lang="ts">
	/**
	 * ChatInput - Full input section with file upload, autocomplete, and voice recording
	 */
	import { tick, onDestroy, createEventDispatcher } from 'svelte';
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

	// Override popup state
	let showModelPopup = $state(false);
	let showModePopup = $state(false);
	let showProfilePopup = $state(false);
	let showProjectPopup = $state(false);

	// Button refs for fixed positioning
	let profileBtnRef = $state<HTMLButtonElement | null>(null);
	let projectBtnRef = $state<HTMLButtonElement | null>(null);
	let modelBtnRef = $state<HTMLButtonElement | null>(null);
	let modeBtnRef = $state<HTMLButtonElement | null>(null);

	// Dropdown positions (calculated from button positions)
	let profileDropdownPos = $state({ left: 0, bottom: 0 });
	let projectDropdownPos = $state({ left: 0, bottom: 0 });
	let modelDropdownPos = $state({ left: 0, bottom: 0 });
	let modeDropdownPos = $state({ left: 0, bottom: 0 });

	// Calculate dropdown position from button
	function updateDropdownPosition(btnRef: HTMLButtonElement | null, setter: (pos: { left: number; bottom: number }) => void) {
		if (!btnRef) return;
		const rect = btnRef.getBoundingClientRect();
		// Position dropdown centered above the button
		setter({
			left: rect.left + rect.width / 2,
			bottom: window.innerHeight - rect.top + 6 // 6px gap above button
		});
	}

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
			<!-- Floating Island Input Container - now contains everything -->
			<div class="chat-pill-island">
				<!-- Top Controls Row (Context + Profile + Project + Model + Mode + Attach) -->
				<div class="pill-controls-row">
				<!-- Context Usage Indicator -->
				<div class="relative group">
					<button
						type="button"
						class="inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-medium rounded-full bg-accent/50 text-muted-foreground hover:text-foreground border border-transparent hover:border-border/50 transition-all"
						title="Context usage: {formatTokenCount(contextUsed)} / {formatTokenCount(contextMax)}"
					>
						<!-- Circular progress indicator -->
						<svg class="w-3.5 h-3.5 -rotate-90" viewBox="0 0 20 20">
							<circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" stroke-width="2" opacity="0.2" />
							<circle
								cx="10" cy="10" r="8" fill="none"
								stroke={contextPercent > 80 ? '#ef4444' : contextPercent > 60 ? '#f59e0b' : '#22c55e'}
								stroke-width="2"
								stroke-dasharray={2 * Math.PI * 8}
								stroke-dashoffset={2 * Math.PI * 8 * (1 - contextPercent / 100)}
								stroke-linecap="round"
							/>
						</svg>
						<span>{Math.round(contextPercent)}%</span>
					</button>
					<!-- Token dropdown -->
					<div class="absolute left-1/2 -translate-x-1/2 bottom-full mb-1.5 w-52 bg-card border border-border rounded-xl shadow-xl opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-[10002]">
						<div class="py-2 px-3 space-y-2">
							<!-- Context header -->
							<div class="flex items-center justify-between text-xs pb-1 border-b border-border">
								<span class="text-muted-foreground">Context</span>
								<span class="text-foreground font-medium">{formatTokenCount(contextUsed)} / {formatTokenCount(contextMax)}</span>
							</div>
							<div class="flex items-center justify-between text-xs">
								<span class="flex items-center gap-1.5 text-muted-foreground">
									<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16V4m0 0L3 8m4-4l4 4" />
									</svg>
									Input
								</span>
								<span class="text-foreground font-medium">{formatTokenCount(tab.totalTokensIn)}</span>
							</div>
							<div class="flex items-center justify-between text-xs">
								<span class="flex items-center gap-1.5 text-muted-foreground">
									<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 8v12m0 0l4-4m-4 4l-4-4" />
									</svg>
									Output
								</span>
								<span class="text-foreground font-medium">{formatTokenCount(tab.totalTokensOut)}</span>
							</div>
							{#if tab.totalCacheCreationTokens > 0}
								<div class="flex items-center justify-between text-xs">
									<span class="flex items-center gap-1.5 text-muted-foreground">
										<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
										</svg>
										Cache Creation
									</span>
									<span class="text-foreground font-medium">{formatTokenCount(tab.totalCacheCreationTokens)}</span>
								</div>
							{/if}
							{#if tab.totalCacheReadTokens > 0}
								<div class="flex items-center justify-between text-xs">
									<span class="flex items-center gap-1.5 text-blue-400">
										<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
										</svg>
										Cache Read
									</span>
									<span class="text-blue-400 font-medium">{formatTokenCount(tab.totalCacheReadTokens)}</span>
								</div>
							{/if}
						</div>
					</div>
				</div>

				<!-- Profile Selector Pill (always visible, mobile-friendly) -->
				{#if !isProfileLocked}
					<div class="relative">
						<button
							bind:this={profileBtnRef}
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
							<button class="fixed inset-0 z-[10001]" onclick={() => showProfilePopup = false} aria-label="Close"></button>
							<div
								class="absolute left-1/2 -translate-x-1/2 bottom-full mb-2 bg-card border border-border rounded-xl shadow-xl overflow-hidden z-[10002] min-w-[160px] max-h-[280px] overflow-y-auto {profileContextMenu.show ? 'pointer-events-auto' : ''}"
							>
								{#if $profiles.length === 0}
									<div class="px-3 py-2 text-xs text-muted-foreground">No profiles</div>
								{:else}
									<!-- Grouped profiles -->
									{#each profilesOrganized.groupOrder as groupItem}
										{@const groupProfiles = profilesOrganized.grouped.get(groupItem.name) || []}
										{#if groupProfiles.length > 0}
											<div class="py-0.5">
												<button
													type="button"
													class="w-full flex items-center gap-1.5 px-2.5 py-1 text-[10px] font-medium text-muted-foreground hover:text-foreground uppercase tracking-wide bg-muted/30 transition-colors"
													onclick={() => groups.toggleGroupCollapsed('profiles', groupItem.name)}
												>
													<svg class="w-2.5 h-2.5 transition-transform {groupItem.collapsed ? '-rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
													</svg>
													<span>{groupItem.name}</span>
													<span class="text-muted-foreground/60 ml-auto">({groupProfiles.length})</span>
												</button>
												{#if !groupItem.collapsed}
													{#each groupProfiles as profile}
														<button
															type="button"
															onclick={() => {
																tabs.setTabProfile(tab.id, profile.id);
																showProfilePopup = false;
															}}
															oncontextmenu={(e) => handleProfileContextMenu(e, profile)}
															class="w-full px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors flex items-center justify-between gap-2 {tab.profile === profile.id ? 'bg-accent/50 text-foreground font-medium' : 'text-muted-foreground'}"
															title="Right-click for options"
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
									{/each}
									<!-- Ungrouped profiles -->
									{#if profilesOrganized.ungrouped.length > 0}
										{#if hasProfileGroups}
											<div class="py-0.5">
												<div class="px-2.5 py-1 text-[10px] font-medium text-muted-foreground uppercase tracking-wide bg-muted/30">
													Other
												</div>
											</div>
										{/if}
										{#each profilesOrganized.ungrouped as profile}
											<button
												type="button"
												onclick={() => {
													tabs.setTabProfile(tab.id, profile.id);
													showProfilePopup = false;
												}}
												oncontextmenu={(e) => handleProfileContextMenu(e, profile)}
												class="w-full px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors flex items-center justify-between gap-2 {tab.profile === profile.id ? 'bg-accent/50 text-foreground font-medium' : 'text-muted-foreground'}"
												title="Right-click for options"
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
							bind:this={projectBtnRef}
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
							<button class="fixed inset-0 z-[10001]" onclick={() => showProjectPopup = false} aria-label="Close"></button>
							<div
								class="absolute left-1/2 -translate-x-1/2 bottom-full mb-2 bg-card border border-border rounded-xl shadow-xl overflow-hidden z-[10002] min-w-[160px] max-h-[280px] overflow-y-auto {projectContextMenu.show ? 'pointer-events-auto' : ''}"
							>
								{#if $projects.length === 0}
									<div class="px-3 py-2 text-xs text-muted-foreground">No projects</div>
								{:else}
									<!-- Grouped projects -->
									{#each projectsOrganized.groupOrder as groupItem}
										{@const groupProjects = projectsOrganized.grouped.get(groupItem.name) || []}
										{#if groupProjects.length > 0}
											<div class="py-0.5">
												<button
													type="button"
													class="w-full flex items-center gap-1.5 px-2.5 py-1 text-[10px] font-medium text-muted-foreground hover:text-foreground uppercase tracking-wide bg-muted/30 transition-colors"
													onclick={() => groups.toggleGroupCollapsed('projects', groupItem.name)}
												>
													<svg class="w-2.5 h-2.5 transition-transform {groupItem.collapsed ? '-rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
													</svg>
													<span>{groupItem.name}</span>
													<span class="text-muted-foreground/60 ml-auto">({groupProjects.length})</span>
												</button>
												{#if !groupItem.collapsed}
													{#each groupProjects as project}
														<button
															type="button"
															onclick={() => {
																tabs.setTabProject(tab.id, project.id);
																showProjectPopup = false;
															}}
															oncontextmenu={(e) => handleProjectContextMenu(e, project)}
															class="w-full px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors flex items-center justify-between gap-2 {tab.project === project.id ? 'bg-accent/50 text-foreground font-medium' : 'text-muted-foreground'}"
															title="Right-click for options"
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
									{/each}
									<!-- Ungrouped projects -->
									{#if projectsOrganized.ungrouped.length > 0}
										{#if hasProjectGroups}
											<div class="py-0.5">
												<div class="px-2.5 py-1 text-[10px] font-medium text-muted-foreground uppercase tracking-wide bg-muted/30">
													Other
												</div>
											</div>
										{/if}
										{#each projectsOrganized.ungrouped as project}
											<button
												type="button"
												onclick={() => {
													tabs.setTabProject(tab.id, project.id);
													showProjectPopup = false;
												}}
												oncontextmenu={(e) => handleProjectContextMenu(e, project)}
												class="w-full px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors flex items-center justify-between gap-2 {tab.project === project.id ? 'bg-accent/50 text-foreground font-medium' : 'text-muted-foreground'}"
												title="Right-click for options"
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
							bind:this={modelBtnRef}
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
							<button class="fixed inset-0 z-[10001]" onclick={() => showModelPopup = false} aria-label="Close"></button>
							<div
								class="absolute left-1/2 -translate-x-1/2 bottom-full mb-2 bg-card border border-border rounded-xl shadow-xl overflow-hidden z-[10002] min-w-[90px]"
							>
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
							bind:this={modeBtnRef}
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
							<button class="fixed inset-0 z-[10001]" onclick={() => showModePopup = false} aria-label="Close"></button>
							<div
								class="absolute left-1/2 -translate-x-1/2 bottom-full mb-2 bg-card border border-border rounded-xl shadow-xl overflow-hidden z-[10002] min-w-[100px]"
							>
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

				<!-- Uploaded Files -->
				{#if uploadedFiles.length > 0}
					<div class="px-3 pt-2 pb-0 flex flex-wrap gap-1.5">
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
				<div class="pill-input-row">
					<!-- Textarea Container -->
					<div class="pill-textarea-container">
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

					<!-- Attach/Voice/Send/Stop Buttons -->
					<div class="flex items-center gap-1">
						<!-- Attach File Button (icon only) -->
						<button
							type="button"
							onclick={triggerFileUpload}
							disabled={tab.isStreaming || !$claudeAuthenticated || isUploading}
							class="w-9 h-9 sm:w-8 sm:h-8 flex items-center justify-center rounded-xl transition-all disabled:opacity-30 disabled:cursor-not-allowed {isUploading ? 'bg-primary/15 text-primary' : 'hover:bg-accent text-muted-foreground hover:text-foreground'}"
							title={isUploading ? 'Uploading...' : tab.project ? 'Attach file' : 'Select a project to attach files'}
						>
							{#if isUploading}
								<svg class="w-4 h-4 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
									<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
								</svg>
							{:else}
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
								</svg>
							{/if}
						</button>

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

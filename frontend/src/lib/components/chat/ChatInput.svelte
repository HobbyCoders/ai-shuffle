<script lang="ts">
	/**
	 * ChatInput - Modern chat input island with progressive disclosure
	 *
	 * Layer 1: Minimal - textarea + essential buttons
	 * Layer 2: Context Bar - inline profile/project dropdown selectors
	 *
	 * Delegates voice recording to VoiceRecorder, controls dock to ControlsDock,
	 * and drag-and-drop to DragDropOverlay.
	 */
	import { tick, onDestroy, onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { activeSession } from '$lib/stores/active-session';
	import { profiles } from '$lib/stores/app-data';
	import { claudeAuthenticated } from '$lib/stores/auth';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { api, type FileUploadResponse } from '$lib/api/client';
	import CommandAutocomplete from '$lib/components/CommandAutocomplete.svelte';
	import FileAutocomplete, { type FileItem } from '$lib/components/FileAutocomplete.svelte';
	import TiptapEditor from './TiptapEditor.svelte';
	import VoiceRecorder from './VoiceRecorder.svelte';
	import ControlsDock from './ControlsDock.svelte';
	import { hasAtMention, findLastAtMentionIndex, extractAtMentionQuery } from './tiptap/serialization';

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
		compact?: boolean;
	}

	let { compact = false }: Props = $props();

	// Input state - use draft for persistence across session switches
	let inputValue = $state($activeSession.draft || '');
	let plainTextValue = $state(''); // Plain text without chip serialization, for autocomplete
	let uploadedFiles = $state<FileUploadResponse[]>([]);
	let draftSaveTimeout: ReturnType<typeof setTimeout> | null = null;
	// Flag to prevent onDestroy from overwriting draft after we've already cleared it.
	// Using plain variable (not $state) to avoid reactivity issues with onDestroy capture.
	let messageSent = false;
	const initialSessionId = $activeSession.sessionId;

	// Get the draft value as JSON string (preserves file chips)
	function getDraftJSON(): string {
		const json = editorRef?.getJSON();
		if (!json) return inputValue;
		return JSON.stringify(json);
	}

	// Helper to save draft immediately (used on unmount)
	function saveDraftImmediately(value?: string) {
		if (draftSaveTimeout) {
			clearTimeout(draftSaveTimeout);
			draftSaveTimeout = null;
		}
		activeSession.setDraft(value ?? getDraftJSON());
	}

	// Save draft to store when input changes (debounced)
	// Captures session ID to prevent writing stale drafts to the wrong session after switch
	$effect(() => {
		// Track inputValue for reactivity (triggers on any text change)
		const _trigger = inputValue;
		const capturedSessionId = $activeSession.sessionId;

		// Debounce draft saves to avoid excessive updates
		if (draftSaveTimeout) {
			clearTimeout(draftSaveTimeout);
		}
		draftSaveTimeout = setTimeout(() => {
			// Guard: only save if we're still on the same session
			if (get(activeSession).sessionId !== capturedSessionId) return;
			const draftValue = getDraftJSON();
			if (draftValue !== $activeSession.draft) {
				activeSession.setDraft(draftValue);
			}
			draftSaveTimeout = null;
		}, 100);
	});

	// Save draft immediately when component unmounts (critical for mode switches)
	// BUT skip if we just sent a message or a session switch happened - the draft
	// was already cleared and we don't want to overwrite it with stale text.
	// Check the store imperatively (get()) since $effects don't run during teardown.
	onDestroy(() => {
		if (messageSent) return;
		const currentSessionId = get(activeSession).sessionId;
		if (currentSessionId !== initialSessionId) return; // session switched
		saveDraftImmediately();
	});

	let editorRef = $state<TiptapEditor | null>(null);
	let prevSessionId = $state($activeSession.sessionId);

	// Sync editor content when session changes (ChatInput is NOT keyed by sessionId)
	$effect(() => {
		const newSessionId = $activeSession.sessionId;
		if (newSessionId !== prevSessionId) {
			prevSessionId = newSessionId;
			const newDraft = $activeSession.draft || '';
			inputValue = newDraft;
			plainTextValue = '';
			editorRef?.setContent(newDraft);
		}
	});

	let fileInputRef = $state<HTMLInputElement | null>(null);
	let isUploading = $state(false);

	// Voice recording state (bindable from VoiceRecorder)
	let isRecording = $state(false);
	let isTranscribing = $state(false);

	// Error display — shared between file upload errors and voice recording errors
	let errorMessage = $state('');

	// Autocomplete state
	let showCommandAutocomplete = $state(false);
	let showFileAutocomplete = $state(false);
	let commandAutocompleteRef = $state<CommandAutocomplete | null>(null);
	let fileAutocompleteRef = $state<FileAutocomplete | null>(null);

	// Island ref
	let islandRef = $state<HTMLDivElement | null>(null);

	// Get selected profile name for display in placeholder
	const selectedProfileName = $derived($profiles.find(p => p.id === $activeSession.profile)?.name || 'Profile');

	// Handle queued message edit events from QueuedMessages component
	function handleQueuedMessageEdit(e: Event) {
		const content = (e as CustomEvent<{ content: string }>).detail.content;
		inputValue = content;
		plainTextValue = content;
		editorRef?.setContent(content);
		// Focus the editor after setting content
		tick().then(() => editorRef?.focus());
	}

	onMount(() => {
		window.addEventListener('queued-message-edit', handleQueuedMessageEdit);
		return () => {
			window.removeEventListener('queued-message-edit', handleQueuedMessageEdit);
		};
	});

	// Handle input changes for autocomplete
	function handleInputChange() {
		// Slash command autocomplete
		if (inputValue.startsWith('/')) {
			const slashContent = inputValue.slice(1);
			const spaceIndex = slashContent.indexOf(' ');
			// Dismiss once user types text after a space (they've moved past command selection)
			// e.g., "/compact " still shows (trailing space only), but "/compact arg" dismisses
			showCommandAutocomplete = spaceIndex === -1 || slashContent.slice(spaceIndex + 1).trim().length === 0;
		} else {
			showCommandAutocomplete = false;
		}

		// File autocomplete (@mentions)
		// Use plain text for detection to avoid false positives from chip @paths
		if (!!$activeSession.project && hasAtMention(plainTextValue)) {
			const atInfo = extractAtMentionQuery(plainTextValue);
			if (atInfo) {
				const query = atInfo.query;
				if (query.length > 0 && /^\s/.test(query)) {
					// @ followed immediately by whitespace → not a mention
					showFileAutocomplete = false;
				} else if (!query.includes('/') && query.includes(' ')) {
					// Root level query contains a space → only keep open if user is typing
					// a known virtual folder name (e.g., "Chat History")
					const knownFolders = ['Chat History', 'Notes', 'Subagents'];
					showFileAutocomplete = knownFolders.some(
						folder => folder.toLowerCase().startsWith(query.toLowerCase())
					);
				} else {
					showFileAutocomplete = true;
				}
			} else {
				showFileAutocomplete = false;
			}
		} else {
			showFileAutocomplete = false;
		}
	}

	// Handle form submission
	async function handleSubmit(sendLater: boolean = false) {
		// Get serialized message from editor (includes inline @path references)
		const finalPrompt = editorRef?.getMessage() ?? '';

		if (!finalPrompt.trim() || isUploading || $activeSession.worktreeDeleted || ($activeSession.executionMode === 'worktree' && !$activeSession.worktreeId)) return;

		// Client-side slash command: /fast [on|off]
		const fastMatch = finalPrompt.trim().match(/^\/fast(?:\s+(on|off|enable|disable|true|false))?$/i);
		if (fastMatch) {
			const arg = fastMatch[1]?.toLowerCase();
			const currentlyOn = chatPreferences.getValue('fastMode');
			const enable = arg === 'on' || arg === 'enable' || arg === 'true'
				? true
				: arg === 'off' || arg === 'disable' || arg === 'false'
				? false
				: !currentlyOn;
			chatPreferences.setFastMode(enable);
			inputValue = '';
			editorRef?.clear();
			return;
		}

		// Require profile and project to be selected
		if (!$activeSession.profile) {
			return;
		}
		if (!$activeSession.project) {
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
		sendMessage(messageToSend, sendLater);
	}

	// Actually send the message (or queue it for later)
	function sendMessage(finalPrompt: string, sendLater: boolean = false) {
		// Mark that we're sending - prevents onDestroy from overwriting the cleared draft
		// This is critical for the first message when the component gets destroyed/recreated
		messageSent = true;

		inputValue = '';
		uploadedFiles = [];

		// Clear editor content
		editorRef?.clear();

		// Clear draft immediately (not debounced) - important because
		// the component may be destroyed/recreated when isEmptyState changes
		saveDraftImmediately('');

		if (sendLater && $activeSession.isStreaming) {
			// Queue for when current stream completes naturally
			activeSession.queueMessage(finalPrompt);
		} else {
			// Send immediately (interrupt_and_query if streaming, normal query if not)
			activeSession.sendMessage(finalPrompt);
		}
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
		// Ctrl/Cmd+Enter during streaming = Send Later (queue for after completion)
		if (e.key === 'Enter' && !e.shiftKey && !isMobile) {
			e.preventDefault();
			if ((e.ctrlKey || e.metaKey) && $activeSession.isStreaming) {
				handleSubmit(true); // Send Later
			} else {
				handleSubmit(false); // Send Now
			}
		}
	}

	// Handle command selection
	async function handleCommandSelect(command: Command) {
		showCommandAutocomplete = false;

		// Insert the command into the input
		inputValue = `/${command.name} `;
		editorRef?.setContent(`/${command.name} `);
		await tick();
		editorRef?.focus();
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
		if (!$activeSession.project) {
			errorMessage = 'Please select a project first to upload files.';
			return;
		}
		fileInputRef?.click();
	}

	async function handleFileUpload(event: Event) {
		const input = event.target as HTMLInputElement;
		const files = input.files;
		if (!files || files.length === 0 || !$activeSession.project) return;

		isUploading = true;
		try {
			for (const file of Array.from(files)) {
				console.debug(`[Upload] Uploading file: "${file.name}" (${file.size} bytes)`);
				const result = await api.uploadFile(
					`/projects/${$activeSession.project}/upload`,
					file,
					$activeSession.sessionId ?? undefined,
					$activeSession.executionMode === 'worktree' ? $activeSession.worktreeId ?? undefined : undefined
				);
				console.debug(`[Upload] Response for "${file.name}": filename="${result.filename}", path="${result.path}"`);
				if (result.filename !== file.name && !result.filename.startsWith(file.name.replace(/\.[^.]+$/, ''))) {
					console.warn(`[Upload] Filename mismatch! Uploaded "${file.name}" but server returned "${result.filename}"`);
				}
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
			errorMessage = `Upload failed: ${err.detail || 'Unknown error'}`;
		} finally {
			isUploading = false;
			input.value = '';
		}
	}

	// Handle transcription result from VoiceRecorder
	async function handleTranscription(text: string) {
		editorRef?.appendText(text);
		inputValue = editorRef?.getText() ?? '';
		await tick();
		editorRef?.focus();
	}

	// Drag and drop file upload (inline since DragDropOverlay uses alert() for errors)
	let isDragging = $state(false);

	function handleDragEnter(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
		isDragging = true;
	}

	function handleDragLeave(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		if (
			e.clientX < rect.left ||
			e.clientX > rect.right ||
			e.clientY < rect.top ||
			e.clientY > rect.bottom
		) {
			isDragging = false;
		}
	}

	function handleDragOver(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
	}

	async function handleDrop(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
		isDragging = false;

		if (!e.dataTransfer?.files?.length) return;

		if (!$activeSession.project) {
			errorMessage = 'Please select a project first to upload files.';
			return;
		}

		isUploading = true;
		try {
			for (const file of Array.from(e.dataTransfer.files)) {
				const result = await api.uploadFile(
					`/projects/${$activeSession.project}/upload`,
					file,
					$activeSession.sessionId ?? undefined,
					$activeSession.executionMode === 'worktree' ? $activeSession.worktreeId ?? undefined : undefined
				);
				editorRef?.insertFileChip({
					path: result.path,
					filename: result.filename,
					fullPath: result.full_path,
					size: result.size,
					type: 'file'
				});
			}
			inputValue = editorRef?.getText() ?? '';
		} catch (error: unknown) {
			console.error('Upload failed:', error);
			const err = error as { detail?: string };
			errorMessage = `Upload failed: ${err.detail || 'Unknown error'}`;
		} finally {
			isUploading = false;
		}
	}
</script>

<div
	class="chat-input-wrapper"
	role="region"
	aria-label="Chat input with file drop"
>
	<div
		class="chat-input-container"
		role="region"
		aria-label="File drop zone"
		ondragenter={handleDragEnter}
		ondragleave={handleDragLeave}
		ondragover={handleDragOver}
		ondrop={handleDrop}
	>
		{#if isDragging}
			<div class="drag-overlay">
				<div class="drag-overlay-content">
					<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
					<span>Drop files to attach</span>
				</div>
			</div>
		{/if}
		<!-- Hidden file input -->
		<input
			type="file"
			bind:this={fileInputRef}
			onchange={handleFileUpload}
			class="hidden"
			multiple
			accept="*/*"
		/>

		<!-- Error Message (upload errors, voice recording errors) -->
		{#if errorMessage}
			<div class="error-banner">
				<span>{errorMessage}</span>
				<button type="button" onclick={() => errorMessage = ''} class="error-close">&times;</button>
			</div>
		{/if}

		<!-- Worktree Error Message -->
		{#if $activeSession.worktreeError}
			<div class="error-banner worktree-error">
				<svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<circle cx="6" cy="6" r="2" stroke-width="2"/>
					<circle cx="12" cy="18" r="2" stroke-width="2"/>
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 8v4c0 2 2 4 6 4v2" />
				</svg>
				<span>{$activeSession.worktreeError}</span>
				<button type="button" onclick={() => activeSession.clearWorktreeError()} class="error-close">&times;</button>
			</div>
		{/if}

		<!-- Worktree Deleted Banner (read-only mode) -->
		{#if $activeSession.worktreeDeleted}
			<div class="error-banner worktree-completed">
				<svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
				</svg>
				<span>This worktree session has been completed. Chat history is read-only.</span>
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
								projectId={$activeSession.project}
								worktreeId={$activeSession.executionMode === 'worktree' ? $activeSession.worktreeId ?? undefined : undefined}
								visible={showCommandAutocomplete}
								onSelect={handleCommandSelect}
								onClose={() => showCommandAutocomplete = false}
							/>

							<FileAutocomplete
								bind:this={fileAutocompleteRef}
								inputValue={plainTextValue}
								projectId={$activeSession.project}
								profileId={$activeSession.profile}
								worktreeId={$activeSession.executionMode === 'worktree' ? $activeSession.worktreeId ?? undefined : undefined}
								visible={showFileAutocomplete}
								onSelect={handleFileSelect}
								onClose={() => showFileAutocomplete = false}
							/>

							<TiptapEditor
								bind:this={editorRef}
								placeholder="Message {selectedProfileName}..."
								initialContent={$activeSession.draft || ''}
								disabled={!$claudeAuthenticated || $activeSession.worktreeDeleted}
								onTextChange={(text) => {
									inputValue = text;
									// Also update plain text for autocomplete (without chip @path serialization)
									plainTextValue = editorRef?.getText() ?? '';
									handleInputChange();
								}}
								onKeyDown={handleKeyDown}
							/>
						</div>

						<!-- Voice + Send buttons -->
						<div class="send-button-wrapper">
							<!-- Voice Input (delegated to VoiceRecorder component) -->
							<VoiceRecorder
								onTranscription={handleTranscription}
								disabled={!$claudeAuthenticated || isUploading || $activeSession.worktreeDeleted}
								bind:isRecording
								bind:isTranscribing
							/>

							{#if $activeSession.isCreatingWorktree}
								<!-- Creating worktree - show spinner -->
								<button type="button" class="send-btn creating-worktree" disabled title="Creating worktree...">
									<svg class="w-3 h-3 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
									</svg>
								</button>
							{:else if $activeSession.isStreaming}
								{#if plainTextValue.trim()}
									<!-- Send Now: interrupts current response -->
									<button type="submit" class="send-btn" title="Send now (interrupts) — Enter">
										<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 12h14M12 5l7 7-7 7" />
										</svg>
									</button>
									<!-- Send Later: queues for after completion -->
									<button type="button" class="send-btn send-later-btn"
										onclick={() => handleSubmit(true)}
										title="Send later (after response) — {navigator.platform?.includes('Mac') ? '⌘' : 'Ctrl'}+Enter"
									>
										<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
										</svg>
									</button>
								{:else}
									<button
										type="button"
										onclick={() => activeSession.stopGeneration()}
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
									disabled={!inputValue.trim() || !$claudeAuthenticated || isUploading || isRecording || isTranscribing || $activeSession.worktreeDeleted || ($activeSession.executionMode === "worktree" && !$activeSession.worktreeId)}
									title={$activeSession.worktreeDeleted ? "This worktree session has been completed" : ($activeSession.executionMode === "worktree" && !$activeSession.worktreeId) ? "Select a branch first" : isUploading ? "Uploading files..." : isRecording ? "Recording..." : isTranscribing ? "Transcribing..." : "Send message"}
								>
									<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 12h14M12 5l7 7-7 7" />
									</svg>
								</button>
							{/if}
						</div>
					</div>
				</div>

				<!-- Controls Dock (delegated to ControlsDock component) -->
				<ControlsDock
					{isUploading}
					onAttachClick={triggerFileUpload}
				/>
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

	/* Container with max-width - positioning context for drag overlay */
	.chat-input-container {
		position: relative;
		max-width: 56rem;
		margin: 0 auto;
	}

	/* Drag overlay */
	.drag-overlay {
		position: absolute;
		inset: 0;
		z-index: 50;
		background: color-mix(in oklch, var(--popover) 95%, transparent);
		backdrop-filter: blur(8px);
		display: flex;
		align-items: center;
		justify-content: center;
		border-radius: 0.75rem;
		animation: fadeIn 0.2s ease;
	}

	.drag-overlay-content {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 1rem 1.5rem;
		border: 2px dashed var(--primary);
		border-radius: 0.75rem;
		background: color-mix(in oklch, var(--primary) 8%, transparent);
		color: var(--primary);
		font-weight: 500;
		font-size: 0.875rem;
	}

	.drag-overlay-content svg {
		width: 1.25rem;
		height: 1.25rem;
		flex-shrink: 0;
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

	/* Worktree completed (read-only) styling */
	.error-banner.worktree-completed {
		justify-content: center;
		background: color-mix(in srgb, var(--muted-foreground) 8%, transparent);
		border-color: color-mix(in srgb, var(--muted-foreground) 20%, transparent);
		color: var(--muted-foreground);
	}

	.error-banner.worktree-completed svg {
		color: var(--success);
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

	/* Send button wrapper - stays vertically centered regardless of text height */
	.send-button-wrapper {
		display: flex;
		align-items: center;
		align-self: center;
		flex-shrink: 0;
		gap: 4px;
	}

	/* ===== SEND BUTTON - COMPACT ===== */
	.send-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		min-width: 32px;
		min-height: 32px;
		width: 32px;
		height: 32px;
		padding: 6px;
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

	/* Send Later button - secondary/muted style to distinguish from Send Now */
	.send-btn.send-later-btn {
		background: var(--muted);
		border: 1px solid var(--border-subtle);
	}

	.send-btn.send-later-btn::before {
		display: none;
	}

	.send-btn.send-later-btn svg {
		color: var(--muted-foreground);
	}

	.send-btn.send-later-btn:hover:not(:disabled) {
		background: color-mix(in oklch, var(--muted) 80%, var(--foreground) 10%);
		transform: translateY(-1px);
		box-shadow: none;
	}

	.send-btn.send-later-btn:hover:not(:disabled) svg {
		color: var(--foreground);
	}

	.stop-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		background: var(--status-error-bg);
		color: var(--destructive);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		transition: background-color 0.15s;
	}

	.stop-btn:hover {
		background: color-mix(in oklch, var(--destructive) 25%, transparent);
	}

	@keyframes fadeIn {
		from { opacity: 0; }
		to { opacity: 1; }
	}
</style>

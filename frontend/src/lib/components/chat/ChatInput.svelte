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
	import { readDraft, writeDraft } from '$lib/stores/session/draft-persistence';
	import { profiles } from '$lib/stores/app-data.svelte';
	import { anyCredential } from '$lib/stores/auth.svelte';

	import { api, type FileUploadResponse } from '$lib/api/client';
	import { toast } from '$lib/stores/toast';
	import { evaluateSubmitGuards } from './submit-guards';
	import Banner from '$lib/components/Banner.svelte';
	import CommandAutocomplete from '$lib/components/CommandAutocomplete.svelte';
	import FileAutocomplete, { type FileItem } from '$lib/components/FileAutocomplete.svelte';
	import TiptapEditor from './TiptapEditor.svelte';
	import VoiceRecorder from './VoiceRecorder.svelte';
	import VoiceCallButton from './VoiceCallButton.svelte';
	import StopButton from './StopButton.svelte';
	import ControlsDock from './ControlsDock.svelte';
	import ContextPillBar from './ContextPillBar.svelte';
	import { hasAtMention, findLastAtMentionIndex, extractAtMentionQuery, extractFileChips } from './tiptap/serialization';

	interface Command {
		name: string;
		display: string;
		description: string;
		argument_hint?: string;
		type: 'custom' | 'interactive' | 'sdk_builtin' | 'plugin' | 'skill';
		source?: string;
		namespace?: string;
	}

	interface Props {
		compact?: boolean;
	}

	let { compact = false }: Props = $props();

	// Resolve the draft to seed the composer with. The in-memory store draft is
	// authoritative, but it's wiped by a full page reload (e.g. a PWA update), so
	// fall back to the per-tab sessionStorage mirror when the store is empty.
	function resolveInitialDraft(): string {
		const storeDraft = $activeSession.draft;
		if (storeDraft) return storeDraft;
		return readDraft($activeSession.sessionId) || '';
	}

	// Input state - use draft for persistence across session switches
	const initialComposerDraft = resolveInitialDraft();
	let inputValue = $state(initialComposerDraft);
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

	// Helper to save draft immediately (used on unmount and on send)
	function saveDraftImmediately(value?: string) {
		if (draftSaveTimeout) {
			clearTimeout(draftSaveTimeout);
			draftSaveTimeout = null;
		}
		const val = value ?? getDraftJSON();
		activeSession.setDraft(val);
		// Keep the sessionStorage mirror in lockstep. On send this is called with
		// '' which clears the persisted entry; on unmount it flushes the live text.
		writeDraft(get(activeSession).sessionId, val);
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
			// Mirror to sessionStorage so the draft survives a full page reload
			// (e.g. a silent PWA update). Empty text clears the entry.
			writeDraft(capturedSessionId, draftValue);
			draftSaveTimeout = null;
		}, 100);

		// Clear the pending save on re-run/unmount so a stale timer can't fire
		// after the editor is torn down (or after a session switch).
		return () => {
			if (draftSaveTimeout) {
				clearTimeout(draftSaveTimeout);
				draftSaveTimeout = null;
			}
		};
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

	// Sync editor content when session changes (ChatInput is NOT keyed by sessionId).
	// Because this single component is reused across sessions, it is solely
	// responsible for moving draft text between the editor and the correct
	// session — getting this wrong is what made text "leak" between sessions.
	$effect(() => {
		const newSessionId = $activeSession.sessionId;
		if (newSessionId !== prevSessionId) {
			// Persist the LEAVING session's draft from the live editor before we
			// overwrite it. Reading the editor directly (not the debounced store
			// draft) guarantees the text follows the session it was typed in,
			// even when the switch happens within the debounce window.
			if (!messageSent && editorRef) {
				const leavingDraft = getDraftJSON();
				activeSession.saveDraftForSession(prevSessionId, leavingDraft);
				// Keep the persisted mirror aligned with the ephemeral cache so the
				// leaving session's draft also survives a reload.
				writeDraft(prevSessionId, leavingDraft);
			}
			prevSessionId = newSessionId;
			// Prefer the in-memory draft; fall back to the sessionStorage mirror
			// (populated across reloads) when the cache has nothing.
			const newDraft = $activeSession.draft || readDraft(newSessionId) || '';
			inputValue = newDraft;
			plainTextValue = '';
			editorRef?.setContent(newDraft);
		}
	});

	// Restore a discarded interrupted prompt back into the editor so the user can
	// edit or resend it. Set by the backend's `user_message_discarded` event when
	// a turn is stopped before any assistant output (the orphan user message is
	// removed from chat). Only fill an EMPTY editor so we never clobber text the
	// user has already started typing; either way clear the pending prompt so it
	// fires exactly once.
	$effect(() => {
		const pending = $activeSession.pendingInterruptPrompt;
		if (!pending) return;
		const current = (editorRef?.getMessage() ?? inputValue ?? '').trim();
		if (current.length === 0) {
			inputValue = pending;
			plainTextValue = pending;
			editorRef?.setContent(pending);
			tick().then(() => editorRef?.focus());
		}
		activeSession.clearPendingInterruptPrompt();
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
	let commandJustSelected = false;
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
		if (commandJustSelected) {
			commandJustSelected = false;
		} else if (inputValue.startsWith('/')) {
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

		// Surface WHY a send is blocked instead of silently swallowing the click,
		// which read as a dead app. The guards keep the message in the composer and
		// (except for an empty composer) name the missing piece with a toast.
		const block = evaluateSubmitGuards({
			hasPrompt: !!finalPrompt.trim(),
			isUploading,
			isCreatingWorktree: $activeSession.isCreatingWorktree,
			hasProfile: !!$activeSession.profile,
			hasProject: !!$activeSession.project
		});
		if (block) {
			if (block.notice) toast.push(block.notice.message, { tone: block.notice.tone });
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
	async function sendMessage(finalPrompt: string, sendLater: boolean = false) {
		// Mark that we're sending - prevents onDestroy from overwriting the cleared draft
		// This is critical for the first message when the component gets destroyed/recreated
		messageSent = true;

		// Collect pending upload paths before clearing state — needed for auto-worktree
		// to migrate files from main project to the new worktree directory.
		// Extract from both uploadedFiles (legacy) and inline file chips in the editor.
		let pendingPaths: string[] | undefined;
		if ($activeSession.worktreeIntent === 'worktree' && !$activeSession.worktreeId) {
			const chipPaths = editorRef?.getEditor()
				? extractFileChips(editorRef.getEditor()!).map(c => c.fullPath).filter(Boolean)
				: [];
			const legacyPaths = uploadedFiles.map(f => f.full_path);
			const allPaths = [...new Set([...chipPaths, ...legacyPaths])];
			pendingPaths = allPaths.length ? allPaths : undefined;
		}

		// Snapshot state needed to restore the input on failure. Capturing BEFORE
		// clearing means a failed interrupt / dropped WS doesn't lose the user's
		// typed prompt or uploaded attachments — we repopulate the editor.
		const priorInputValue = inputValue;
		const priorUploadedFiles = uploadedFiles;

		inputValue = '';
		uploadedFiles = [];

		// Clear editor content
		editorRef?.clear();

		// Clear draft immediately (not debounced) - important because
		// the component may be destroyed/recreated when isEmptyState changes
		saveDraftImmediately('');

		const restoreOnFailure = () => {
			inputValue = priorInputValue;
			uploadedFiles = priorUploadedFiles;
			editorRef?.setContent(finalPrompt);
			saveDraftImmediately(finalPrompt);
			messageSent = false;
		};

		try {
			if (sendLater && $activeSession.isStreaming) {
				// Queue for when current stream completes naturally
				const queued = activeSession.queueMessage(finalPrompt);
				if (queued === false) restoreOnFailure();
			} else {
				// Send immediately (interrupt → confirm → query if streaming, normal query if not)
				const sent = await activeSession.sendMessage(
					finalPrompt,
					pendingPaths?.length ? { pendingUploadPaths: pendingPaths } : undefined,
				);
				if (sent === false) restoreOnFailure();
			}
		} catch (err) {
			console.error('[ChatInput] sendMessage threw', err);
			restoreOnFailure();
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
	async function handleCommandSelect(command: Command, andSubmit?: boolean) {
		showCommandAutocomplete = false;
		commandJustSelected = true;

		// Insert the command into the input
		inputValue = `/${command.name} `;
		editorRef?.setContent(`/${command.name} `);
		await tick();

		if (andSubmit) {
			handleSubmit(false);
		} else {
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
					$activeSession.worktreeIntent === 'worktree' ? $activeSession.worktreeId ?? undefined : undefined
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
					$activeSession.worktreeIntent === 'worktree' ? $activeSession.worktreeId ?? undefined : undefined
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
			<Banner severity="error" message={errorMessage} dismissible onDismiss={() => (errorMessage = '')} />
		{/if}

		<!-- Worktree Error Message -->
		{#if $activeSession.worktreeError}
			<Banner severity="warning" message={$activeSession.worktreeError} dismissible onDismiss={() => activeSession.clearWorktreeError()} />
		{/if}

		<form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
			<!-- Main Island -->
			<div class="chat-island" bind:this={islandRef}>
				<!-- Context pills (profile + branch) sit directly above the input -->
				<ContextPillBar />

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
								worktreeId={$activeSession.worktreeIntent === 'worktree' ? $activeSession.worktreeId ?? undefined : undefined}
								visible={showCommandAutocomplete}
								onSelect={handleCommandSelect}
								onClose={() => showCommandAutocomplete = false}
							/>

							<FileAutocomplete
								bind:this={fileAutocompleteRef}
								inputValue={plainTextValue}
								projectId={$activeSession.project}
								profileId={$activeSession.profile}
								worktreeId={$activeSession.worktreeIntent === 'worktree' ? $activeSession.worktreeId ?? undefined : undefined}
								visible={showFileAutocomplete}
								onSelect={handleFileSelect}
								onClose={() => showFileAutocomplete = false}
							/>

							<TiptapEditor
								bind:this={editorRef}
								placeholder="Message {selectedProfileName}..."
								initialContent={initialComposerDraft}
								disabled={!$anyCredential}
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
							<!-- Voice call (VM19): starts/resumes a voice call. Renders
							     nothing when voice mode is off or the surface doesn't
							     qualify (active non-voice chat). -->
							<VoiceCallButton disabled={isUploading || isRecording || isTranscribing} />

							<!-- Voice Input (delegated to VoiceRecorder component) -->
							<VoiceRecorder
								onTranscription={handleTranscription}
								disabled={!$anyCredential || isUploading}
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
									<!-- Send Now: stops current response then sends -->
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
									<!-- Stop is an informed choice when background agents are live:
									     the SDK interrupt cancels the agents too, so StopButton
									     confirms first. -->
									<StopButton
										activeTasks={$activeSession.activeTasks}
										onStop={() => activeSession.stopGeneration()}
									/>
								{/if}
							{:else}
								<button
									type="submit"
									class="send-btn"
									disabled={!inputValue.trim() || !$anyCredential || isUploading || isRecording || isTranscribing}
									title={isUploading ? "Uploading files..." : isRecording ? "Recording..." : isTranscribing ? "Transcribing..." : ($activeSession.worktreeIntent === "worktree" && !$activeSession.worktreeId) ? "Branch will be auto-created from your message" : "Send message"}
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

	/* Main Island - Two tier structure */
	.chat-island {
		display: flex;
		flex-direction: column;
		overflow: visible;
	}

	/* ===== INPUT WRAPPER - THE HERO (glass outer frame) ===== */
	.input-wrapper {
		position: relative;
		/* The `backdrop-filter` below establishes a stacking context, which traps
		   the @/slash autocomplete panels (z-index: 50) inside this subtree. Without
		   an explicit z-index here, the whole wrapper stacks at the auto level (0)
		   and the ContextPillBar's ExecutionModeBar (z-index: 10) — plus its upward
		   dropdown — would render ABOVE the autocomplete menus. Lifting the wrapper
		   above the exec-mode bar keeps the @ and / menus on top where they belong. */
		z-index: 20;
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

	@keyframes fadeIn {
		from { opacity: 0; }
		to { opacity: 1; }
	}
</style>

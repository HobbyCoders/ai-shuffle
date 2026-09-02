<script lang="ts">
	/**
	 * ChatInput - the split-chips composer (mockup: docs/design/composer-split-chips.mockup.html).
	 *
	 * Three stacked parts, top to bottom:
	 *   1. ContextPillBar — the loose row of context chips (agent / runner /
	 *      repository / branch / worktree) floating ABOVE the box with a gap, in
	 *      both the new-chat and active states.
	 *   2. The box — a flat hairline rectangle holding the Tiptap editor with
	 *      the @/slash autocomplete panels, the return-glyph send cluster on its
	 *      right (send · send-while-streaming · Stop · creating-worktree
	 *      spinner), and the ControlsDock's bare-text controls row. The dictate
	 *      and voice-call buttons are passed to the dock as its `voiceActions`
	 *      snippet so they stay bound to this component's recording state.
	 *   3. A dim keyboard-hint line under the box.
	 *
	 * Delegates voice recording to VoiceRecorder, controls to ControlsDock, and
	 * drag-and-drop to the inline overlay.
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
	import { attachmentBlockReason } from './attach-disable';
	import {
		reconcileMobileComposerExpanded,
		shouldCollapseMobileComposer
	} from './mobile-composer-collapse';
	import { isPanelFullscreenWidth } from '$lib/utils/viewport';
	import Banner from '$lib/components/Banner.svelte';
	import CommandAutocomplete from '$lib/components/CommandAutocomplete.svelte';
	import FileAutocomplete, { type FileItem } from '$lib/components/FileAutocomplete.svelte';
	import TiptapEditor from './TiptapEditor.svelte';
	import VoiceRecorder from './VoiceRecorder.svelte';
	import VoiceCallButton from './VoiceCallButton.svelte';
	import StopButton from './StopButton.svelte';
	import ControlsDock from './ControlsDock.svelte';
	import ContextPillBar from './ContextPillBar.svelte';
	import { CornerDownLeft } from 'lucide-svelte';
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

	// Restore a removed user prompt back into the editor so the user can edit or
	// resend it. Set by the backend's `user_message_discarded` event when a turn
	// is stopped before any assistant output, and by a successful rewind that
	// truncated away the target user message. Only fill an EMPTY editor so we
	// never clobber text the user has already started typing; either way clear
	// the pending prompt so it fires exactly once.
	$effect(() => {
		const pending = $activeSession.pendingRestorePrompt;
		if (!pending) return;
		const current = (editorRef?.getMessage() ?? inputValue ?? '').trim();
		if (current.length === 0) {
			inputValue = pending;
			plainTextValue = pending;
			editorRef?.setContent(pending);
			tick().then(() => editorRef?.focus());
		}
		activeSession.clearPendingRestorePrompt();
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
	let composerFocused = $state(false);
	let mobileControlsOpen = $state(false);
	let commandJustSelected = false;
	let commandAutocompleteRef = $state<CommandAutocomplete | null>(null);
	let fileAutocompleteRef = $state<FileAutocomplete | null>(null);

	// Island ref
	let islandRef = $state<HTMLDivElement | null>(null);
	let mobileComposerExpanded = $state(false);
	const mobileCollapsed = $derived(
		shouldCollapseMobileComposer({
			compact,
			hasDraft: inputValue.trim().length > 0,
			expanded: mobileComposerExpanded,
			composerFocused,
			controlsOpen: mobileControlsOpen,
			commandAutocompleteOpen: showCommandAutocomplete,
			fileAutocompleteOpen: showFileAutocomplete
		})
	);

	function isMobileComposerViewport(): boolean {
		return typeof window !== 'undefined' && isPanelFullscreenWidth(window.innerWidth);
	}

	let mobileReconcileRaf: number | null = null;
	let mobileComposerDestroyed = false;

	function isComposerOwnedNode(target: EventTarget | Node | null): boolean {
		if (!(target instanceof Node)) return false;
		if (islandRef?.contains(target)) return true;
		return target instanceof Element && !!target.closest('[data-composer-owned-overlay="true"]');
	}

	function reconcileMobileComposer() {
		if (!isMobileComposerViewport()) return;
		mobileComposerExpanded = reconcileMobileComposerExpanded(
			mobileComposerExpanded,
			{
				compact,
				hasDraft: inputValue.trim().length > 0,
				composerFocused,
				controlsOpen: mobileControlsOpen,
				commandAutocompleteOpen: showCommandAutocomplete,
				fileAutocompleteOpen: showFileAutocomplete,
				activeElementOwned: isComposerOwnedNode(document.activeElement)
			}
		);
	}

	function scheduleMobileComposerReconciliation() {
		if (!compact || !isMobileComposerViewport()) return;
		if (mobileReconcileRaf !== null) cancelAnimationFrame(mobileReconcileRaf);
		// Focusout, pointerdown, and a portaled sheet's focus restoration settle in
		// separate microtasks. Reconcile on the next frame against final ownership.
		tick().then(() => {
			if (mobileComposerDestroyed) return;
			mobileReconcileRaf = requestAnimationFrame(() => {
				mobileReconcileRaf = null;
				if (!mobileComposerDestroyed) reconcileMobileComposer();
			});
		});
	}

	function keepMobileComposerExpanded() {
		if (isMobileComposerViewport()) mobileComposerExpanded = true;
	}

	function handleDocumentPointerDown(event: PointerEvent) {
		if (!compact || !isMobileComposerViewport()) return;
		if (isComposerOwnedNode(event.target)) {
			mobileComposerExpanded = true;
			return;
		}
		// A transcript/background tap is an explicit request to leave the empty
		// composer. Blurring first makes the deferred ownership check deterministic
		// even when the tapped element itself cannot receive focus.
		editorRef?.blur();
		scheduleMobileComposerReconciliation();
	}

	onDestroy(() => {
		mobileComposerDestroyed = true;
		if (mobileReconcileRaf !== null) cancelAnimationFrame(mobileReconcileRaf);
	});

	onMount(() => {
		document.addEventListener('pointerdown', handleDocumentPointerDown);
		return () => {
			document.removeEventListener('pointerdown', handleDocumentPointerDown);
		};
	});

	// Click-to-focus: the input area is taller than one text line, and clicks
	// landing on its padding (or the textarea wrapper) used to be dead. Any
	// click not already consumed by a real control focuses the editor, so the
	// whole upper half of an empty composer behaves like the text field.
	// Autocomplete panels render upward OUTSIDE .input-area, so their rows never
	// pass through here; selection inside them keeps working untouched.
	function handleEditorAreaClick(event: MouseEvent) {
		if (event.defaultPrevented) return;
		if (!(event.target instanceof Element)) return;
		// Buttons/inputs handle themselves (e.g. the inline file-chip remove).
		if (event.target.closest('button, a, input, select, textarea')) return;
		// A chip's own click positions the caret — only bare padding forwards.
		if (event.target.closest('[data-file-chip], .file-chip')) return;
		event.preventDefault();
		editorRef?.focus();
	}

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
	async function handleSubmit() {
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
			hasAnyProfile: $profiles.length > 0,
			hasProject: !!$activeSession.project || !!$activeSession.projectV2Id
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
		sendMessage(messageToSend);
	}

	// Actually send the message. There is no send-later fork: a send during a live
	// turn still goes out, and the backend decides when the model sees it (mid-turn
	// steer, or the start of the next turn). Until then it renders as a pending
	// bubble at the tail of the transcript.
	async function sendMessage(finalPrompt: string) {
		// Mark that we're sending - prevents onDestroy from overwriting the cleared draft
		// This is critical for the first message when the component gets destroyed/recreated
		messageSent = true;

		// Collect the staged upload paths this message references, BEFORE the
		// composer state is cleared below. Two consumers, both server-side:
		// auto-worktree migrates them into the new worktree directory, and a turn
		// placed on another device lifts them into durable upload blobs. Neither
		// applies to a plain local turn — the backend simply ignores the list
		// then — so they are always collected and the decision is made there.
		// Extract from both uploadedFiles (legacy) and inline file chips in the editor.
		let pendingPaths: string[] | undefined;
		{
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

		const collapseMobileComposer = () => {
			if (!isMobileComposerViewport()) return;
			mobileComposerExpanded = reconcileMobileComposerExpanded(
				mobileComposerExpanded,
				{
					compact,
					hasDraft: false,
					composerFocused: false,
					controlsOpen: false,
					commandAutocompleteOpen: false,
					fileAutocompleteOpen: false,
					activeElementOwned: false
				},
				'successful-send'
			);
			editorRef?.blur();
		};

		try {
			const sent = await activeSession.sendMessage(
				finalPrompt,
				pendingPaths?.length ? { pendingUploadPaths: pendingPaths } : undefined,
			);
			if (sent === false) restoreOnFailure();
			else collapseMobileComposer();
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
		if (e.key === 'Enter' && !e.shiftKey && !isMobile) {
			e.preventDefault();
			handleSubmit();
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
			handleSubmit();
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
		const block = attachmentBlockReason($activeSession.project, $activeSession.projectV2Id);
		if (block) {
			errorMessage = block;
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

		const dropBlock = attachmentBlockReason($activeSession.project, $activeSession.projectV2Id);
		if (dropBlock) {
			errorMessage = dropBlock;
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
	class:mobile-collapsed={mobileCollapsed}
	onfocusout={scheduleMobileComposerReconciliation}
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

		<form
			onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}
			onpointerdown={keepMobileComposerExpanded}
		>
			<!-- Context chips: agent · runner · repository · branch · worktree.
			     A loose row floating ABOVE the box with a visible gap, in both the
			     new-chat and active states (ContextPillBar decides which chips are
			     selectors and which are read-only). Mobile hides this row; the
			     mobile sheet's Chat-setup section carries the same selectors. -->
			<div class="context-pill-row"><ContextPillBar /></div>

			<!-- The box: a flat hairline rectangle holding the editor, the send
			     cluster on the right, and the bare-text controls row. -->
			<div class="chat-island" bind:this={islandRef}>
				<!-- The editor. The whole area forwards pointer events to the Tiptap
				     editor so the padding around the text line is clickable too — an
				     empty composer reads as one target. The autocomplete panels
				     (positioned upward OUTSIDE this element) are unaffected: they are
				     never inside this element's event path. -->
				<!-- svelte-ignore a11y_click_events_have_key_events -->
				<!-- svelte-ignore a11y_no_static_element_interactions -->
				<div class="input-area" onclick={handleEditorAreaClick}>
					<!-- Textarea with autocomplete -->
					<div class="textarea-wrapper">
						<CommandAutocomplete
							bind:this={commandAutocompleteRef}
							inputValue={inputValue}
							projectId={$activeSession.project}
							worktreeId={$activeSession.worktreeIntent === 'worktree' ? $activeSession.worktreeId ?? undefined : undefined}
							sessionId={$activeSession.sessionId ?? undefined}
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
							sessionId={$activeSession.sessionId ?? undefined}
							visible={showFileAutocomplete}
							onSelect={handleFileSelect}
							onClose={() => showFileAutocomplete = false}
						/>

						<TiptapEditor
							bind:this={editorRef}
							placeholder="Describe a task or ask a question"
							initialContent={initialComposerDraft}
							disabled={!$anyCredential}
							onTextChange={(text) => {
								inputValue = text;
								// Also update plain text for autocomplete (without chip @path serialization)
								plainTextValue = editorRef?.getText() ?? '';
								handleInputChange();
							}}
							onKeyDown={handleKeyDown}
							onFocusChange={(focused) => {
								composerFocused = focused;
								if (focused) keepMobileComposerExpanded();
								else scheduleMobileComposerReconciliation();
							}}
						/>
					</div>

					<!-- Send cluster (mockup `.send`): the return glyph on the right
					     of the editor row, vertically centred in that row, accent-
					     coloured once there is something to send. Typing during a
					     turn does not replace Stop: the send goes out as a pending
					     message and the turn keeps running, so Stop stays one click
					     away beside it. Stop is an informed choice when background
					     agents are live — StopButton confirms first. -->
					<div class="send-cluster">
						{#if $activeSession.isCreatingWorktree}
							<!-- Creating worktree - show spinner -->
							<button type="button" class="send-btn creating-worktree" disabled title="Creating worktree..." aria-label="Creating worktree">
								<svg class="w-3.5 h-3.5 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
									<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
								</svg>
							</button>
						{:else if $activeSession.isStreaming}
							{#if plainTextValue.trim()}
								<button type="submit" class="send-btn" title="Send — Enter" aria-label="Send">
									<CornerDownLeft size={13} strokeWidth={2.2} aria-hidden="true" />
								</button>
							{/if}
							<StopButton
								activeTasks={$activeSession.activeTasks}
								onStop={() => activeSession.stopGeneration()}
							/>
						{:else}
							<button
								type="submit"
								class="send-btn"
								disabled={!inputValue.trim() || !$anyCredential || isUploading || isRecording || isTranscribing}
								title={isUploading ? "Uploading files..." : isRecording ? "Recording..." : isTranscribing ? "Transcribing..." : ($activeSession.worktreeIntent === "worktree" && !$activeSession.worktreeId) ? "Branch will be auto-created from your message" : "Send message"}
								aria-label="Send"
							>
								<CornerDownLeft size={13} strokeWidth={2.2} aria-hidden="true" />
							</button>
						{/if}
					</div>
				</div>

				<!-- Controls row. ControlsDock owns permission mode, attach, model,
				     effort and the context ring; the dictate and voice-call buttons
				     are handed to it as a snippet so they stay bound to this
				     component's recording state and render inside the form. -->
				<ControlsDock
					{isUploading}
					onAttachClick={triggerFileUpload}
					onSheetOpenChange={(open) => {
						mobileControlsOpen = open;
						if (open) keepMobileComposerExpanded();
						else scheduleMobileComposerReconciliation();
					}}
				>
					{#snippet voiceActions()}
						<!-- Voice Input (delegated to VoiceRecorder component) -->
						<VoiceRecorder
							onTranscription={handleTranscription}
							disabled={!$anyCredential || isUploading}
							bind:isRecording
							bind:isTranscribing
						/>

						<!-- Voice call (VM19): starts/resumes a voice call. Renders
						     nothing when voice mode is off or the surface doesn't
						     qualify (active non-voice chat). -->
						<VoiceCallButton disabled={isUploading || isRecording || isTranscribing} />
					{/snippet}
				</ControlsDock>
			</div>

			<!-- Dim hint line under the box (mockup `.hint`). Desktop only: on
			     mobile Enter inserts a newline, so the shortcut would be wrong. -->
			<div class="composer-hint" aria-hidden="true">
				<span><kbd>@</kbd> files</span>
				<span><kbd>/</kbd> commands</span>
				<span><kbd>shift ↵</kbd> newline</span>
			</div>
		</form>
	</div>
</div>

<style>
	/* ===== DOCK =====
	   Mirrors the mockup's `.dock`: the composer floats over the transcript with
	   generous bottom padding and no surface of its own. */
	.chat-input-wrapper {
		padding: 0 0.75rem 0.75rem;
		flex-shrink: 0;
	}

	@media (min-width: 1081px) {
		.chat-input-wrapper {
			padding: 0 1.5rem 1.25rem;
		}
	}

	/* Mockup `.composer` — shares the transcript column's 768px measure. Also
	   the positioning context for the drag overlay. */
	.chat-input-container {
		position: relative;
		max-width: 48rem;
		margin: 0 auto;
	}

	/* The chip row floats above the box; ContextPillBar supplies the gap (its
	   bottom margin) and the wrapping. */
	.context-pill-row {
		min-width: 0;
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
		border-radius: calc(var(--radius, 10px) + 2px);
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

	/* ===== THE BOX (mockup `.box`) =====
	   A flat rectangle: 1px hairline, the theme radius (10px), card fill, no
	   glass. It deliberately establishes NO stacking context (no backdrop-filter,
	   no z-index) so the rows order themselves against the chip row above:
	   chip row (z 10, set in ContextPillBar) < input area (z 20; its @/slash
	   panels open upward over the chips) < controls row (z 30, set in
	   ControlsDock; its popovers paint over the message box). `overflow` stays
	   VISIBLE: every popover in the composer opens out of these bounds, and
	   clipping them would make them unreachable. */
	.chat-island {
		position: relative;
		display: flex;
		flex-direction: column;
		border-radius: var(--radius, 10px);
		border: 1px solid var(--border);
		background: var(--card);
		transition: border-color 0.2s ease;
	}

	.chat-island:focus-within {
		border-color: var(--border-strong);
	}

	/* ===== THE EDITOR ROW ===== */
	.input-area {
		position: relative;
		z-index: 20;
		display: flex;
		align-items: flex-start;
		gap: 8px;
		padding: 12px 12px 4px 14px;
		min-height: 70px;
	}

	/* Desktop: the empty editor stretches to the box's full upper region, so
	   the caret lands where the user clicked instead of snapping to a one-line
	   strip. Once typing grows past that the natural height takes over. Mobile
	   keeps its compact metrics — there the composer collapses/focuses as one
	   surface already. (.tiptap-editor is TiptapEditor's scoped root; its inner
	   ProseMirror div inherits the stretch from the flex column.) */
	@media (min-width: 1081px) {
		.textarea-wrapper :global(.tiptap-editor),
		.textarea-wrapper :global(.tiptap-editor .ProseMirror) {
			min-height: 3.4em;
		}
	}

	/* Textarea wrapper — the autocomplete panels anchor to this. */
	.textarea-wrapper {
		flex: 1;
		min-width: 0;
		position: relative;
		background: transparent;
	}

	/* ===== SEND CLUSTER (mockup `.send`) =====
	   Right side of the editor row, vertically centred in that row. A flex
	   sibling of the editor rather than an absolutely positioned overlay, so
	   the text never runs under it and the mobile collapsed row keeps it at
	   the end of the single input line. */
	.send-cluster {
		display: flex;
		align-items: center;
		align-self: center;
		gap: 4px;
		flex-shrink: 0;
	}

	.send-btn {
		display: grid;
		place-items: center;
		width: 24px;
		height: 24px;
		padding: 0;
		background: var(--primary);
		border: none;
		border-radius: 6px;
		color: var(--primary-foreground);
		cursor: pointer;
		transition: background-color 0.12s ease, color 0.12s ease, filter 0.12s ease;
	}

	.send-btn:hover:not(:disabled) {
		filter: brightness(1.08);
	}

	.send-btn:active:not(:disabled) {
		filter: brightness(0.96);
	}

	.send-btn:focus-visible {
		outline: 1px solid var(--primary);
		outline-offset: 2px;
	}

	/* Nothing to send yet: the quiet return glyph the mockup shows at rest. */
	.send-btn:disabled {
		background: var(--muted);
		color: var(--subtle-foreground);
		cursor: default;
	}

	/* Creating worktree: keep the accent fill behind the spinner. */
	.send-btn.creating-worktree,
	.send-btn.creating-worktree:disabled {
		background: var(--primary);
		color: var(--primary-foreground);
		cursor: wait;
	}

	.send-cluster :global(.stop-btn) {
		width: 24px;
		height: 24px;
		border-radius: 6px;
	}

	/* ===== HINT LINE (mockup `.hint`) ===== */
	.composer-hint {
		display: flex;
		justify-content: flex-end;
		gap: 12px;
		margin: 8px 4px 0;
		color: var(--subtle-foreground);
		font-size: 11px;
		line-height: 1;
	}

	.composer-hint kbd {
		font: inherit;
		color: var(--muted-foreground);
	}

	@keyframes fadeIn {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	/* T3-style mobile composer: an idle chat shrinks to a single input line with
	   the send glyph at its end. Focusing restores the controls row; a
	   successful send blurs it again. */
	@media (max-width: 1080px) {
		.chat-input-wrapper {
			padding: 12px 0.5rem calc(0.5rem + env(safe-area-inset-bottom, 0px));
			background: linear-gradient(to top, var(--background) 68%, transparent);
		}

		.chat-island {
			border-radius: 16px;
			transition:
				border-radius 180ms ease,
				border-color 180ms ease;
		}

		/* Profile, branch and worktree setup live in the mobile controls sheet;
		   the keyboard hint describes desktop keys only. */
		.context-pill-row,
		.composer-hint {
			display: none;
		}

		.input-area {
			min-height: 56px;
			padding: 10px 10px 4px 12px;
		}

		/* Thumb-sized primary action. */
		.send-btn,
		.send-cluster :global(.stop-btn) {
			width: 34px;
			height: 34px;
			border-radius: 10px;
		}

		.mobile-collapsed .chat-island {
			min-height: 50px;
			border-radius: 14px;
		}

		.mobile-collapsed .context-pill-row {
			display: none;
		}

		/* Collapsed: the controls row goes away entirely. The send / stop
		   cluster lives in the input area, so the primary action stays on the
		   single remaining line. */
		.mobile-collapsed :global(.controls-dock) {
			display: none;
		}

		.mobile-collapsed .input-area {
			min-width: 0;
			min-height: 48px;
			align-items: center;
			padding: 7px 8px 7px 16px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.chat-island {
			transition: none;
		}
	}
</style>

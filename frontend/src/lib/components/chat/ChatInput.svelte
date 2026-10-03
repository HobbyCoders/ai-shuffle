<script lang="ts">
	/**
	 * ChatInput - the split-chips composer (mockup: docs/design/composer-split-chips.mockup.html).
	 *
	 * Two stacked parts, top to bottom:
	 *   1. ContextPillBar — the loose row of context chips (agent / runner /
	 *      repository / branch / worktree) floating ABOVE the box with a gap, in
	 *      both the new-chat and active states. Code lane only; see the markup.
	 *   2. The box — a flat hairline rectangle holding the Tiptap editor with
	 *      the @/slash autocomplete panels, the return-glyph send cluster on its
	 *      right (send · send-while-streaming · Stop · creating-worktree
	 *      spinner), and the ControlsDock's bare-text controls row. The dictate
	 *      and voice-call buttons are passed to the dock as its `voiceActions`
	 *      snippet so they stay bound to this component's recording state.
	 *
	 * Delegates voice recording to VoiceRecorder, controls to ControlsDock, and
	 * drag-and-drop to the inline overlay.
	 */
	import { tick, onDestroy, onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { activeSession } from '$lib/stores/active-session';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { composerSuggestion, suggestionKey, hasComposerDraft } from '$lib/stores/session/composer-suggestion';
	import { persistVoiceContext, voiceContextFor } from '$lib/services/voiceExecutionContext';
	import { attachComposerDictation, detachComposerDictation, startComposerDictation } from '$lib/services/composerDictation';
	import { readDraft, writeDraft } from '$lib/stores/session/draft-persistence';
	import { promptStash, type StashedPrompt } from '$lib/stores/session/prompt-stash';
	import { promptHistory, createPromptRecall } from '$lib/stores/session/prompt-recall';
	import { pastedTextFile } from './tiptap/large-paste';
	import PromptStashMenu from './PromptStashMenu.svelte';
	import { sessionOwnerEpoch } from '$lib/stores/session-owner';
	import { getInitGeneration } from '$lib/stores/session/session-init';
	import { appData, profiles, sessions } from '$lib/stores/app-data.svelte';
	import { hasStoppableWork } from './stoppable-work';
	import { agentGate, anyCredential } from '$lib/stores/auth.svelte';

	import { api, type FileUploadResponse } from '$lib/api/client';
	import { notify } from '$lib/stores/notifications';
	import { evaluateSubmitGuards, sendButtonState } from './submit-guards';
	import { attachmentBlockReason } from './attach-disable';
	// Collapse / gesture / sheet behaviour is the mobile app's; the web build
	// gets the desktop no-op defaults (see composer-shell.ts).
	import { composerShell } from './composer-shell';
	import { platform } from '$lib/stores/platform.svelte';
	import Banner from '$lib/components/Banner.svelte';
	import { detectProjectMismatch } from './project-mismatch';
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
		onBrowserToggle?: () => void;
		onBrowserPreload?: () => void;
		browserOpen?: boolean;
	}

	let { compact = false, onBrowserToggle, onBrowserPreload, browserOpen = false }: Props = $props();
	// ChatView remounts the editor when its account changes. Teardown and
	// already queued callbacks must not write the previous account's text.
	const ownerEpoch = sessionOwnerEpoch();
	const ownsComposer = () => ownerEpoch === sessionOwnerEpoch();

	// The lane this composer belongs to. `$activeSession.mode` — not the
	// sidepanel's `appMode` — is the authority: it is the session's own mode
	// once one is open, and only falls back to the sidepanel's pick on a fresh
	// compose screen.
	const composerMode = $derived($activeSession.mode);
	// Remote runtime summaries are display/control truth, not transcript streaming
	// state. A parked turn can also retain background work that Stop must reach.
	const stoppableWork = $derived(hasStoppableWork(
		$activeSession,
		$sessions.find(session => session.id === $activeSession.sessionId)
	));
	const commandSettingsKey = $derived.by(() => {
		const config = $profiles.find(profile => profile.id === $activeSession.profile)?.config;
		return JSON.stringify([config?.setting_sources ?? null, config?.cwd ?? null, config?.provider ?? null, config?.model ?? null]);
	});

	$effect(() => {
		const state = $activeSession;
		if (!ownsComposer()) return;
		if (state.mode !== 'voice' || !state.sessionId || state.isRestoringSession) return;
		const owner = state.sessionId;
		void persistVoiceContext(owner, voiceContextFor(state)).catch(() => {
			if (get(activeSession).sessionId === owner) {
				notify('Could not save voice execution settings. Try again before requesting work.', { severity: 'error' });
			}
		});
	});

	// The invitation has to match the lane. Code mode dispatches work against a
	// repository; chat and voice are plain conversations with nothing to point
	// work at, so promising "a task" there would be a lie.
	const composerPlaceholder = $derived(
		composerMode !== 'chat' ? 'Describe a task or ask a question' : 'Ask anything'
	);

	// Voice passes local file references to its worker rather than uploading
	// binary data to the Realtime text channel.
	const attachSupported = true;

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
	const hasMessageContent = $derived(hasComposerDraft(inputValue) || uploadedFiles.length > 0);
	let draftSaveTimeout: ReturnType<typeof setTimeout> | null = null;
	// Flag to prevent onDestroy from overwriting draft after we've already cleared it.
	// Using plain variable (not $state) to avoid reactivity issues with onDestroy capture.
	let messageSent = false;
	// A new chat's remembered project and branch resolve against the project
	// catalog. A send before it answers is held with the text in place (#1636).
	let heldForCatalog = $state(false);
	// After a hold that ended without a usable catalog, the next send is the
	// user's answer and goes out as composed.
	let catalogHoldWaived = false;
	const CATALOG_HOLD_MS = 10_000;
	const initialSessionId = $activeSession.sessionId;

	// Get the draft value as JSON string (preserves file chips)
	function getDraftJSON(): string {
		const json = editorRef?.getJSON();
		if (!json) return inputValue;
		return JSON.stringify(json);
	}

	// Helper to save draft immediately (used on unmount and on send)
	function saveDraftImmediately(value?: string) {
		if (!ownsComposer()) return;
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
			if (!ownsComposer()) return;
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
		detachComposerDictation(editorRef?.getEditor());
		if (!ownsComposer()) return;
		if (messageSent) return;
		const currentSessionId = get(activeSession).sessionId;
		if (currentSessionId !== initialSessionId) return; // session switched
		saveDraftImmediately();
	});

	let isUploading = $state(false);
	let isRecording = $state(false);
	let isTranscribing = $state(false);
	const sendState = $derived(sendButtonState({
		hasContent: hasMessageContent,
		hasCredential: $anyCredential,
		isUploading,
		isRecording,
		isTranscribing,
		autoBranch: $activeSession.worktreeIntent === 'worktree' && !$activeSession.worktreeId
	}));
	let editorRef = $state<TiptapEditor | null>(null);
	let prevSessionId = $state($activeSession.sessionId);

	// Subscribe synchronously: effects may coalesce a short live→idle transition.
	onMount(() => {
		let session = get(activeSession);
		let enabled = chatPreferences.current.suggestNextResponse;
		const observe = () => composerSuggestion.observe({
			sessionId: session.sessionId, mode: session.mode, enabled,
			turnId: session.liveTurnId, streaming: session.isStreaming,
			blocked: hasComposerDraft(session.draft) || !!session.error,
			connected: session.wsConnected
		});
		const stopSession = activeSession.subscribe(value => { session = value; observe(); });
		const stopPrefs = chatPreferences.subscribe(value => { enabled = value.suggestNextResponse; observe(); });
		return () => { stopSession(); stopPrefs(); composerSuggestion.detach(); };
	});
	$effect(() => {
		if (hasComposerDraft(inputValue) || uploadedFiles.length || isUploading) composerSuggestion.invalidate();
	});
	const ghostReply = $derived($composerSuggestion && !hasComposerDraft(inputValue) && !uploadedFiles.length &&
		!isUploading && !$activeSession.isStreaming && $chatPreferences.suggestNextResponse ? $composerSuggestion : '');
	function acceptSuggestion() {
		if (!ghostReply || !editorRef) return;
		const text = composerSuggestion.accept();
		if (text) { editorRef.setPlainText(text); editorRef.focus(); }
	}

	// Sync editor content when session changes (ChatInput is NOT keyed by sessionId).
	// Because this single component is reused across sessions, it is solely
	// responsible for moving draft text between the editor and the correct
	// session — getting this wrong is what made text "leak" between sessions.
	$effect(() => {
		const newSessionId = $activeSession.sessionId;
		if (!ownsComposer()) return;
		if (newSessionId !== prevSessionId) {
			// Move capture off the shared editor before replacing its document.
			detachComposerDictation(editorRef?.getEditor());
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
			recall.reset();
			stashOpen = false;
			// Prefer the in-memory draft; fall back to the sessionStorage mirror
			// (populated across reloads) when the cache has nothing.
			const newDraft = $activeSession.draft || readDraft(newSessionId) || '';
			inputValue = newDraft;
			plainTextValue = '';
			editorRef?.setContent(newDraft);
		}
		attachComposerDictation(editorRef?.getEditor(), newSessionId);
	});

	// Restore a removed user prompt back into the editor so the user can edit or
	// resend it. Set by the backend's `user_message_discarded` event when a turn
	// is stopped before any assistant output, by a successful rewind that
	// truncated away the target user message, and by unsending a pending
	// message. An empty editor takes the prompt as is; text the user already
	// typed is kept and the prompt goes in front of it, so neither is lost.
	// Either way clear the pending prompt so it fires exactly once.
	$effect(() => {
		const pending = $activeSession.pendingRestorePrompt;
		if (!ownsComposer()) return;
		if (!pending) return;
		const current = (editorRef?.getMessage() ?? inputValue ?? '').trim();
		if (current.length === 0) {
			inputValue = pending;
			plainTextValue = pending;
			editorRef?.setContent(pending);
		} else {
			setComposerDraft(joinDrafts(textDoc(pending), getDraftJSON()));
		}
		recall.reset();
		tick().then(() => editorRef?.focus());
		activeSession.clearPendingRestorePrompt();
	});

	/** Plain text as a composer document, one paragraph per line. */
	function textDoc(text: string): string {
		return JSON.stringify({
			type: 'doc',
			content: text.split('\n').map(line => ({
				type: 'paragraph',
				...(line ? { content: [{ type: 'text', text: line }] } : {})
			}))
		});
	}

	/** Replace the composer's content and persist it at once. */
	function setComposerDraft(draft: string) {
		editorRef?.setContent(draft);
		inputValue = editorRef?.getMessage() ?? draft;
		plainTextValue = editorRef?.getText() ?? '';
		saveDraftImmediately(draft);
	}

	// ── Prompt recall (ArrowUp in an empty composer) ──────────────────────────
	const recall = createPromptRecall();

	function composerIsEmpty(): boolean {
		return !hasComposerDraft(getDraftJSON()) && uploadedFiles.length === 0;
	}

	/** Step through earlier prompts; true when the key was used. */
	function recallPrompt(direction: 'up' | 'down'): boolean {
		const history = promptHistory(get(activeSession).messages);
		const step = recall.step(direction, history, composerIsEmpty());
		if (!step) return false;
		if (step.text) editorRef?.setPlainText(step.text);
		else editorRef?.clear();
		inputValue = editorRef?.getMessage() ?? step.text;
		plainTextValue = editorRef?.getText() ?? step.text;
		editorRef?.getEditor()?.commands.focus('end');
		return true;
	}

	// ── Prompt stash (Ctrl/Cmd+S) ─────────────────────────────────────────────
	let stashOpen = $state(false);

	function offerUndo(message: string, undo: () => void) {
		const id = notify.banner(message, {
			severity: 'info',
			persist: false,
			duration_ms: 6000,
			action: { label: 'Undo', onClick: () => { notify.dismissLive(id); undo(); } }
		});
	}

	/** Set the composer's draft aside. With nothing to stash, open the list. */
	function stashDraft() {
		const draft = getDraftJSON();
		if (!hasComposerDraft(draft)) {
			if ($promptStash.length) stashOpen = !stashOpen;
			else notify('Nothing to stash — type a prompt, then press Ctrl+S.', { severity: 'info' });
			return;
		}
		const entry = promptStash.push(draft);
		if (!entry) return;
		setComposerDraft('');
		recall.reset();
		composerSuggestion.invalidate();
		offerUndo('Prompt stashed', () => {
			const back = promptStash.take(entry.id);
			if (back) restoreEntry(back);
		});
	}

	/** Put a stashed prompt in the composer, stashing whatever is there first. */
	function restoreEntry(entry: StashedPrompt) {
		const current = getDraftJSON();
		const swapped = hasComposerDraft(current) ? promptStash.push(current) : null;
		setComposerDraft(entry.draft);
		recall.reset();
		stashOpen = false;
		tick().then(() => editorRef?.getEditor()?.commands.focus('end'));
		if (swapped) notify('Your previous draft was stashed.', { severity: 'info' });
	}

	function restoreStashed(id: string) {
		const entry = promptStash.take(id);
		if (entry) restoreEntry(entry);
	}

	function deleteStashed(id: string) {
		const entry = promptStash.take(id);
		if (entry) offerUndo('Stashed prompt deleted', () => promptStash.putBack(entry));
	}

	// ── Large paste → text attachment ─────────────────────────────────────────
	/** Take a large paste as a .txt attachment; false keeps it inline. */
	function handleLargePaste(text: string): boolean {
		if (!attachSupported || !$anyCredential || isUploading) return false;
		if (attachmentBlockReason($activeSession.project, $activeSession.projectV2Id, composerMode, $activeSession.sessionId)) return false;
		const file = pastedTextFile(text);
		isUploading = true;
		uploadComposerFile(file)
			.then(result => {
				editorRef?.insertFileChip({
					path: result.path,
					filename: result.filename,
					fullPath: result.full_path,
					size: result.size,
					type: 'file'
				});
				inputValue = editorRef?.getMessage() ?? '';
			})
			.catch((error: unknown) => {
				// Never lose the paste: fall back to inline.
				editorRef?.insertPlainText(text);
				inputValue = editorRef?.getMessage() ?? '';
				const err = error as { detail?: string };
				errorMessage = `Couldn't attach the pasted text (${err.detail || 'upload failed'}), so it was pasted inline.`;
			})
			.finally(() => { isUploading = false; });
		return true;
	}

	let fileInputRef = $state<HTMLInputElement | null>(null);

	// Voice recording state (bindable from VoiceRecorder) is declared with
	// isUploading above, since the send button's state reads all three.

	// Error display — shared between file upload errors and voice recording errors
	let errorMessage = $state('');

	// Wrong-project nudge: a new chat's draft names another registered project
	// (and not the selected one). Offer a one-click switch before sending.
	let dismissedMismatch = $state<string | null>(null);
	const projectMismatch = $derived(
		!$activeSession.sessionId && $activeSession.mode !== 'chat' && plainTextValue.length >= 8
			? detectProjectMismatch(plainTextValue, appData.current.projects, $activeSession.project)
			: null
	);

	// Autocomplete state
	let showCommandAutocomplete = $state(false);
	let showFileAutocomplete = $state(false);
	let composerFocused = $state(false);
	let mobileControlsOpen = $state(false);
	let controlsDockRef = $state<ControlsDock | null>(null);
	function composerOpenGesture(node: HTMLFormElement) {
		return composerShell().attachComposerOpen(node, {
			enabled: () => isMobileComposerViewport(),
			allowEditor: () => !inputValue.trim() && !composerFocused,
			open: () => mobileControlsOpen,
			openDirection: () => compact ? 'up' : 'down',
			onOpenChange: (open) => controlsDockRef?.setMobileSheetOpen(open)
		});
	}
	let commandJustSelected = false;
	let commandAutocompleteRef = $state<CommandAutocomplete | null>(null);
	let fileAutocompleteRef = $state<FileAutocomplete | null>(null);

	// Island ref
	let islandRef = $state<HTMLDivElement | null>(null);
	let mobileComposerExpanded = $state(false);
	// Mobile buttons can lose focus when disabled (voice preparation), and touch
	// browsers need not focus them at all. Retain interaction ownership until the
	// user explicitly moves outside the composer, rather than relying on focus.
	let composerInteractionOwned = false;
	/** The mobile composer shell is a platform decision, not a window width. */
	function isMobileComposerViewport(): boolean {
		return platform.isMobile;
	}
	// `compact` means "not the empty-hero layout" and is true on desktop as soon
	// as a session has messages. Collapse / toolbar-inert is mobile-shell only.
	const mobileViewport = $derived(platform.isMobile);
	const mobileCollapsed = $derived(
		composerShell().shouldCollapse({
			compact,
			mobileViewport,
			hasDraft: inputValue.trim().length > 0,
			expanded: mobileComposerExpanded,
			composerFocused,
			controlsOpen: mobileControlsOpen,
			commandAutocompleteOpen: showCommandAutocomplete,
			fileAutocompleteOpen: showFileAutocomplete
		})
	);
	let expandPointerActive = $state(false);
	const toolbarPointerBlocked = $derived(
		composerShell().shouldBlockToolbarPointer({
			collapsed: mobileCollapsed,
			expandPointerActive,
			mobileViewport
		})
	);

	let mobileReconcileRaf: number | null = null;
	let mobileComposerDestroyed = false;
	let expandPointerCleanup: (() => void) | null = null;
	let expandPointerTimeout: ReturnType<typeof setTimeout> | null = null;

	function isComposerOwnedNode(target: EventTarget | Node | null): boolean {
		if (!(target instanceof Node)) return false;
		if (islandRef?.contains(target)) return true;
		return target instanceof Element && !!target.closest('[data-composer-owned-overlay="true"]');
	}

	function reconcileMobileComposer() {
		if (!isMobileComposerViewport()) return;
		mobileComposerExpanded = composerShell().reconcileExpanded(
			mobileComposerExpanded,
			{
				compact,
				hasDraft: inputValue.trim().length > 0,
				composerFocused,
				controlsOpen: mobileControlsOpen,
				commandAutocompleteOpen: showCommandAutocomplete,
				fileAutocompleteOpen: showFileAutocomplete,
				activeElementOwned: isComposerOwnedNode(document.activeElement),
				interactionOwned: composerInteractionOwned
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

	function clearExpandPointerGuard() {
		expandPointerCleanup?.();
		expandPointerCleanup = null;
		if (expandPointerTimeout !== null) {
			clearTimeout(expandPointerTimeout);
			expandPointerTimeout = null;
		}
		expandPointerActive = false;
	}

	function armExpandPointerGuard() {
		if (!mobileCollapsed || expandPointerActive) return;
		expandPointerActive = true;
		const onFinish = () => {
			expandPointerCleanup = null;
			window.removeEventListener('pointerup', onFinish, true);
			window.removeEventListener('pointercancel', onFinish, true);
			// pointerup is followed by click in the same turn; release after it.
			expandPointerTimeout = setTimeout(() => {
				expandPointerTimeout = null;
				expandPointerActive = false;
			}, 0);
		};
		window.addEventListener('pointerup', onFinish, true);
		window.addEventListener('pointercancel', onFinish, true);
		expandPointerCleanup = () => {
			window.removeEventListener('pointerup', onFinish, true);
			window.removeEventListener('pointercancel', onFinish, true);
		};
	}

	function keepMobileComposerExpanded() {
		if (isMobileComposerViewport()) mobileComposerExpanded = true;
	}

	function isComposerActionNode(target: EventTarget | null): boolean {
		return target instanceof Element && !!target.closest('.controls-dock, [data-composer-owned-overlay="true"]');
	}

	function handleComposerPointerDown(event: PointerEvent) {
		if (!isMobileComposerViewport()) return;
		armExpandPointerGuard();
		composerInteractionOwned = isComposerActionNode(event.target);
		mobileComposerExpanded = true;
	}

	function handleDocumentFocusIn(event: FocusEvent) {
		if (!isMobileComposerViewport()) return;
		// Body focus is a side effect of disabled controls/native pickers, not a
		// deliberate keyboard move to another control.
		if (event.target === document.body || event.target === document.documentElement) return;
		composerInteractionOwned = isComposerActionNode(event.target);
		scheduleMobileComposerReconciliation();
	}

	function handleDocumentPointerDown(event: PointerEvent) {
		if (!compact || !isMobileComposerViewport()) return;
		if (isComposerOwnedNode(event.target)) {
			armExpandPointerGuard();
			composerInteractionOwned = isComposerActionNode(event.target);
			mobileComposerExpanded = true;
			return;
		}
		// A transcript/background tap is an explicit request to leave the empty
		// composer. Blurring first makes the deferred ownership check deterministic
		// even when the tapped element itself cannot receive focus.
		composerInteractionOwned = false;
		if (isComposerOwnedNode(document.activeElement) && document.activeElement instanceof HTMLElement) {
			document.activeElement.blur();
		}
		editorRef?.blur();
		scheduleMobileComposerReconciliation();
	}

	onDestroy(() => {
		mobileComposerDestroyed = true;
		if (mobileReconcileRaf !== null) cancelAnimationFrame(mobileReconcileRaf);
		clearExpandPointerGuard();
	});

	onMount(() => {
		document.addEventListener('pointerdown', handleDocumentPointerDown);
		document.addEventListener('focusin', handleDocumentFocusIn);
		return () => {
			document.removeEventListener('pointerdown', handleDocumentPointerDown);
			document.removeEventListener('focusin', handleDocumentFocusIn);
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
		if (!ownsComposer() || heldForCatalog) return;
		if (composerMode !== 'chat' && !catalogHoldWaived && activeSession.composerAwaitingProjectCatalog) {
			if (!(await holdForProjectCatalog())) return;
		}
		// Get serialized message from editor (includes inline @path references)
		const finalPrompt = editorRef?.getMessage() ?? '';

		// Surface WHY a send is blocked instead of silently swallowing the click,
		// which read as a dead app. The guards keep the message in the composer and
		// (except for an empty composer) name the missing piece with a toast.
		const block = evaluateSubmitGuards({
			hasPrompt: !!finalPrompt.trim() || uploadedFiles.length > 0,
			isUploading,
			isCreatingWorktree: $activeSession.isCreatingWorktree,
			hasProfile: !!$activeSession.profile,
			hasAnyProfile: $profiles.length > 0,
			hasProject: !!$activeSession.project || !!$activeSession.projectV2Id,
			// The project guard is a code-lane requirement; the projectless lanes
			// would otherwise refuse every send.
			mode: composerMode
		});
		if (block) {
			if (block.notice) notify(block.notice.message, { severity: block.notice.tone });
			return;
		}

		// Append any legacy uploaded files (from file upload button) to the end
		// Note: Files added via @ autocomplete are now inline chips, not in uploadedFiles
		let messageToSend = finalPrompt;
		if (uploadedFiles.length > 0) {
			const fileRefs = uploadedFiles.map(f => `@${f.path}`).join(' ');
			messageToSend = [finalPrompt.trim(), fileRefs].filter(Boolean).join('\n\n');
		}

		// Send the message
		sendMessage(messageToSend);
	}

	/**
	 * Hold a send until the project catalog answers, so a new chat's
	 * remembered project and branch are in place first (#1636). The text stays
	 * in the composer throughout. True when the send may go on.
	 */
	async function holdForProjectCatalog(): Promise<boolean> {
		const navigation = getInitGeneration();
		heldForCatalog = true;
		let timer: ReturnType<typeof setTimeout> | undefined;
		const loaded = await Promise.race([
			appData.whenProjectCatalogLoaded().then(() => true),
			new Promise<boolean>(resolve => { timer = setTimeout(() => resolve(false), CATALOG_HOLD_MS); }),
		]);
		clearTimeout(timer);
		heldForCatalog = false;
		// The view moved on: the text stays with the view it was typed in.
		if (!ownsComposer() || getInitGeneration() !== navigation || get(activeSession).sessionId) return false;
		if (loaded) activeSession.resolveComposerDefaultsAfterCatalog();
		if (!loaded || appData.projectCatalogFailed) {
			catalogHoldWaived = true;
			notify("Projects didn't load, so your usual project and branch may not be set. Check them, then send again.", { severity: 'warning' });
			return false;
		}
		return true;
	}

	/** Two composer drafts as one, earlier first. Drafts are Tiptap JSON, or
	 *  plain text when no editor was mounted. */
	function joinDrafts(earlier: string, later: string): string {
		try {
			const a = JSON.parse(earlier), b = JSON.parse(later);
			if (a?.type === 'doc' && b?.type === 'doc') {
				return JSON.stringify({ ...a, content: [...(a.content ?? []), ...(b.content ?? [])] });
			}
		} catch { /* plain text */ }
		return [earlier, later].join('\n\n');
	}

	// Actually send the message. There is no send-later fork: a send during a live
	// turn still goes out, and the backend decides when the model sees it (mid-turn
	// steer, or the start of the next turn). Until then it renders as a pending
	// bubble at the tail of the transcript.
	async function sendMessage(finalPrompt: string) {
		if (!ownsComposer()) return;
		composerSuggestion.invalidate();
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
		const sendingSessionId = $activeSession.sessionId;
		const sendingNavigation = getInitGeneration();
		// A refusal names its reason by setting the error or worktree banner. One
		// already showing from an earlier attempt is not that reason, so watch
		// for either to be set during this send.
		const reasonAtSend = { error: $activeSession.error, worktreeError: $activeSession.worktreeError };
		const reasonSet = { error: false, worktreeError: false };
		const stopWatchingReason = activeSession.subscribe(s => {
			if (s.error !== reasonAtSend.error) reasonSet.error = true;
			if (s.worktreeError !== reasonAtSend.worktreeError) reasonSet.worktreeError = true;
		});
		const priorDraftJSON = getDraftJSON();
		const priorInputValue = inputValue;
		const priorUploadedFiles = uploadedFiles;

		inputValue = '';
		uploadedFiles = [];
		recall.reset();

		// Clear editor content
		editorRef?.clear();

		// Clear draft immediately (not debounced) - important because
		// the component may be destroyed/recreated when isEmptyState changes
		saveDraftImmediately('');

		const restoreOnFailure = () => {
			if (!ownsComposer()) return;
			// A move to another view hands the text to the session it was typed
			// in. A New Chat adopting the session its own create made (a worktree
			// session whose first message was then refused) is still this view
			// (#1636).
			const adoptedOwnSession = sendingSessionId === null && getInitGeneration() === sendingNavigation;
			if ($activeSession.sessionId !== sendingSessionId && !adoptedOwnSession) {
				writeDraft(sendingSessionId, priorDraftJSON);
				return;
			}
			// Every refusal names its reason in the session's error or worktree
			// banner; this covers one that does not, including one that left an
			// earlier attempt's banner standing.
			const now = get(activeSession);
			if (!(now.error && reasonSet.error) && !(now.worktreeError && reasonSet.worktreeError)) {
				notify('Message not sent. Try again.', { severity: 'warning' });
			}
			// A slow create can fail after the user started a new message: put
			// the refused one back in front of it instead of replacing it.
			const laterDraft = getDraftJSON();
			const restored = hasComposerDraft(laterDraft) ? joinDrafts(priorDraftJSON, laterDraft) : priorDraftJSON;
			uploadedFiles = [...priorUploadedFiles, ...uploadedFiles];
			editorRef?.setContent(restored);
			inputValue = editorRef?.getMessage() ?? priorInputValue;
			saveDraftImmediately(restored);
			messageSent = false;
		};

		const collapseMobileComposer = () => {
			if (!isMobileComposerViewport()) return;
			// The dock owns the drawer state; resetting the expansion latch alone
			// leaves the drawer open and prevents the composer from collapsing.
			controlsDockRef?.setMobileSheetOpen(false);
			// Closing the drawer restores focus to its toolbar trigger. Release
			// that focus too, or deferred reconciliation reopens the empty composer.
			if (isComposerOwnedNode(document.activeElement) && document.activeElement instanceof HTMLElement) {
				document.activeElement.blur();
			}
			composerInteractionOwned = false;
			mobileComposerExpanded = composerShell().reconcileExpanded(
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
			else if (ownsComposer() && !mobileComposerDestroyed && getInitGeneration() === sendingNavigation) {
				// A first send can adopt a new session id before it resolves (notably
				// worktree creation). Only actual navigation retires its drawer close.
				collapseMobileComposer();
			}
		} catch (err) {
			console.error('[ChatInput] sendMessage threw', err);
			restoreOnFailure();
		} finally {
			stopWatchingReason();
		}
	}

	// Handle keydown for textarea
	function handleKeyDown(e: KeyboardEvent) {
		// Ctrl/Cmd+S stashes the draft. Only while the composer has focus: the
		// browser's own Save keeps working everywhere else.
		if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 's' && !e.isComposing) {
			e.preventDefault();
			if (!e.repeat) stashDraft();
			return true;
		}
		// Never turn an IME confirmation, held key, or modifier shortcut into Send.
		if (e.isComposing || e.keyCode === 229 || e.repeat || e.ctrlKey || e.altKey || e.metaKey) return;
		if (ghostReply) {
			const action = suggestionKey(e);
			if (action) {
				e.preventDefault();
				e.stopImmediatePropagation();
				if (action === 'accept') acceptSuggestion();
				else composerSuggestion.invalidate();
				return true;
			}
		}
		// Let command autocomplete handle the event first
		if (commandAutocompleteRef && showCommandAutocomplete) {
			const handled = commandAutocompleteRef.handleKeyDown(e);
			if (handled) return;
		}

		// Let file autocomplete handle the event
		if (fileAutocompleteRef && showFileAutocomplete) {
			const handled = fileAutocompleteRef.handleKeyDown(e, editorRef?.getText() ?? plainTextValue);
			if (handled) return;
		}

		// ArrowUp in an empty composer recalls earlier prompts; ArrowDown steps
		// back toward the empty composer. Editing a recalled prompt ends recall.
		if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && !e.shiftKey && (recall.active || e.key === 'ArrowUp')) {
			if (recallPrompt(e.key === 'ArrowUp' ? 'up' : 'down')) {
				e.preventDefault();
				return true;
			}
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
		inputValue = editorRef?.getMessage() ?? '';
		showFileAutocomplete = false;

		tick().then(() => editorRef?.focus());
	}

	// File upload
	//
	// The chat lane is projectless, so its files cannot go to the project
	// endpoint. They go to one shared directory under the workspace root — not
	// one per session, because a brand-new chat has no session id at the moment
	// the user picks a file. `CHAT_MODE_TOOL_SURFACE` grants that lane a `Read`
	// pinned to this directory, which is what makes the resulting @ref openable.
	// Mirrors `CHAT_UPLOADS_DIRNAME` in app/core/session_modes.py.
	const CHAT_UPLOADS_DIRNAME = 'chat-uploads';

	/**
	 * Upload one composer attachment to whichever endpoint this lane owns.
	 * Both call sites (the picker and the drop handler) go through here so the
	 * two can never drift onto different targets.
	 */
	function uploadComposerFile(file: File): Promise<FileUploadResponse> {
		if (composerMode !== 'chat' && !$activeSession.project && $activeSession.sessionId && $activeSession.projectV2Id) {
			return api.uploadFile(`/sessions/${encodeURIComponent($activeSession.sessionId)}/upload`, file);
		}
		if (composerMode === 'chat') {
			return api.uploadFile('/workspace/upload', file, undefined, undefined, CHAT_UPLOADS_DIRNAME);
		}
		return api.uploadFile(
			`/projects/${$activeSession.project}/upload`,
			file,
			$activeSession.sessionId ?? undefined,
			$activeSession.worktreeIntent === 'worktree' ? $activeSession.worktreeId ?? undefined : undefined
		);
	}

	function triggerFileUpload() {
		const block = attachmentBlockReason(
			$activeSession.project,
			$activeSession.projectV2Id,
			composerMode,
			$activeSession.sessionId
		);
		if (block) {
			errorMessage = block;
			return;
		}
		fileInputRef?.click();
	}

	async function handleFileUpload(event: Event) {
		const input = event.target as HTMLInputElement;
		const files = input.files;
		if (!files || files.length === 0) return;
		// A code-lane upload needs the project in the URL; a chat-lane one has no
		// project by construction and must not be turned away for missing it.
		if (attachmentBlockReason($activeSession.project, $activeSession.projectV2Id, composerMode, $activeSession.sessionId)) return;

		isUploading = true;
		try {
			for (const file of Array.from(files)) {
				const result = await uploadComposerFile(file);
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
			inputValue = editorRef?.getMessage() ?? '';
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
		if (!ownsComposer()) return;
		editorRef?.appendText(text);
		inputValue = editorRef?.getMessage() ?? '';
		await tick();
		editorRef?.focus();
	}

	// Live dictation writes straight into the editor as it is spoken. The editor
	// owns a span for it and replaces that span on every update, so a transcript
	// that revises a word mid-sentence corrects in place instead of stuttering.
	function handleLiveText(text: string) {
		if (!ownsComposer()) return;
		editorRef?.streamText(text);
		inputValue = editorRef?.getMessage() ?? '';
	}

	function handleLiveCommit() {
		if (!ownsComposer()) return;
		editorRef?.commitDictation();
		inputValue = editorRef?.getMessage() ?? '';
	}

	function handleLiveDiscard() {
		editorRef?.discardDictation();
		inputValue = editorRef?.getMessage() ?? '';
	}

	// Drag and drop file upload with errors shown by the composer.
	let isDragging = $state(false);

	function handleDragEnter(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
		// Voice has no upload target at all (and no attach button in the dock), so
		// its drop zone never invites a drop it would only have to refuse.
		if (!attachSupported) return;
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
		// Voice has nowhere to put a dropped file; ignore it rather than banner an
		// error the user cannot act on.
		if (!attachSupported) return;

		const dropBlock = attachmentBlockReason(
			$activeSession.project,
			$activeSession.projectV2Id,
			composerMode,
			$activeSession.sessionId
		);
		if (dropBlock) {
			errorMessage = dropBlock;
			return;
		}

		isUploading = true;
		try {
			for (const file of Array.from(e.dataTransfer.files)) {
				const result = await uploadComposerFile(file);
				editorRef?.insertFileChip({
					path: result.path,
					filename: result.filename,
					fullPath: result.full_path,
					size: result.size,
					type: 'file'
				});
			}
			inputValue = editorRef?.getMessage() ?? '';
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
	class:mobile-toolbar-blocked={toolbarPointerBlocked}
	class:mobile-controls-open={mobileControlsOpen}
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

		{#if projectMismatch && dismissedMismatch !== projectMismatch.id}
			<Banner
				severity="info"
				message={`This looks like it's about ${projectMismatch.name}. Start it there instead?`}
				action={{ label: `Switch to ${projectMismatch.name}`, onClick: () => activeSession.setProject(projectMismatch.id) }}
				dismissible
				onDismiss={() => (dismissedMismatch = projectMismatch?.id ?? null)}
			/>
		{/if}

		<!-- Error Message (upload errors, voice recording errors) -->
		{#if errorMessage}
			<Banner severity="error" message={errorMessage} dismissible onDismiss={() => (errorMessage = '')} />
		{/if}

		<!-- Agents cannot start here yet (no license, or no credential for the
		     chosen Claude Code mode): say what is missing instead of failing later. -->
		{#if $agentGate && !$agentGate.ready && $agentGate.reason && (!$anyCredential || $agentGate.license_allows_runs === false)}
			<Banner severity="warning" message={$agentGate.reason} />
		{/if}

		<!-- Worktree Error Message -->
		{#if $activeSession.worktreeError}
			<Banner severity="warning" message={$activeSession.worktreeError} dismissible onDismiss={() => activeSession.clearWorktreeError()} />
		{/if}

		<form
			use:composerOpenGesture
			onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}
			onpointerdown={handleComposerPointerDown}
		>
			<!-- Context chips: agent · runner · repository · branch · worktree.
			     A loose row floating ABOVE the box with a visible gap, in both the
			     new-chat and active states (ContextPillBar decides which chips are
			     selectors and which are read-only). Mobile hides this row; the
			     mobile sheet's Chat-setup section carries the same selectors.

			     Chat still requires an agent. ContextPillBar keeps its selector
			     visible while hiding the repository and execution controls. -->
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
							model={$activeSession.modelOverride ?? undefined}
							profileId={$activeSession.profile || undefined}
							settingsKey={commandSettingsKey}
							mode={$activeSession.mode}
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
							onBeforeDestroy={detachComposerDictation}
							placeholder={composerPlaceholder}
							suggestion={ghostReply}
							onAcceptSuggestion={acceptSuggestion}
							initialContent={initialComposerDraft}
							disabled={!$anyCredential}
							onTextChange={(text) => {
								inputValue = text;
								// Also update plain text for autocomplete (without chip @path serialization)
								plainTextValue = editorRef?.getText() ?? '';
								handleInputChange();
							}}
							onUserInput={() => { composerSuggestion.invalidate(); recall.reset(); }}
							onKeyDown={handleKeyDown}
							onLargePaste={handleLargePaste}
							onFocusChange={(focused) => {
								composerFocused = focused;
								if (focused) keepMobileComposerExpanded();
								else scheduleMobileComposerReconciliation();
							}}
						/>
					</div>

					<!-- Stop must remain reachable whenever session work is live,
					     including with a draft. Send can queue that draft independently.
					     StopButton confirms first when background agents are live. -->
					<div class="send-cluster">
						<PromptStashMenu
							entries={$promptStash}
							open={stashOpen}
							onOpenChange={(open) => { stashOpen = open; }}
							onRestore={restoreStashed}
							onDelete={deleteStashed}
						/>
						{#if $activeSession.isCreatingWorktree || heldForCatalog}
							<!-- Creating worktree, or a send held for the project catalog - show spinner -->
							{#if heldForCatalog}<span class="sr-only" role="status">Loading projects…</span>{/if}
							<button type="button" class="send-btn creating-worktree" disabled title={heldForCatalog ? 'Loading projects…' : 'Creating worktree...'} aria-label={heldForCatalog ? 'Loading projects…' : 'Creating worktree'}>
								<svg class="w-3.5 h-3.5 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
									<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
								</svg>
							</button>
						{:else if stoppableWork}
							{#if hasMessageContent}
								<button type="submit" class="send-btn" disabled={sendState.disabled} title={sendState.disabled ? sendState.title : 'Send — Enter'} aria-label="Send">
									<CornerDownLeft size={13} strokeWidth={2.2} aria-hidden="true" />
								</button>
							{/if}
							<StopButton
								activeTasks={$activeSession.activeTasks}
								onStop={() => { composerSuggestion.invalidate(); activeSession.stopGeneration(); }}
							/>
						{:else}
							<button
								type="submit"
								class="send-btn"
								disabled={sendState.disabled}
								title={sendState.title}
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
					bind:this={controlsDockRef}
					{onBrowserToggle}
					{onBrowserPreload}
					{browserOpen}
					{isUploading}
					inertToolbar={toolbarPointerBlocked}
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
							onLiveText={handleLiveText}
							onLiveCommit={handleLiveCommit}
							onLiveDiscard={handleLiveDiscard}
							getOwner={() => get(activeSession).sessionId}
							sessionId={$activeSession.sessionId}
							onStart={() => {
								const editor = editorRef?.getEditor();
								if (editor) startComposerDictation(editor, get(activeSession).sessionId);
							}}
							disabled={!$anyCredential || isUploading}
							bind:isRecording
							bind:isTranscribing
						/>

						<!-- Voice call (VM19): starts a call on this conversation. On a
						     fresh composer it creates the conversation from the
						     composer's picks first (#1616); it shows disabled with the
						     reason while the session is working or parked. -->
						<VoiceCallButton
							disabled={isUploading || isRecording || isTranscribing}
							disabledReason={isUploading ? 'Uploading files...' : isRecording ? 'Recording...' : isTranscribing ? 'Transcribing...' : null}
						/>
					{/snippet}
				</ControlsDock>
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

	:global(html[data-platform='desktop']) .chat-input-wrapper {
		padding: 0 1.5rem 1.25rem;
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
		align-items: center;
		gap: 8px;
		padding: 12px 12px 4px 14px;
		min-height: 70px;
	}

	/* Desktop: give the empty editor a taller hit area. The caret and
	   placeholder stay vertically centred in that box (TiptapEditor) so they
	   line up with the send button. Don't stretch ProseMirror itself — that
	   parked the caret at the top while the placeholder sat in the middle. */
	:global(html[data-platform='desktop']) .textarea-wrapper :global(.tiptap-editor) {
		min-height: 3.4em;
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

	@keyframes fadeIn {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	@media (prefers-reduced-motion: reduce) {
		.chat-island {
			transition: none;
		}
	}
</style>

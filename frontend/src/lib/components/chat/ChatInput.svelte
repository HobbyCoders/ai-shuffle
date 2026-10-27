<script lang="ts">
	import type { TerminalToggle } from './composer-shell';
	/**
	 * ChatInput - the split-chips composer (mockup: docs/design/composer-split-chips.mockup.html).
	 *
	 * Two stacked parts, top to bottom:
	 *   1. ContextPillBar — the loose row of context chips (agent / runner /
	 *      repository / branch / worktree) floating ABOVE the box with a gap.
	 *      A new session has none: the hero's "Runs on …" sentence stands in
	 *      for it until the first message (`newSession`).
	 *   2. The box — a flat hairline rectangle. Its first row is the attach
	 *      circle, the Tiptap editor (with the @/slash autocomplete panels) and
	 *      the circles on the right: dictate, then one slot that shows Talk
	 *      while there is nothing to send and Send once there is (a spinner
	 *      while a worktree is created), then Stop while work runs. Below it is the
	 *      ControlsDock's bare-text controls row. Once the text wraps, the text
	 *      takes the full width and attach and the circles move to the two ends
	 *      of the controls row (composer-wrap.ts).
	 *
	 * Delegates voice recording to VoiceRecorder, calls to VoiceCallButton,
	 * controls to ControlsDock, and drag-and-drop to the inline overlay.
	 */
	import { tick, onDestroy, onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { activeSession } from '$lib/stores/active-session';
	import { pendingHandoff, handoffPlaceholder } from './pending-handoff';
	import { handoffPending } from '$lib/stores/handoff-pending';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { composerSuggestion, suggestionKey, hasComposerDraft } from '$lib/stores/session/composer-suggestion';
	import { persistVoiceContext, voiceContextFor } from '$lib/services/voiceExecutionContext';
	import { attachComposerDictation, detachComposerDictation, startComposerDictation } from '$lib/services/composerDictation';
	import { readDraft, writeDraft } from '$lib/stores/session/draft-persistence';
	import { promptStash } from '$lib/stores/session/prompt-stash';
	import { promptHistory, createPromptRecall } from '$lib/stores/session/prompt-recall';
	import { pastedTextFile } from './tiptap/large-paste';
	import { restoreMessageDocument } from './tiptap/serialization';
	import { composerInsert, completeComposerInsert } from '$lib/stores/composer-insert';
	import PromptStashMenu from './PromptStashMenu.svelte';
	import { sessionOwnerEpoch } from '$lib/stores/session-owner';
	import { getInitGeneration } from '$lib/stores/session/session-init';
	import { appData, profiles, sessions } from '$lib/stores/app-data.svelte';
	import { hasStoppableWork } from './stoppable-work';
	import { agentGate, anyCredential } from '$lib/stores/auth.svelte';
	import { git } from '$lib/stores/git.svelte';

	import { api, getErrorMessage, type FileUploadResponse } from '$lib/api/client';
	import { getCacheState, type CacheState } from '$lib/api/limit-resume';
	import LimitResumeBanner from './LimitResumeBanner.svelte';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { acknowledge, acknowledgedTokens, coldSendText, shouldWarn } from './cold-send';
	import { notify } from '$lib/stores/notifications';
	import { evaluateSubmitGuards, gateBlocksComposer, projectRulesLane, sendButtonState, NO_CREDENTIAL_REASON } from './submit-guards';
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
	import { ArrowUp, Plus } from 'lucide-svelte';
	import { composerCircles, createWrapWatcher } from './composer-wrap';
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
		terminal?: TerminalToggle;
		/**
		 * The new-session screen: the hero above names the agent and says
		 * where the session runs ("Runs on … in … on … in …"), so the chip row
		 * above the box would repeat it and steps aside. The placeholder
		 * addresses the agent by name.
		 */
		newSession?: boolean;
	}

	let { compact = false, onBrowserToggle, onBrowserPreload, browserOpen = false, terminal, newSession = false }: Props = $props();
	// ChatView remounts the editor when its account changes. Teardown and
	// already queued callbacks must not write the previous account's text.
	const ownerEpoch = sessionOwnerEpoch();
	const ownsComposer = () => ownerEpoch === sessionOwnerEpoch();

	// The lane this composer belongs to. `$activeSession.mode` — not the
	// sidepanel's `appMode` — is the authority: it is the session's own mode
	// once one is open, and only falls back to the sidepanel's pick on a fresh
	// compose screen.
	const composerMode = $derived($activeSession.mode);
	// Which lane's project rules apply.
	const projectLane = $derived(projectRulesLane(composerMode));
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
	// A queued move (#1819) holds new messages until it runs: say so.
	const moveWait = $derived.by(() => {
		const row = $sessions.find((session) => session.id === $activeSession.sessionId);
		return handoffPlaceholder(pendingHandoff($activeSession.messages, row ? { deviceId: row.owner_device_id, deviceName: row.device_name } : null, $activeSession.sessionId, $activeSession.sessionId ? $handoffPending[$activeSession.sessionId] : null));
	});
	// The new-session screen addresses the agent by name.
	const agentName = $derived($profiles.find(profile => profile.id === $activeSession.profile)?.name?.trim() || null);
	const composerPlaceholder = $derived(
		moveWait ??
			(newSession && agentName ? `What should ${agentName} do?`
				: composerMode !== 'chat' ? 'Describe a task or ask a question' : 'Ask anything')
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

	// Before a send into a large chat whose provider cache went cold: say what
	// it costs and offer to compact first (#1831). The check is bounded so a
	// slow answer never holds a send.
	let coldNotice = $state<{ sessionId: string; tokens: number; text: string } | null>(null);
	let coldCheckWaived = false;
	let compacting = $state(false);
	const COLD_CHECK_MS = 1500;
	$effect(() => {
		if (coldNotice && coldNotice.sessionId !== $activeSession.sessionId) coldNotice = null;
	});

	async function coldSendBlocks(sessionId: string): Promise<boolean> {
		if (coldCheckWaived) {
			coldCheckWaived = false;
			return false;
		}
		let state: CacheState | null = null;
		let timer: ReturnType<typeof setTimeout> | undefined;
		try {
			state = await Promise.race([
				getCacheState(sessionId),
				new Promise<null>((resolve) => { timer = setTimeout(() => resolve(null), COLD_CHECK_MS); }),
			]);
		} catch {
			return false;
		} finally {
			clearTimeout(timer);
		}
		if (!state || !shouldWarn(state, acknowledgedTokens(sessionId))) return false;
		const text = coldSendText(state);
		if (!text || !ownsComposer() || get(activeSession).sessionId !== sessionId) return false;
		coldNotice = { sessionId, tokens: state.context_tokens ?? 0, text };
		return true;
	}

	function sendAnyway() {
		const notice = coldNotice;
		if (!notice || compacting) return;
		acknowledge(notice.sessionId, notice.tokens);
		coldNotice = null;
		coldCheckWaived = true;
		handleSubmit();
	}

	async function compactFirst() {
		const notice = coldNotice;
		if (!notice || compacting) return;
		compacting = true;
		try {
			// The server owns both turns, so closing the tab cannot lose the
			// follow-up. Reuse normal send clearing and failure restoration.
			await handleSubmit(true);
		} finally {
			compacting = false;
		}
	}
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
	const availableSendState = $derived(sendButtonState({
		hasContent: hasMessageContent,
		hasCredential: $anyCredential,
		isUploading,
		isRecording,
		isTranscribing,
		autoBranch: $activeSession.worktreeIntent === 'worktree' && !$activeSession.worktreeId
	}));
	const sendState = $derived(compacting
		? { ...availableSendState, disabled: true, title: 'Starting compaction…' }
		: availableSendState);
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
		const paths = $activeSession.pendingRestoreUploadPaths ?? [];
		if (!pending && paths.length === 0) return;
		const current = (editorRef?.getMessage() ?? inputValue ?? '').trim();
		const restored = JSON.stringify(restoreMessageDocument(pending ?? '', paths));
		setComposerDraft(current.length === 0 ? restored : joinDrafts(restored, getDraftJSON()));
		recall.reset();
		tick().then(() => editorRef?.focus());
		activeSession.clearPendingRestorePrompt();
	});

	// Text handed over from another surface (the terminal's "Send selection to
	// chat") goes at the end of this chat's draft. Never sent: the user adds a
	// question and sends it. On a phone the terminal covers the chat, so focus
	// stays where it is instead of raising the keyboard behind it.
	$effect(() => {
		const insert = $composerInsert;
		// Wait for the editor: a request taken before it exists would be lost.
		if (!insert || !editorRef || !ownsComposer() || insert.sessionId !== $activeSession.sessionId) return;
		const block = JSON.stringify(restoreMessageDocument(insert.text));
		const current = getDraftJSON();
		setComposerDraft(hasComposerDraft(current) ? joinDrafts(current, block) : block);
		recall.reset();
		completeComposerInsert(insert);
		if (!isMobileComposerViewport()) tick().then(() => editorRef?.focus());
	});

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
		const pushed = promptStash.push(draft);
		if (!pushed) return;
		// Only clear the composer once the browser has kept the stash; otherwise
		// the composer is the only copy and a reload would lose it.
		if (!pushed.durable) {
			notifyStashFailed();
			return;
		}
		setComposerDraft('');
		recall.reset();
		composerSuggestion.invalidate();
		offerUndo('Prompt stashed', () => restoreStashed(pushed.entry.id));
	}

	/** A live banner where the success banner would be, so the refusal is seen at once. */
	function stashBannerError(message: string) {
		notify.banner(message, { severity: 'error', persist: false, duration_ms: 8000 });
	}

	function notifyStashFailed() {
		stashBannerError("Couldn't stash. Your draft is still here.");
	}

	/**
	 * Put a stashed prompt in the composer. Whatever the composer holds takes
	 * its place in the stash, and nothing changes if that save fails.
	 */
	function restoreStashed(id: string) {
		const entry = promptStash.find(id);
		if (!entry) return;
		const current = getDraftJSON();
		const swapped = hasComposerDraft(current);
		if (swapped) {
			// One write that swaps the entry for the draft, so a full stash
			// evicts nothing and a failed save changes nothing.
			if (!promptStash.swap(id, current)?.durable) {
				notifyStashFailed();
				return;
			}
		} else {
			// The entry is in hand; if its removal isn't saved it simply stays listed.
			promptStash.take(id);
		}
		setComposerDraft(entry.draft);
		recall.reset();
		stashOpen = false;
		tick().then(() => editorRef?.getEditor()?.commands.focus('end'));
		if (swapped) notify('Your previous draft was stashed.', { severity: 'info' });
	}

	function deleteStashed(id: string) {
		const entry = promptStash.find(id);
		if (!entry) return;
		if (!promptStash.take(id)) {
			stashBannerError("Couldn't delete the stashed prompt.");
			return;
		}
		offerUndo('Stashed prompt deleted', () => {
			if (!promptStash.putBack(entry)) stashBannerError("Couldn't put the stashed prompt back.");
		});
	}

	// ── Large paste → text attachment ─────────────────────────────────────────
	/** Take a large paste as a .txt attachment; false keeps it inline. */
	function handleLargePaste(text: string): boolean {
		if (!attachSupported || !$anyCredential || isUploading) return false;
		if (attachmentBlockReason($activeSession.project, $activeSession.projectV2Id, projectLane, $activeSession.sessionId)) return false;
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
	// The attach circle left of the editor. Why it cannot attach right now, if
	// it cannot: shown as its tooltip on desktop. On the phone a blocked attach
	// stays tappable so the tap can say why (triggerFileUpload).
	const attachBlock = $derived(attachmentBlockReason($activeSession.project, $activeSession.projectV2Id, projectLane, $activeSession.sessionId));
	const attachDisabled = $derived(!$anyCredential || isUploading || (!platform.isMobile && !!attachBlock));
	const attachTitle = $derived(!$anyCredential ? NO_CREDENTIAL_REASON : isUploading ? 'Uploading…' : (attachBlock ?? 'Attach files'));

	// Talk turns into Send: the slot right of dictate shows the Talk circle
	// while there is nothing to send and a call can start here, and Send once
	// there is. Stop follows it while work runs (composer-wrap.ts).
	let callAvailable = $state(false);
	const circles = $derived(composerCircles({
		callAvailable,
		hasContent: hasMessageContent,
		working: stoppableWork,
		busy: $activeSession.isCreatingWorktree || heldForCatalog
	}));
	const showTalk = $derived(circles.includes('talk'));

	// Wrap: while the text fits on one line, attach and the circles sit inline
	// with it; once it needs a second line they move to the controls row's two
	// ends and the text takes the full width (composer-wrap.ts).
	let composerWrapped = $state(false);
	let sendClusterRef = $state<HTMLDivElement | null>(null);
	const wrapWatcher = createWrapWatcher({
		getText: () => editorRef?.getText() ?? '',
		getEditor: () => (editorRef?.getEditor()?.view.dom as HTMLElement | undefined) ?? null,
		getIsland: () => islandRef,
		getCircles: () => sendClusterRef,
		isWrapped: () => composerWrapped,
		setWrapped: (wrapped) => { composerWrapped = wrapped; }
	});
	// Re-measure after the text changes, and after the circles do (Stop
	// arriving or leaving changes the room the text has on its line).
	$effect(() => {
		void inputValue;
		void circles;
		wrapWatcher.schedule();
	});
	$effect(() => {
		const island = islandRef;
		if (!island) return;
		return wrapWatcher.observe(island, sendClusterRef);
	});
	onDestroy(() => wrapWatcher.destroy());

	// Voice recording state (bindable from VoiceRecorder) is declared with
	// isUploading above, since the send button's state reads all three.

	// Error display — shared between file upload errors and voice recording errors
	let errorMessage = $state('');

	// Wrong-project nudge: a new chat's draft names another registered project
	// (and not the selected one). Offer a one-click switch before sending.
	let dismissedMismatch = $state<string | null>(null);
	// Without a project the nudge offers to add the one the draft names.
	const projectMismatch = $derived(
		!$activeSession.sessionId && plainTextValue.length >= 8
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
	async function handleSubmit(compactBeforeSend = false) {
		if (!ownsComposer() || heldForCatalog || (compacting && !compactBeforeSend)) return;
		if (projectLane !== 'chat' && !catalogHoldWaived && activeSession.composerAwaitingProjectCatalog) {
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
			// An opened chat whose agent is still loading sends without one; the
			// backend keeps the chat's own agent (#418).
			hasProfile: !!$activeSession.profile || (!!$activeSession.sessionId && !!$activeSession.bindingUnhydrated?.profile),
			hasAnyProfile: $profiles.length > 0,
			hasProject: !!$activeSession.project || !!$activeSession.projectV2Id,
			// The project guard is a code-lane requirement; the projectless lanes
			// would otherwise refuse every send.
			mode: projectLane
		});
		if (block) {
			if (block.notice) notify(block.notice.message, { severity: block.notice.tone });
			return;
		}

		// A large chat with a cold provider cache: ask first (#1831).
		const sendingTo = $activeSession.sessionId;
		if (!compactBeforeSend && sendingTo && !$activeSession.isStreaming && (await coldSendBlocks(sendingTo))) return;

		// Append any legacy uploaded files (from file upload button) to the end
		// Note: Files added via @ autocomplete are now inline chips, not in uploadedFiles
		let messageToSend = finalPrompt;
		if (uploadedFiles.length > 0) {
			const fileRefs = uploadedFiles.map(f => `@${f.path}`).join(' ');
			messageToSend = [finalPrompt.trim(), fileRefs].filter(Boolean).join('\n\n');
		}

		// Send the message, optionally queued behind a server-owned compaction.
		await sendMessage(messageToSend, compactBeforeSend);
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
	async function sendMessage(finalPrompt: string, compactBeforeSend = false) {
		if (!ownsComposer()) return;
		// Enforce the same capability as the hidden worktree checkbox, even if
		// remembered intent changes before the context bar's effect has flushed.
		const repository = git.current;
		if (!$activeSession.sessionId && repository.hydrated
			&& (repository.projectId === $activeSession.projectV2Id || repository.projectId === $activeSession.project)
			&& repository.sessionId === null && repository.worktreeId === $activeSession.worktreeId
			&& (repository.status?.is_git_repo === false || !!repository.status?.enclosing_repository_root)) {
			activeSession.setWorktreeIntent('local', { persist: false });
		}
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
				compactBeforeSend || pendingPaths?.length
					? { pendingUploadPaths: pendingPaths, ...(compactBeforeSend ? { compactFirst: true } : {}) }
					: undefined,
			);
			if (sent === false) restoreOnFailure();
			else if (ownsComposer() && !mobileComposerDestroyed && getInitGeneration() === sendingNavigation) {
				if (compactBeforeSend) coldNotice = null;
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
		if (projectLane !== 'chat' && !$activeSession.project && $activeSession.sessionId && $activeSession.projectV2Id) {
			return api.uploadFile(`/sessions/${encodeURIComponent($activeSession.sessionId)}/upload`, file);
		}
		if (projectLane === 'chat') {
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
			projectLane,
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
		if (attachmentBlockReason($activeSession.project, $activeSession.projectV2Id, projectLane, $activeSession.sessionId)) return;

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
			projectLane,
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
	class:has-draft={hasMessageContent}
	class:dictating={isRecording || isTranscribing}
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
				message={$activeSession.project
					? `This looks like it's about ${projectMismatch.name}. Start it there instead?`
					: `This looks like it's about ${projectMismatch.name}. Add it as this session's project?`}
				action={{ label: $activeSession.project ? `Switch to ${projectMismatch.name}` : `Add ${projectMismatch.name} as project`, onClick: () => activeSession.setProject(projectMismatch.id) }}
				dismissible
				onDismiss={() => (dismissedMismatch = projectMismatch?.id ?? null)}
			/>
		{/if}

		<!-- Error Message (upload errors, voice recording errors) -->
		{#if errorMessage}
			<Banner severity="error" message={errorMessage} dismissible onDismiss={() => (errorMessage = '')} />
		{/if}

		<!-- Agents cannot start here yet (no license, or no provider ready):
		     say what is missing instead of failing later. -->
		{#if gateBlocksComposer($agentGate, $anyCredential)}
			<Banner severity="warning" message={$agentGate?.reason ?? ''} />
		{/if}

		<!-- Worktree Error Message -->
		{#if $activeSession.worktreeError}
			<Banner severity="warning" message={$activeSession.worktreeError} dismissible onDismiss={() => activeSession.clearWorktreeError()} />
		{/if}

		<!-- Usage limit: when it resets, "Resume at reset" once, and Cancel once armed (#1831). -->
		<LimitResumeBanner
			sessionId={$activeSession.sessionId}
			messages={$activeSession.messages}
			streaming={$activeSession.isStreaming}
			canAct={$isAdmin}
		/>

		{#if coldNotice && coldNotice.sessionId === $activeSession.sessionId}
			<Banner
				severity="warning"
				message={compacting ? 'Starting compaction. Queuing your message to send next…' : coldNotice.text}
				action={compacting ? undefined : { label: 'Compact first', onClick: compactFirst }}
				secondaryAction={compacting ? undefined : { label: 'Send anyway', onClick: sendAnyway }}
				dismissible={!compacting}
				onDismiss={() => (coldNotice = null)}
			/>
		{/if}

		<form
			use:composerOpenGesture
			onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}
			onpointerdown={handleComposerPointerDown}
		>
			<!-- Context chips: agent · runner · repository · branch · worktree.
			     A loose row floating ABOVE the box with a visible gap once a
			     session is under way (ContextPillBar decides which chips are
			     selectors and which are read-only).

			     The phone shell carries these selectors in its controls panel; a
			     second, hidden copy would share their dropdown state. A new
			     session shows none: the hero's "Runs on …" sentence above the
			     box says the same, and opens the same menus. -->
			{#if !mobileViewport && !newSession}
				<div class="context-pill-row"><ContextPillBar /></div>
			{/if}

			<!-- The box: a flat hairline rectangle holding the attach circle, the
			     editor and the circles on the right, then the bare-text controls
			     row. `wrapped`: the text needs a second line, so it takes the full
			     width and attach and the circles sit at the controls row's ends.
			     The move is layout only (CSS grid), never a remount: a dictation
			     or a starting call in those circles carries straight on. -->
			<div class="chat-island" class:wrapped={composerWrapped} bind:this={islandRef}>
				<!-- The editor. The whole area forwards pointer events to the Tiptap
				     editor so the padding around the text line is clickable too — an
				     empty composer reads as one target. The autocomplete panels
				     (positioned upward OUTSIDE this element) are unaffected: they are
				     never inside this element's event path. -->
				<!-- svelte-ignore a11y_click_events_have_key_events -->
				<!-- svelte-ignore a11y_no_static_element_interactions -->
				<div class="input-area" onclick={handleEditorAreaClick}>
					<!-- Attach: a circle left of the text (bottom-left once it wraps).
					     A code-lane upload lands in the project's or worktree's
					     uploads dir; a chat-lane one in the workspace's shared
					     chat-uploads dir. -->
					<button type="button" class="composer-attach" onclick={triggerFileUpload} disabled={attachDisabled}
						aria-label={isUploading ? 'Uploading' : 'Attach files'} title={attachTitle}>
						{#if isUploading}
							<svg class="composer-attach-spin" width="18" height="18" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" stroke-width="2" stroke-dasharray="40" stroke-dashoffset="12" /></svg>
						{:else}
							<Plus size={18} strokeWidth={2} aria-hidden="true" />
						{/if}
					</button>
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

					<!-- The circles: dictate, then the send slot, then Stop. Talk holds
					     the slot while there is nothing to send; Send takes it once
					     there is. Stop must remain reachable whenever session work is
					     live, including with a draft; Send can queue that draft
					     independently. StopButton confirms first when background
					     agents are live. -->
					<div class="send-cluster" bind:this={sendClusterRef}>
						{@render recorder()}
						<PromptStashMenu
							entries={$promptStash}
							open={stashOpen}
							onOpenChange={(open) => { stashOpen = open; }}
							onRestore={restoreStashed}
							onDelete={deleteStashed}
						/>
						{#if circles.includes('busy')}
							<!-- Creating worktree, or a send held for the project catalog - show spinner -->
							{#if heldForCatalog}<span class="sr-only" role="status">Loading projects…</span>{/if}
							<button type="button" class="send-btn creating-worktree" disabled title={heldForCatalog ? 'Loading projects…' : 'Creating worktree...'} aria-label={heldForCatalog ? 'Loading projects…' : 'Creating worktree'}>
								<svg class="w-3.5 h-3.5 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
									<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="31.4" stroke-dashoffset="10" />
								</svg>
							</button>
						{:else if circles.includes('send')}
							<button
								type="submit"
								class="send-btn"
								disabled={sendState.disabled}
								title={stoppableWork && !sendState.disabled ? 'Send — Enter' : sendState.title}
								aria-label="Send"
							>
								<ArrowUp size={16} strokeWidth={2.4} aria-hidden="true" />
							</button>
						{/if}
						<!-- Always mounted (hidden when it does not hold the slot), so
						     voice availability is read once and a starting call is
						     never torn down by a keystroke. -->
						<span class="talk-slot" hidden={!showTalk}>{@render talk()}</span>
						{#if circles.includes('stop')}
							<StopButton
								activeTasks={$activeSession.activeTasks}
								onStop={() => { composerSuggestion.invalidate(); activeSession.stopGeneration(); }}
							/>
						{/if}
					</div>
				</div>

				<!-- Controls row. ControlsDock owns access mode, the browser and
				     terminal toggles, model, effort and the context ring. The phone
				     shell's controls panel also offers a call. -->
				<ControlsDock
					bind:this={controlsDockRef}
					{onBrowserToggle}
					{onBrowserPreload}
					{browserOpen}
					{terminal}
					inertToolbar={toolbarPointerBlocked}
					callAction={mobileViewport && !newSession ? caller : undefined}
					onSheetOpenChange={(open) => {
						mobileControlsOpen = open;
						if (open) keepMobileComposerExpanded();
						else scheduleMobileComposerReconciliation();
					}}
				/>
			</div>
		</form>
	</div>
</div>

<!-- The dictate mic, the Talk circle in the send slot, and the call button
     the phone shell's controls panel shows. -->
{#snippet recorder()}
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
{/snippet}

{#snippet talk()}
	<!-- Talk (VM19): starts a call on this conversation. On a fresh
	     composer it creates the conversation from the composer's picks
	     first (#1616) and reads "Talk to <agent>". It shows disabled with
	     the reason while it cannot start. -->
	<VoiceCallButton
		variant="circle"
		agentName={newSession ? agentName : null}
		bind:available={callAvailable}
		disabled={isUploading || isRecording || isTranscribing}
		disabledReason={isUploading ? 'Uploading files...' : isRecording ? 'Recording...' : isTranscribing ? 'Transcribing...' : null}
	/>
{/snippet}

{#snippet caller()}
	<VoiceCallButton
		disabled={isUploading || isRecording || isTranscribing}
		disabledReason={isUploading ? 'Uploading files...' : isRecording ? 'Recording...' : isTranscribing ? 'Transcribing...' : null}
	/>
{/snippet}

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
	   line up with the circles. Don't stretch ProseMirror itself — that
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

	/* ===== DESKTOP LAYOUT: one grid, two arrangements =====
	   Single line:   [+] [ text .............. ] [mic][talk|send][stop]
	                  [ access · browser · terminal      model · effort · ring ]
	   Wrapped:       [ text ............................................... ]
	                  [+] [ access · browser · terminal   model · effort · ring ] [circles]
	   The editor row's box steps aside (display: contents) so attach, the
	   text and the circles are grid items of the box itself and can share
	   the controls row once the text wraps. Moving them is layout only. */
	:global(html[data-platform='desktop']) .chat-island {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
	}
	:global(html[data-platform='desktop']) .input-area {
		display: contents;
	}
	:global(html[data-platform='desktop']) .composer-attach {
		grid-row: 1;
		grid-column: 1;
		margin: 12px 0 4px 12px;
	}
	:global(html[data-platform='desktop']) .textarea-wrapper {
		grid-row: 1;
		grid-column: 2;
		z-index: 20;
		padding: 12px 10px 4px;
	}
	:global(html[data-platform='desktop']) .send-cluster {
		grid-row: 1;
		grid-column: 3;
		position: relative;
		z-index: 20;
		margin: 12px 12px 4px 0;
	}
	:global(html[data-platform='desktop']) .chat-island > :global(.controls-dock) {
		grid-row: 2;
		grid-column: 1 / -1;
		align-self: stretch;
	}
	/* Wrapped: the text takes the whole first row; attach and the circles sit
	   at the controls row's two ends, centred on it. */
	:global(html[data-platform='desktop']) .chat-island.wrapped .textarea-wrapper {
		grid-column: 1 / -1;
		padding: 14px 16px 4px;
	}
	:global(html[data-platform='desktop']) .chat-island.wrapped .composer-attach {
		grid-row: 2;
		margin: 4px 0 8px 10px;
	}
	:global(html[data-platform='desktop']) .chat-island.wrapped .send-cluster {
		grid-row: 2;
		margin: 4px 10px 8px 0;
	}
	:global(html[data-platform='desktop']) .chat-island.wrapped > :global(.controls-dock) {
		grid-column: 2;
		padding: 4px 8px 8px;
	}

	/* ===== THE CIRCLES =====
	   Attach (left), then dictate · Talk or Send · Stop (right): 32px rounds
	   in either layout. Attach and dictate are quiet
	   (muted fill); Talk keeps the call button's accent tint; Send is the
	   filled accent; Stop keeps its destructive tint. */
	.composer-attach {
		display: grid;
		place-items: center;
		flex: none;
		width: 32px;
		height: 32px;
		padding: 0;
		border: 0;
		border-radius: 50%;
		background: var(--muted);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background-color 0.15s ease, color 0.15s ease, transform 0.1s ease;
	}
	.composer-attach:hover:not(:disabled) {
		background: color-mix(in oklch, var(--muted) 80%, var(--foreground));
		color: var(--foreground);
	}
	.composer-attach:active:not(:disabled) { transform: scale(0.94); }
	.composer-attach:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.composer-attach:disabled { opacity: 0.45; cursor: not-allowed; }
	.composer-attach-spin { animation: composer-attach-spin 0.9s linear infinite; }
	@keyframes composer-attach-spin { to { transform: rotate(360deg); } }

	/* ===== SEND CLUSTER =====
	   Right of the text (or at the controls row's right end once it wraps). */
	.send-cluster {
		display: flex;
		align-items: center;
		align-self: center;
		gap: 6px;
		flex-shrink: 0;
	}

	/* Only one of Talk and Send shows; the hidden one stays mounted. */
	.talk-slot { display: contents; }
	.talk-slot[hidden] { display: none; }

	/* Dictate: a quiet circle at rest. Listening, preparing and transcribing
	   keep VoiceRecorder's own widths and tints. */
	.input-area .send-cluster :global(.voice-btn) {
		height: 32px;
	}
	.send-cluster :global(.voice-btn-wrapper:not(.expanded)) {
		border-radius: 50%;
	}
	.send-cluster :global(.voice-btn:not(.expanded):not(.preparing):not(.transcribing)) {
		width: 32px;
		border-radius: 50%;
	}
	.send-cluster :global(.voice-btn:not(.recording):not(.preparing):not(.transcribing)) {
		background: var(--muted);
		color: var(--muted-foreground);
	}
	.send-cluster :global(.voice-btn:not(.recording):not(.preparing):not(.transcribing):hover:not(:disabled)) {
		background: color-mix(in oklch, var(--muted) 80%, var(--foreground));
		color: var(--foreground);
	}
	.send-cluster :global(.voice-btn:focus-visible) {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.send-btn {
		display: grid;
		place-items: center;
		flex: none;
		width: 32px;
		height: 32px;
		padding: 0;
		background: var(--primary);
		border: none;
		border-radius: 50%;
		color: var(--primary-foreground);
		cursor: pointer;
		transition: background-color 0.15s ease, color 0.15s ease, filter 0.12s ease, transform 0.1s ease;
	}

	.send-btn:hover:not(:disabled) {
		filter: brightness(1.08);
	}

	.send-btn:active:not(:disabled) {
		filter: brightness(0.96);
		transform: scale(0.94);
	}

	.send-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Nothing to send yet: a quiet circle. */
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
		width: 32px;
		height: 32px;
		border-radius: 50%;
	}

	/* The circles keep their 32px in the controls row too: the wrap
	   measurement (composer-wrap.ts) compares their width across layouts. */

	@keyframes fadeIn {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	@media (prefers-reduced-motion: reduce) {
		.chat-island,
		.composer-attach,
		.send-btn {
			transition: none;
		}
		.composer-attach-spin { animation: none; }
	}
</style>

<script lang="ts">
	import type { TerminalToggle } from '$lib/components/chat/composer-shell';
	/**
	/** Chat surface for code, chat and voice. Lane-specific heroes share one
	 * persistent editor. A live voice call adds compact controls alongside it. */
	import { onDestroy, tick, untrack } from 'svelte';
	import { finishJourneyAfterPaint } from '$lib/services/journey-timing';
	import { activeSession, activeWsConnected, streamingSessions } from '$lib/stores/active-session';
	import { sessions } from '$lib/stores/app-data.svelte';
	import { openSession } from '$lib/services/session-navigation';
	import { interruptSession, stopSessionTask } from '$lib/api/sessions';
	import { buildBackgroundJobs, type BackgroundJob } from '$lib/utils/background-work';
	import BackgroundWorkStrip from '$lib/components/chat/BackgroundWorkStrip.svelte';
	import { isAssistantConfirm } from '$lib/utils/assistant-confirm';
	import { sessionOwnerBoundary } from '$lib/stores/session-owner';
	import { notify } from '$lib/stores/notifications';
	import { platform } from '$lib/stores/platform.svelte';
	import { createConnectionBannerGrace } from '$lib/stores/session/connection-banner-grace';
	import Banner from '$lib/components/Banner.svelte';
	import NotificationSlot from '$lib/components/NotificationSlot.svelte';
	import { DOCK_ORDER } from '$lib/stores/notification-dock.svelte';
	import { forkOfferedSessionHere } from '$lib/components/chat/fork-here';
	import MessageArea from '$lib/components/chat/MessageArea.svelte';
	import ChatInput from '$lib/components/chat/ChatInput.svelte';
	import ArchivedChatBar from '$lib/components/chat/ArchivedChatBar.svelte';
	import { archivedChats } from '$lib/stores/archived-chats';
	import { assistantState } from '$lib/stores/assistant';
	import PermissionQueue from '$lib/components/PermissionQueue.svelte';
	import QuestionTray from '$lib/components/chat/QuestionTray.svelte';
	import PrivateAccessPrompt from '$lib/components/chat/PrivateAccessPrompt.svelte';
	import WorkspaceSelector from './WorkspaceSelector.svelte';
	import ChatModeHero from './ChatModeHero.svelte';
	import ChatUnavailableNotice from './ChatUnavailableNotice.svelte';
	import { unavailableChat, clearUnavailableChat } from '$lib/stores/unavailable-chat';
	import VoiceCallPanel from '$lib/components/voice/VoiceCallPanel.svelte';
	import { voiceCallState, isCallSurfaceActive } from '$lib/services/voiceCall';
	import { RotateCcw } from 'lucide-svelte';
	import { classifyChatSurface } from './chat-empty-state';
	import { pendingDeepLink, deepLinkLoading } from '$lib/stores/pending-deep-link';
	// The phone shell docks prompts above its composer; the web build has none.
	import { composerShell } from '$lib/components/chat/composer-shell';

	interface Props {
		/** The open chat is the assistant's home (decided once, in MainPanel). */
		assistantMode?: boolean;
		onBrowserToggle?: () => void;
		onBrowserPreload?: () => void;
		browserOpen?: boolean;
		terminal?: TerminalToggle;
	}

	let { assistantMode = false, onBrowserToggle, onBrowserPreload, browserOpen = false, terminal }: Props = $props();

	// Classify the empty-message surface. While a session's history is being
	// fetched (boot restore OR an openChat navigation) messages is still [] but
	// sessionId already points at the target — showing an empty hero then would
	// invite sends into an invisible session AND could flash the cleared hint for
	// a session that turns out to have messages, so the load window renders the
	// message column (non-empty), not a hero. See chat-empty-state.ts + #558.
	const chatSurface = $derived(
		classifyChatSurface({
			sessionId: $activeSession.sessionId,
			// Pending peers belong in the transcript, even before its first message.
			messageCount: $activeSession.messages.length + ($activeSession.pendingPeerMessages?.length ?? 0),
			isRestoringSession: $activeSession.isRestoringSession,
			// A deep link boot has read but not opened yet is a load in progress,
			// not a New Chat: no hero flash before the linked chat appears.
			isLoadingSession: $activeSession.isLoadingSession || deepLinkLoading($pendingDeepLink, $activeSession.sessionId)
		})
	);
	// The chat.open sample ends after the DOM update once history has settled.
	// A primitive $derived keeps the effect from re-running (and restarting its
	// frames) on every activeSession tick, such as streaming text; a teardown
	// cancels the frames, so a view that moved on never completes the old sample.
	const settledChatId = $derived.by(() => {
		const id = $activeSession.sessionId;
		const loading = $activeSession.isLoadingSession || $activeSession.isRestoringSession;
		return id && !loading && !deepLinkLoading($pendingDeepLink, id) ? id : null;
	});
	$effect(() => {
		if (settledChatId) return finishJourneyAfterPaint('chat.open', settledChatId);
	});
	const isEmptyState = $derived(chatSurface === 'new' || chatSurface === 'cleared');
	// A BOUND, settled, empty session — a just-cleared conversation. Presents the
	// hero with the session's own title + a "cleared" hint instead of the
	// brand-new "Let's build your project" workspace picker.
	const isClearedSession = $derived(chatSurface === 'cleared');
	const primaryAssistant = $derived(assistantMode ? $assistantState : null);
	// A chat that could not be opened, while this surface is still the unbound
	// New Chat it fell back to. Binding any chat hides it.
	const unavailable = $derived($activeSession.sessionId ? null : $unavailableChat);

	// The assistant's background work, as plain lines beside its composer.
	const backgroundJobs = $derived(
		assistantMode
			? buildBackgroundJobs({
				tasks: $activeSession.activeTasks ?? [],
				messages: $activeSession.messages,
				sessions: $sessions,
				streaming: $streamingSessions
			})
			: []
	);
	function openJob(job: BackgroundJob) {
		if (job.sessionId) openSession(job.sessionId);
	}
	async function stopJob(job: BackgroundJob): Promise<boolean> {
		try {
			if (job.stop.kind === 'task') {
				const home = $activeSession.sessionId;
				if (!home) return false;
				const result = await stopSessionTask(home, job.stop.taskId);
				if (!result.ok) notify.error('Could not stop that job. It may have finished.');
				return result.ok;
			}
			await interruptSession(job.stop.sessionId);
			return true;
		} catch {
			notify.error('Could not stop that job. It may have finished.');
			return false;
		}
	}
	const clearedTitle = $derived($activeSession.title?.trim() || 'Untitled');

	// The lane this surface belongs to. The store carries it for BOTH an opened
	// session (adopted from the row) and a brand-new compose screen (adopted from
	// the sidepanel switcher), so this one read covers every case — no separate
	// "what did the switcher say" lookup that could disagree with the session.
	const surfaceMode = $derived($activeSession.mode);

	// Voice (ruling VM18). The gate is the CALL, not the session's mode: a voice
	// session with no call running is an ordinary chat, and only an in-flight call
	// on the session currently being viewed takes over the composer slot. The
	// singleton controller means the sessionId comparison is what stops a call on
	// one session from swallowing another session's composer.
	const voiceCallActive = $derived(
		isCallSurfaceActive($voiceCallState, $activeSession.sessionId)
	);

	// A live call also suppresses the new-chat hero: a freshly created voice
	// session has no messages yet, and the workspace picker has nothing to offer
	// mid-call. The transcript column takes over and fills in as the exchange
	// lands.
	const showEmptyHero = $derived(isEmptyState && !voiceCallActive);

	// Keep the editor mounted through call transitions so drafts and file chips survive.
	const showComposer = true;
	// An archived chat is a read-only transcript until restored (#1822): the
	// composer slot shows export and restore instead of the editor.
	const archivedSessionId = $derived(
		$activeSession.sessionId && $archivedChats.has($activeSession.sessionId) ? $activeSession.sessionId : null
	);
	let surfaceHeight = $state(0);
	let composerHeight = $state(0);
	$effect(() => {
		if (showEmptyHero && surfaceHeight > 0 && composerHeight > surfaceHeight && platform.isMobile) {
			void tick().then(() => {
				const focused = document.activeElement;
				if (focused instanceof HTMLElement && focused.closest('.empty-input-layout')) {
					focused.scrollIntoView({ block: 'nearest' });
				}
			});
		}
	});

	// VM23 (user directive, 2026-08-15): while a call is live, worker permission
	// requests are the VOICE agent's to relay — it reads them out and answers via
	// respond_approval on the user's spoken yes/no. Showing the GUI modal at the
	// same time creates a race: the user clicks Allow, then says "go ahead", and
	// the agent's respond_approval finds nothing to answer. So the modal's
	// permission leg is suppressed for exactly the sessions/timespan the spoken
	// leg covers; it returns the moment the call ends. Questions are unaffected.
	// A request the assistant asks in words (`assistant_confirm`) never opens
	// the dialog: it reads as the assistant's question in the transcript and is
	// answered by typing or saying yes or no (MessageArea renders it). Every
	// other request keeps the dialog, in the home too.
	const dialogPermissions = $derived(($activeSession.pendingPermissions ?? []).filter((request) => !isAssistantConfirm(request)));
	const showPermissionModal = $derived(!voiceCallActive && dialogPermissions.length > 0);
	// A shell prompt dock answers permissions, private access and questions
	// above the composer, so it replaces the modal and the question tray.
	const PromptDock = composerShell().PromptDock;

	// A call that ends by itself (transport error, or the VW5 length cap) unmounts
	// the dock that was showing the reason, so surface it as a toast instead —
	// otherwise the call would just silently vanish and the composer reappear.
	// Keyed on the message so the same terminal state can't re-toast on unrelated
	// store churn (level updates, caption appends).
	let lastVoiceOutcome: string | null = null;
	$effect(() => {
		const call = $voiceCallState;
		const message =
			call.status === 'error' ? call.error : call.status === 'ended' ? call.notice : null;
		const key = message ? `${call.status}:${call.sessionId}:${message}` : null;
		if (key === lastVoiceOutcome) return;
		lastVoiceOutcome = key;
		if (!message) return;
		if (call.status === 'error') notify.error(message);
		else notify.warning(message);
	});

	// Animation: Track transition from empty → active state
	let transitioningFromEmpty = $state(false);
	let previousIsEmpty = $state(true);

	// Depends only on `isEmptyState`; reading any other store here would re-run
	// the effect (clearing the timer via teardown) without resetting the flag.
	$effect(() => {
		const isEmpty = isEmptyState;
		const wasEmpty = untrack(() => previousIsEmpty);
		if (wasEmpty === isEmpty) return;
		previousIsEmpty = isEmpty;

		if (wasEmpty && !isEmpty) {
			// Empty → active transition: animate the reveal
			transitioningFromEmpty = true;
			const timer = setTimeout(() => {
				transitioningFromEmpty = false;
			}, 450);
			return () => {
				clearTimeout(timer);
				transitioningFromEmpty = false;
			};
		} else {
			// Active → empty transition: clear the animation class immediately.
			// Without this, the class gets stuck if the effect cleanup (clearTimeout)
			// runs before the timeout fires, leaving transitioningFromEmpty=true.
			transitioningFromEmpty = false;
		}
	});

	// Handle permission response — approve/deny only (defer was dropped from the UI).
	function handlePermissionResponse(detail: {
		request_id: string;
		decision: 'allow' | 'deny';
		remember?: 'none' | 'session' | 'profile';
		pattern?: string;
	}) {
		const { request_id, decision, remember, pattern } = detail;
		const sessionId =
			$activeSession.pendingPermissions.find(req => req.request_id === request_id)?.session_id ??
			$activeSession.sessionId ??
			undefined;
		activeSession.sendPermissionResponse(request_id, decision, remember, pattern, sessionId);
	}

	// Handle question response
	function handleQuestionResponse(detail: {
		request_id: string;
		tool_use_id: string;
		answers: Record<string, string | string[]>;
		dismissed?: boolean;
		skipped?: string[];
	}) {
		const { request_id, tool_use_id, answers, dismissed, skipped } = detail;
		const sessionId =
			$activeSession.pendingQuestions.find(q => q.request_id === request_id)?.session_id ??
			$activeSession.sessionId ??
			undefined;
		const sent = activeSession.sendUserQuestionResponse(request_id, tool_use_id, answers, sessionId, {
			dismissed,
			skipped
		});
		if (!sent) notify.error('Your answer was not sent. Reconnect and try again.');
	}

	// Handle project selection from WorkspaceSelector
	function handleProjectSelect(projectId: string, projectV2Id?: string | null) {
		activeSession.setProject(projectId, projectV2Id ?? null);
	}

	// Manually re-establish the WebSocket (the Reconnect button on the
	// disconnection banner). Replace a suspect socket and reset retry backoff.
	function handleReconnect() {
		activeSession.reconnect();
	}

	// Give the "connection lost" banner a 3s grace period so a transient WS
	// blip (e.g. switching tabs/panels and back) doesn't flash it. The gate
	// lives for the component's lifetime (not recreated per-effect-run, or
	// its in-flight timer/shown state would be lost on every dependency
	// change) — `setConnected` is called reactively below, and `destroy()`
	// runs once on actual unmount via onDestroy so no timer can leak or fire
	// after the component is gone.
	let showConnectionBanner = $state(false);
	const connectionBannerGrace = createConnectionBannerGrace({
		onShow: () => { showConnectionBanner = true; },
		onHide: () => { showConnectionBanner = false; }
	});
	onDestroy(() => connectionBannerGrace.destroy());

	$effect(() => {
		// Only track the WS state as "disconnected" while the banner is
		// actually eligible to show (an active session). Otherwise treat it as
		// connected so the grace timer never arms for a session that wouldn't
		// show the banner anyway.
		const eligible = Boolean($activeSession.sessionId);
		connectionBannerGrace.setConnected(!eligible || $activeWsConnected);
	});
</script>

<div class="chat-view" bind:clientHeight={surfaceHeight} style:--chat-surface-height={surfaceHeight ? `${surfaceHeight}px` : undefined} class:transitioning-from-empty={transitioningFromEmpty} class:is-empty={showEmptyHero}>
	{#if showEmptyHero}
		<!-- Empty state hero: workspace selector / cleared hint. The ChatInput is
		     NOT rendered here — it lives in the always-mounted .input-slot below so
		     its subtree (Tiptap draft, open context-usage popover, VoiceRecorder,
		     dropdowns) survives the empty → active flip instead of being destroyed
		     and remounted (#665). -->
		<div class="empty-hero" class:hero-only={!showComposer}>
			{#if $activeSession.error}
				<div class="empty-error-overlay">
					<Banner severity="error" message={$activeSession.error} dismissible onDismiss={() => activeSession.clearError()}
						action={$activeSession.forkOfferSessionId ? { label: 'Fork here', onClick: () => forkOfferedSessionHere($activeSession.forkOfferSessionId!) } : undefined} />
				</div>
			{/if}

			<div class="empty-hero-content">
				{#if primaryAssistant}
					<div class="cleared-hero">
						<h1 class="cleared-title">{primaryAssistant.introduction.preferred_name ? `Hello, ${primaryAssistant.introduction.preferred_name}.` : 'Where shall we begin?'}</h1>
						<p class="cleared-hint">Just say what you need.</p>
					</div>
				{:else if unavailable}
					<ChatUnavailableNotice chat={unavailable} onDismiss={clearUnavailableChat} />
				{:else if isClearedSession}
					<!-- Bound existing session with an empty transcript (cleared via the
					     sessions-board broom, #558). Show its OWN title + a cleared
					     hint so it never reads as a brand-new chat. The project is
					     fixed for an existing session, so no workspace picker here. -->
					<div class="cleared-hero">
						<div class="cleared-badge" aria-hidden="true">
							<RotateCcw size={22} strokeWidth={1.5} />
						</div>
						<h1 class="cleared-title" title={clearedTitle}>{clearedTitle}</h1>
						<p class="cleared-hint">Conversation cleared — send a message to start fresh.</p>
					</div>
				{:else if surfaceMode === 'chat' || surfaceMode === 'voice'}
					<!-- Projectless conversation lane — no workspace to pick. Voice is
					     a call on a conversation, so a voice surface gets the chat hero. -->
					<ChatModeHero />
				{:else}
					<WorkspaceSelector
						selectedProject={$activeSession.project}
						selectedProjectV2Id={$activeSession.projectV2Id}
						onSelectProject={handleProjectSelect}
					/>
				{/if}
			</div>
		</div>
	{:else}
		<!-- Active chat: message column. The input lives in the shared .input-slot
		     below, not here — see the #665 note above. -->
		<div class="chat-column">
			<div class="message-area-wrapper">
				{#if showConnectionBanner}
					<!-- Live connection lost. The client auto-reconnects with backoff,
					     but surface it so the user knows why messages may lag — and
					     give them a manual retry that skips the backoff wait. The
					     banner itself is gated behind a 3s grace period (see
					     connectionBannerGrace above) so transient blips don't flash it. -->
					<NotificationSlot order={DOCK_ORDER.connection}>
						<Banner
							severity="warning"
							variant="page"
							message={$activeSession.wsReconnectRequired
								? 'Chat controls disconnected. Reconnect to send messages.'
								: 'Chat controls disconnected. Trying to reconnect; running sessions continue.'}
							action={{ label: 'Reconnect', onClick: handleReconnect }}
						/>
					</NotificationSlot>
				{/if}
				<!-- No call surface here any more (VM18): the call controls live in the
				     composer slot below, so the transcript column looks the same during
				     a call as it does outside one. -->
				<!-- MessageArea handles session-switch scroll save/restore internally
				     via $effect watching sessionId. No {#key} needed — avoiding
				     component destruction prevents a Svelte 5 reactivity edge case
				     where the recreated component loses reactive updates during
				     new session creation (sessionId: undefined → new ID). -->
				<MessageArea {assistantMode} />
			</div>
		</div>
	{/if}

	<!-- Persistent input slot. Rendered ONCE across empty/active states so the
	     ChatInput subtree (draft, context-usage popover, VoiceRecorder, dropdowns)
	     is never torn down at the isEmptyState boundary (#665). Only the `compact`
	     prop and the surrounding layout classes change between states; the
	     conditional strips below are siblings of ChatInput, not ancestors, so they
	     never remount it. All three lanes keep this same editor. -->
	{#if showComposer}
		<div class="input-slot" bind:clientHeight={composerHeight} class:composer-overflow={surfaceHeight > 0 && composerHeight > surfaceHeight} class:input-wrapper={!showEmptyHero} class:empty-input-layout={showEmptyHero}>
			{#if voiceCallActive && $activeSession.sessionId}
				<VoiceCallPanel sessionId={$activeSession.sessionId} compact />
			{/if}
			{#if assistantMode}
				<BackgroundWorkStrip jobs={backgroundJobs} onOpen={openJob} onStop={stopJob} />
			{/if}
			{#if archivedSessionId}
				<ArchivedChatBar sessionId={archivedSessionId} />
			{:else}
				{#key $sessionOwnerBoundary}
					{#if PromptDock}
						<PromptDock
							sessionId={$activeSession.sessionId}
							permissions={showPermissionModal ? dialogPermissions : []}
							questions={$activeSession.pendingQuestions ?? []}
							working={$activeSession.isStreaming}
							connected={$activeWsConnected}
							onPermission={handlePermissionResponse}
							onQuestion={handleQuestionResponse}
						/>
					{:else}
						<PrivateAccessPrompt sessionId={$activeSession.sessionId} />
						<QuestionTray requests={$activeSession.pendingQuestions ?? []} working={$activeSession.isStreaming} connected={$activeWsConnected} onRespond={handleQuestionResponse} />
					{/if}
					<ChatInput compact={!showEmptyHero} {assistantMode} {onBrowserToggle} {onBrowserPreload} {browserOpen} {terminal} />
				{/key}
			{/if}
		</div>
	{/if}

	{#if showEmptyHero && showComposer}
		<!-- Desktop only: balances the flex:1 hero so the hero + persistent input pair sits
		     vertically centered — reproduces the old .empty-state
		     justify-content:center grouping without wrapping the input in a
		     state-specific ancestor (#665). -->
		<div class="empty-bottom-spacer" aria-hidden="true"></div>
	{/if}

	<!-- Permission gates still block their tool. Questions live above the composer.
	     A shell prompt dock answers them above the composer instead. -->
	{#if !PromptDock}
	{#if !showEmptyHero && showPermissionModal}
		<div class="modal-overlay">
			<div class="modal-backdrop"></div>
			<div class="modal-content">
				<div class="modal-card-wrapper">
					<PermissionQueue requests={dialogPermissions} onRespond={handlePermissionResponse} />
				</div>
			</div>
		</div>
	{/if}
	{/if}
</div>

<style>
	.chat-view {
		position: relative;
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
		background: var(--background);
		color: var(--foreground);
		-webkit-font-smoothing: antialiased;
		-moz-osx-font-smoothing: grayscale;
		text-rendering: optimizeLegibility;
	}

	.chat-column {
		position: relative;
		display: flex;
		flex-direction: column;
		flex: 1;
		min-width: 0;
		min-height: 0;
		overflow: hidden;
	}

	/* ========================================
	   TRANSITION ANIMATION: Empty → Active
	   ======================================== */

	@keyframes messageAreaReveal {
		from {
			opacity: 0;
			transform: translateY(-16px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	@keyframes inputSlideDown {
		from {
			opacity: 0.7;
			transform: translateY(-30vh);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.message-area-wrapper {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}

	/* The input slot is always rendered (see #665). It never shrinks in either
	   state; the empty-state layout (centering / width cap) is applied via
	   .empty-input-layout below, the active-state via .input-wrapper. */
	.input-slot {
		flex-shrink: 0;
	}

	.input-wrapper {
		flex-shrink: 0;
	}

	.transitioning-from-empty .message-area-wrapper {
		animation: messageAreaReveal 350ms cubic-bezier(0.16, 1, 0.3, 1) forwards;
	}

	.transitioning-from-empty .input-wrapper {
		animation: inputSlideDown 400ms cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
	}

	@media (prefers-reduced-motion: reduce) {
		.transitioning-from-empty .message-area-wrapper,
		.transitioning-from-empty .input-wrapper {
			animation: none;
		}
	}

	/* ========================================
	   EMPTY STATE
	   ======================================== */

	/* Hero region (workspace picker / cleared hint). Takes the top flex space with
	   its content bottom-anchored; the .empty-bottom-spacer below the input takes
	   the matching bottom flex space, so the hero + persistent input pair sits
	   vertically centered — reproducing the old .empty-state justify-content:center
	   grouping without wrapping the input in a state-specific ancestor (#665). */
	.empty-hero {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: flex-end;
		width: 100%;
		padding: 1rem 1rem 0;
		position: relative;
	}

	/* Same 768px measure as the transcript column and the composer dock, so the
	   hero and the island below it share one edge. */
	.empty-hero-content {
		display: flex;
		flex-direction: column;
		align-items: center;
		width: 100%;
		max-width: 768px;
	}

	/* A composer-free surface can center its hero on desktop. */
	.empty-hero.hero-only {
		justify-content: center;
		padding-bottom: 1rem;
	}

	.empty-bottom-spacer {
		flex: 1;
	}

	/* Overlay the error so it doesn't shift the vertically-centered content. */
	.empty-error-overlay {
		position: absolute;
		top: 1rem;
		left: 1rem;
		right: 1rem;
		z-index: 5;
	}

	/* Empty-state layout for the persistent input slot: centered and capped at the
	   same 768px content width as the hero above it. Tailwind preflight makes
	   box-sizing:border-box global, so the cap includes the gutters — ChatInput
	   supplies its own dock padding inside, so none is added here. */
	.input-slot.empty-input-layout {
		align-self: center;
		width: 100%;
		max-width: calc(768px + 3rem);
		margin-top: 0.75rem;
	}

	/* Cleared/empty existing-session hero. Reuses WorkspaceSelector's tokens
	   (font-display heading, muted-foreground secondary text) so it reads as the
	   same surface — just communicating "cleared" instead of "new". */
	.cleared-hero {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 8px;
		padding: 2rem 2rem 1.25rem;
		text-align: center;
		user-select: none;
	}

	.cleared-badge {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		margin-bottom: 10px;
		border-radius: 50%;
		color: var(--subtle-foreground);
		background: var(--scrim);
		border: 1px solid var(--border);
	}

	.cleared-title {
		font-family: var(--font-display, Georgia, serif);
		font-size: clamp(1.375rem, 4vw, 2rem);
		font-weight: 700;
		color: var(--foreground);
		letter-spacing: -0.02em;
		line-height: 1.15;
		max-width: 32ch;
		/* Long prompts double as titles — clamp to two lines with an ellipsis. */
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.cleared-hint {
		font-size: 0.875rem;
		color: var(--muted-foreground);
		line-height: 1.5;
		max-width: 40ch;
	}

	/* ========================================
	   Modal Overlay for Permission/Question
	   ======================================== */

	.modal-overlay {
		position: absolute;
		inset: 0;
		z-index: 50;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 1rem;
		overflow: hidden;
		animation: modalFadeIn 0.2s ease-out;
	}

	.modal-backdrop {
		position: absolute;
		inset: 0;
		background: color-mix(in oklch, var(--background) 70%, transparent);
		backdrop-filter: blur(8px);
		-webkit-backdrop-filter: blur(8px);
	}

	.modal-content {
		position: relative;
		z-index: 1;
		width: 100%;
		max-width: 40rem;
		max-height: 100%;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		overscroll-behavior: contain;
	}

	.modal-card-wrapper {
		flex-shrink: 0;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 0.75rem;
		box-shadow: var(--shadow-l);
		max-height: calc(100vh - 8rem);
		overflow-y: auto;
	}

	@keyframes modalFadeIn {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	@media (prefers-reduced-motion: reduce) {
		.modal-overlay {
			animation: none;
		}
	}

	/* ========================================
	   MESSAGE STYLING (inherited from ChatCard)
	   ======================================== */

	.chat-view :global(.prose) {
		color: var(--foreground);
		line-height: 1.6;
	}

	.chat-view :global(.prose a) {
		color: var(--primary);
		text-decoration-color: color-mix(in oklch, var(--primary) 50%, transparent);
	}

	.chat-view :global(.prose a:hover) {
		text-decoration-color: var(--primary);
		opacity: 0.9;
	}

	.chat-view :global(.prose pre) {
		background: var(--muted);
		border: 1px solid var(--border);
		box-shadow: var(--shadow-s);
		border-radius: var(--radius, 0.75rem);
		margin: 12px 0;
		padding: 12px 16px;
	}

	.chat-view :global(.prose code:not(pre code)) {
		background: var(--muted);
		color: var(--foreground);
		border: 1px solid var(--border);
		padding: 2px 6px;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		font-size: 0.875em;
		font-weight: 500;
	}
</style>

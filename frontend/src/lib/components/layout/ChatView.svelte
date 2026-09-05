<script lang="ts">
	/**
	/** Chat surface for code, chat and voice. Lane-specific heroes share one
	 * persistent editor. A live voice call adds compact controls alongside it. */
	import { onDestroy, tick, untrack } from 'svelte';
	import { activeSession, activeWsConnected } from '$lib/stores/active-session';
	import { notify } from '$lib/stores/notifications';
	import { createConnectionBannerGrace } from '$lib/stores/session/connection-banner-grace';
	import Banner from '$lib/components/Banner.svelte';
	import MessageArea from '$lib/components/chat/MessageArea.svelte';
	import TeamStatusBar from '$lib/components/TeamStatusBar.svelte';
	import ChatInput from '$lib/components/chat/ChatInput.svelte';
	import PermissionQueue from '$lib/components/PermissionQueue.svelte';
	import UserQuestion from '$lib/components/UserQuestion.svelte';
	import WorkspaceSelector from './WorkspaceSelector.svelte';
	import ChatModeHero from './ChatModeHero.svelte';
	import VoiceModeHero from './VoiceModeHero.svelte';
	import VoiceCallPanel from '$lib/components/voice/VoiceCallPanel.svelte';
	import { voiceCallState, isCallSurfaceActive } from '$lib/services/voiceCall';
	import { RotateCcw } from 'lucide-svelte';
	import { classifyChatSurface } from './chat-empty-state';

	interface Props {
		onBrowserToggle?: () => void;
		onBrowserPreload?: () => void;
		browserOpen?: boolean;
	}

	let { onBrowserToggle, onBrowserPreload, browserOpen = false }: Props = $props();

	// Classify the empty-message surface. While a session's history is being
	// fetched (boot restore OR an openChat navigation) messages is still [] but
	// sessionId already points at the target — showing an empty hero then would
	// invite sends into an invisible session AND could flash the cleared hint for
	// a session that turns out to have messages, so the load window renders the
	// message column (non-empty), not a hero. See chat-empty-state.ts + #558.
	const chatSurface = $derived(
		classifyChatSurface({
			sessionId: $activeSession.sessionId,
			messageCount: $activeSession.messages.length,
			isRestoringSession: $activeSession.isRestoringSession,
			isLoadingSession: $activeSession.isLoadingSession
		})
	);
	const isEmptyState = $derived(chatSurface === 'new' || chatSurface === 'cleared');
	// A BOUND, settled, empty session — a just-cleared conversation. Presents the
	// hero with the session's own title + a "cleared" hint instead of the
	// brand-new "Let's build your project" workspace picker.
	const isClearedSession = $derived(chatSurface === 'cleared');
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
	let surfaceHeight = $state(0);
	let composerHeight = $state(0);
	$effect(() => {
		if (showEmptyHero && surfaceHeight > 0 && composerHeight > surfaceHeight && window.innerWidth <= 1080) {
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
	const showPermissionModal = $derived(
		!voiceCallActive &&
			$activeSession.pendingPermissions &&
			$activeSession.pendingPermissions.length > 0
	);

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

	$effect(() => {
		const isEmpty = isEmptyState;
		const wasEmpty = untrack(() => previousIsEmpty);
		if (wasEmpty === isEmpty) return;
		console.debug(`[ChatView] isEmptyState changed: ${wasEmpty} → ${isEmpty}, sessionId=${$activeSession.sessionId}, msgs=${$activeSession.messages.length}`);
		previousIsEmpty = isEmpty;

		if (wasEmpty && !isEmpty) {
			// Empty → active transition: animate the reveal
			transitioningFromEmpty = true;
			const timer = setTimeout(() => {
				transitioningFromEmpty = false;
			}, 450);
			return () => clearTimeout(timer);
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
		activeSession.sendUserQuestionResponse(request_id, tool_use_id, answers, sessionId, {
			dismissed,
			skipped
		});
	}

	// Handle project selection from WorkspaceSelector
	function handleProjectSelect(projectId: string, projectV2Id?: string | null) {
		activeSession.setProject(projectId, projectV2Id ?? null);
	}

	// Manually re-establish the WebSocket (the Reconnect button on the
	// disconnection banner). Delegates to the existing reconnect machinery —
	// resets backoff then reconnects — rather than inventing a new path.
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
					<Banner severity="error" message={$activeSession.error} dismissible onDismiss={() => activeSession.clearError()} />
				</div>
			{/if}

			<div class="empty-hero-content">
				{#if isClearedSession}
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
				{:else if surfaceMode === 'chat'}
					<!-- Projectless conversation lane — no workspace to pick. -->
					<ChatModeHero />
				{:else if surfaceMode === 'voice'}
					<!-- Voice lane, unbound: the hero carries the only action (start a
					     call), and .input-slot is withheld — see showComposer. -->
					<VoiceModeHero />
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
					<div class="connection-banner">
						<Banner
							severity="warning"
							message="Connection lost — trying to reconnect. Live updates are paused."
							action={{ label: 'Reconnect', onClick: handleReconnect }}
						/>
					</div>
				{/if}
				{#if $activeSession.sessionId}
					<TeamStatusBar sessionId={$activeSession.sessionId} />
				{/if}
				<!-- No call surface here any more (VM18): the call controls live in the
				     composer slot below, so the transcript column looks the same during
				     a call as it does outside one. -->
				<!-- MessageArea handles session-switch scroll save/restore internally
				     via $effect watching sessionId. No {#key} needed — avoiding
				     component destruction prevents a Svelte 5 reactivity edge case
				     where the recreated component loses reactive updates during
				     new session creation (sessionId: undefined → new ID). -->
				<MessageArea />
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
			<ChatInput compact={!showEmptyHero} {onBrowserToggle} {onBrowserPreload} {browserOpen} />
		</div>
	{/if}

	{#if showEmptyHero && showComposer}
		<!-- Desktop only: balances the flex:1 hero so the hero + persistent input pair sits
		     vertically centered — reproduces the old .empty-state
		     justify-content:center grouping without wrapping the input in a
		     state-specific ancestor (#665). -->
		<div class="empty-bottom-spacer" aria-hidden="true"></div>
	{/if}

	<!-- Modal Overlay for Permission/Question Queues. Now anchored to .chat-view
	     (position:absolute; inset:0; z-index:50) so it still overlays the whole
	     surface, including the persistent input. Active-state only. -->
	{#if !showEmptyHero && (showPermissionModal || ($activeSession.pendingQuestions && $activeSession.pendingQuestions.length > 0))}
		<div class="modal-overlay">
			<div class="modal-backdrop"></div>
			<div class="modal-content">
				{#if showPermissionModal}
					<div class="modal-card-wrapper">
						<PermissionQueue
							requests={$activeSession.pendingPermissions}
							onRespond={handlePermissionResponse}
						/>
					</div>
				{/if}

				{#if $activeSession.pendingQuestions && $activeSession.pendingQuestions.length > 0}
					<div class="modal-card-wrapper space-y-3">
						{#each $activeSession.pendingQuestions as question (question.request_id)}
							<UserQuestion
								data={question}
								onRespond={handleQuestionResponse}
							/>
						{/each}
					</div>
				{/if}
			</div>
			<!-- No floating Stop control lives here. #789 (c6082dd2) used to dock a
			     second StopButton above the backdrop so the composer's own Stop
			     stayed reachable during a prompt; it read as a stray control
			     punching through the dialog, so it is gone for BOTH the permission
			     prompt and the question modal. A modal is answered or dismissed
			     first; the composer's normal Stop takes over once it clears. -->
		</div>
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

	/* Thin, unobtrusive disconnection notice pinned above the message list. */
	.connection-banner {
		flex-shrink: 0;
		padding: 6px 12px 0;
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

	@media (max-width: 1080px) {
		/* The invitation stays at its natural size and a fixed top inset. The
		   whole empty surface scrolls; the workspace popup uses the top layer. */
		.chat-view.is-empty { overflow-y: auto; }
		.is-empty .empty-hero {
			flex: 0 0 auto;
			justify-content: flex-start;
			padding-top: 1rem;
		}
		.is-empty .empty-hero-content { flex-shrink: 0; }
		.is-empty .empty-bottom-spacer { display: none; }
		.chat-view.is-empty .input-slot.empty-input-layout {
			position: sticky;
			bottom: 0;
			z-index: 10;
			background: var(--background);
			margin-top: 0;
		}
		/* An oversized dock must scroll normally, not pin its editor offscreen.
		   Do not clip the slot: its anchored popovers extend outside the dock. */
		.chat-view.is-empty .input-slot.empty-input-layout.composer-overflow { position: static; }
		/* Short expanded panels must not permanently cover the invitation. */
		.chat-view.is-empty .input-slot.empty-input-layout:global([data-panel-flow]) { position: static; }
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

	.chat-view :global(.user-message) {
		background: color-mix(in oklch, var(--primary) 15%, var(--card));
		border: 1px solid color-mix(in oklch, var(--primary) 25%, var(--border));
		border-radius: var(--radius, 0.75rem);
		padding: 12px 16px;
		box-shadow: var(--shadow-s);
		transition: box-shadow 0.2s ease, transform 0.2s ease, background-color 0.2s ease;
	}

	.chat-view :global(.user-message:hover) {
		box-shadow: var(--shadow-m);
		background: color-mix(in oklch, var(--primary) 20%, var(--card));
	}

	.chat-view :global(.user-message p),
	.chat-view :global(.prose) {
		color: var(--foreground);
		line-height: 1.6;
	}

	.chat-view :global(.user-message p) {
		font-size: 0.9375rem;
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

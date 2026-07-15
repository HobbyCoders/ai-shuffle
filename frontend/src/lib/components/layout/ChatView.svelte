<script lang="ts">
	/**
	 * ChatView - Full-panel chat experience
	 *
	 * Renders the chat UI directly in the main panel.
	 * Reads from the activeSession store — no props needed.
	 *
	 * Two states:
	 * - Empty: WorkspaceSelector + ChatInput centered (the ChatInput's
	 *   ContextPillBar hosts the Local/Worktree execution-mode toggle above it)
	 * - Active: MessageArea + ChatInput at bottom
	 */
	import { onDestroy, untrack } from 'svelte';
	import { activeSession, activeWsConnected } from '$lib/stores/active-session';
	import { stopSessionTask } from '$lib/api/sessions';
	import { notify } from '$lib/stores/notifications';
	import { createConnectionBannerGrace } from '$lib/stores/session/connection-banner-grace';
	import Banner from '$lib/components/Banner.svelte';
	import MessageArea from '$lib/components/chat/MessageArea.svelte';
	import TeamStatusBar from '$lib/components/TeamStatusBar.svelte';
	import ChatInput from '$lib/components/chat/ChatInput.svelte';
	import QueuedMessages from '$lib/components/chat/QueuedMessages.svelte';
	import ActiveTasks from '$lib/components/chat/ActiveTasks.svelte';
	import PermissionQueue from '$lib/components/PermissionQueue.svelte';
	import UserQuestion from '$lib/components/UserQuestion.svelte';
	import WorkspaceSelector from './WorkspaceSelector.svelte';
	import { RotateCcw } from 'lucide-svelte';
	import { classifyChatSurface } from './chat-empty-state';

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

	// Handle permission response — Slice B widens decision to include 'defer'.
	function handlePermissionResponse(detail: {
		request_id: string;
		decision: 'allow' | 'deny' | 'defer';
		remember?: 'none' | 'session' | 'profile';
		pattern?: string;
	}) {
		const { request_id, decision, remember, pattern } = detail;
		const sessionId =
			$activeSession.pendingPermissions.find(req => req.request_id === request_id)?.session_id ??
			$activeSession.deferredPermissions.find(req => req.request_id === request_id)?.session_id ??
			$activeSession.sessionId ??
			undefined;
		activeSession.sendPermissionResponse(request_id, decision, remember, pattern, sessionId);
	}

	// Slice B: ask the server to re-broadcast every previously-deferred
	// permission request as fresh `permission_request` events.
	function handleRequeueDeferred() {
		const sessionId = $activeSession.sessionId;
		if (!sessionId) return;
		activeSession.requeueDeferredPermissions(sessionId);
	}

	// Handle question response
	function handleQuestionResponse(detail: {
		request_id: string;
		tool_use_id: string;
		answers: Record<string, string | string[]>;
	}) {
		const { request_id, tool_use_id, answers } = detail;
		const sessionId =
			$activeSession.pendingQuestions.find(q => q.request_id === request_id)?.session_id ??
			$activeSession.sessionId ??
			undefined;
		activeSession.sendUserQuestionResponse(request_id, tool_use_id, answers, sessionId);
	}

	// Stop one in-flight background task (the "x" on the ActiveTasks strip).
	// The task drops off the next /live poll once the backend confirms the stop.
	// Returns true on a successful stop; false (with a toast) when the stop
	// didn't take — so ActiveTasks can re-enable the button instead of leaving
	// it stuck disabled on a ghost row.
	async function handleStopTask(taskId: string): Promise<boolean> {
		const sessionId = $activeSession.sessionId;
		if (!sessionId) return false;
		try {
			const result = await stopSessionTask(sessionId, taskId);
			if (!result.ok) {
				notify.error(
					`Couldn't stop task — ${result.detail ?? 'it may have already finished'}.`
				);
				return false;
			}
			return true;
		} catch (e) {
			notify.error('Failed to stop task. Please try again.');
			return false;
		}
	}

	// Handle project selection from WorkspaceSelector
	function handleProjectSelect(projectId: string) {
		activeSession.setProject(projectId);
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

<div class="chat-view" class:transitioning-from-empty={transitioningFromEmpty}>
	{#if isEmptyState}
		<!-- Empty state: Workspace selector + centered input -->
		<div class="empty-state">
			{#if $activeSession.error}
				<div class="empty-error-overlay">
					<Banner severity="error" message={$activeSession.error} dismissible onDismiss={() => activeSession.clearError()} />
				</div>
			{/if}

			<div class="empty-state-content">
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
				{:else}
					<WorkspaceSelector
						selectedProject={$activeSession.project}
						onSelectProject={handleProjectSelect}
					/>
				{/if}
				<div class="empty-input-wrapper">
					<ChatInput />
				</div>
			</div>
		</div>
	{:else}
		<!-- Active chat: Messages + input -->
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
				<!-- MessageArea handles session-switch scroll save/restore internally
				     via $effect watching sessionId. No {#key} needed — avoiding
				     component destruction prevents a Svelte 5 reactivity edge case
				     where the recreated component loses reactive updates during
				     new session creation (sessionId: undefined → new ID). -->
				<MessageArea />
			</div>

			<!-- Queued Messages Bar (Send Later) -->
			{#if $activeSession.queuedMessages && $activeSession.queuedMessages.length > 0}
				<QueuedMessages />
			{/if}

			<div class="input-wrapper">
				<!-- In-flight background tasks (sub-agents + shell subprocesses).
				     Renders nothing when empty; appears during a turn — and notably
				     while the turn is PARKED waiting on tasks — so the user can see
				     what's pending. -->
				{#if $activeSession.activeTasks && $activeSession.activeTasks.length > 0}
					<div class="active-tasks-strip">
						<ActiveTasks tasks={$activeSession.activeTasks} onStop={handleStopTask} />
					</div>
				{/if}
				<!-- Slice B: deferred-only strip when there are no active requests to
				     review (when there are, the strip is rendered inside the modal
				     PermissionQueue above). -->
				{#if (!$activeSession.pendingPermissions || $activeSession.pendingPermissions.length === 0) && $activeSession.deferredPermissions && $activeSession.deferredPermissions.length > 0}
					<div class="inline-deferred-strip">
						<PermissionQueue
							requests={[]}
							deferred={$activeSession.deferredPermissions}
							onRespond={handlePermissionResponse}
							onRequeueDeferred={handleRequeueDeferred}
						/>
					</div>
				{/if}
				<ChatInput compact />
			</div>

			<!-- Modal Overlay for Permission/Question Queues -->
			{#if ($activeSession.pendingPermissions && $activeSession.pendingPermissions.length > 0) || ($activeSession.pendingQuestions && $activeSession.pendingQuestions.length > 0)}
				<div class="modal-overlay">
					<div class="modal-backdrop"></div>
					<div class="modal-content">
						{#if $activeSession.pendingPermissions && $activeSession.pendingPermissions.length > 0}
							<div class="modal-card-wrapper">
								<PermissionQueue
									requests={$activeSession.pendingPermissions}
									deferred={$activeSession.deferredPermissions ?? []}
									onRespond={handlePermissionResponse}
									onRequeueDeferred={handleRequeueDeferred}
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
				</div>
			{/if}
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

	.input-wrapper {
		flex-shrink: 0;
	}

	/* Thin, unobtrusive disconnection notice pinned above the message list. */
	.connection-banner {
		flex-shrink: 0;
		padding: 6px 12px 0;
	}

	.inline-deferred-strip {
		padding: 4px 12px 0;
	}

	.active-tasks-strip {
		padding: 4px 0 0;
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

	.empty-state {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 1rem;
		position: relative;
	}

	.empty-state-content {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.5rem;
		width: 100%;
		max-width: 640px;
	}

	/* Overlay the error so it doesn't shift the vertically-centered content. */
	.empty-error-overlay {
		position: absolute;
		top: 1rem;
		left: 1rem;
		right: 1rem;
		z-index: 5;
	}

	.empty-input-wrapper {
		width: 100%;
	}

	/* Cleared/empty existing-session hero. Reuses WorkspaceSelector's tokens
	   (font-display heading, muted-foreground secondary text) so it reads as the
	   same surface — just communicating "cleared" instead of "new". */
	.cleared-hero {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 6px;
		padding: 2rem 2rem 1rem;
		text-align: center;
		user-select: none;
	}

	.cleared-badge {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		margin-bottom: 8px;
		border-radius: 50%;
		color: var(--muted-foreground);
		background: var(--muted);
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

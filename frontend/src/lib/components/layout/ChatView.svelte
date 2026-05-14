<script lang="ts">
	/**
	 * ChatView - Full-panel chat experience
	 *
	 * Renders the chat UI directly in the main panel.
	 * Reads from the activeSession store — no props needed.
	 *
	 * Two states:
	 * - Empty: WorkspaceSelector + ChatInput centered
	 * - Active: MessageArea + ChatInput at bottom + ExecutionModeBar below
	 */
	import { untrack } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import MessageArea from '$lib/components/chat/MessageArea.svelte';
	import TeamStatusBar from '$lib/components/TeamStatusBar.svelte';
	import ChatInput from '$lib/components/chat/ChatInput.svelte';
	import QueuedMessages from '$lib/components/chat/QueuedMessages.svelte';
	import PermissionQueue from '$lib/components/PermissionQueue.svelte';
	import UserQuestion from '$lib/components/UserQuestion.svelte';
	import RateLimitBanner from '$lib/components/RateLimitBanner.svelte';
	import { githubAuthenticated } from '$lib/stores/auth';
	import WorkspaceSelector from './WorkspaceSelector.svelte';
	import ExecutionModeBar from './ExecutionModeBar.svelte';

	// Check if this is an empty state (no messages yet)
	const isEmptyState = $derived($activeSession.messages.length === 0);

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

	// Handle project selection from WorkspaceSelector
	function handleProjectSelect(projectId: string) {
		activeSession.setProject(projectId);
	}
</script>

<div class="chat-view" class:transitioning-from-empty={transitioningFromEmpty}>
	{#if isEmptyState}
		<!-- Empty state: Workspace selector + centered input -->
		<div class="empty-state">
			{#if $activeSession.error}
				<div class="error-banner">
					<span>{$activeSession.error}</span>
					<button onclick={() => activeSession.clearError()}>&times;</button>
				</div>
			{/if}

			<div class="empty-state-content">
				<WorkspaceSelector
					selectedProject={$activeSession.project}
					onSelectProject={handleProjectSelect}
				/>
				<div class="empty-input-wrapper">
					<RateLimitBanner />
					<ChatInput />
				</div>
				{#if $githubAuthenticated}
					<ExecutionModeBar />
				{/if}
			</div>
		</div>
	{:else}
		<!-- Active chat: Messages + input -->
		<div class="chat-column">
			<div class="message-area-wrapper">
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
				<RateLimitBanner />
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

	.inline-deferred-strip {
		padding: 4px 12px 0;
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

	.empty-input-wrapper {
		width: 100%;
	}

	.error-banner {
		position: absolute;
		top: 1rem;
		left: 1rem;
		right: 1rem;
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 0.75rem 1rem;
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--destructive) 30%, transparent);
		border-radius: 0.5rem;
		color: var(--destructive);
		font-size: 0.875rem;
	}

	.error-banner button {
		flex-shrink: 0;
		background: none;
		border: none;
		color: var(--destructive);
		font-size: 1.25rem;
		cursor: pointer;
		padding: 0;
		line-height: 1;
		opacity: 0.7;
	}

	.error-banner button:hover {
		opacity: 1;
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

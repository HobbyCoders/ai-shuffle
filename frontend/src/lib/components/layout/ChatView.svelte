<script lang="ts">
	/**
	 * ChatView - Full-panel chat experience
	 *
	 * Renders the chat UI directly in the main panel.
	 *
	 * Two states:
	 * - Empty: WorkspaceSelector + ChatInput centered
	 * - Active: MessageArea + ChatInput at bottom + ExecutionModeBar below
	 */
	import { untrack } from 'svelte';
	import { tabs, allTabs } from '$lib/stores/tabs';
	import { MessageArea, ChatInput } from '$lib/components/chat';
	import PermissionQueue from '$lib/components/PermissionQueue.svelte';
	import UserQuestion from '$lib/components/UserQuestion.svelte';
	import { githubAuthenticated } from '$lib/stores/auth';
	import WorkspaceSelector from './WorkspaceSelector.svelte';
	import ExecutionModeBar from './ExecutionModeBar.svelte';

	interface Props {
		tabId: string;
	}

	let { tabId }: Props = $props();

	// Get the tab data from the tabs store
	const tab = $derived($allTabs.find((t) => t.id === tabId));

	// Check if this is an empty state (no messages yet)
	const isEmptyState = $derived(tab ? tab.messages.length === 0 : true);

	// Animation: Track transition from empty → active state
	let transitioningFromEmpty = $state(false);
	let previousIsEmpty = $state(true);

	$effect(() => {
		const isEmpty = isEmptyState;
		const wasEmpty = untrack(() => previousIsEmpty);
		if (wasEmpty === isEmpty) return;
		previousIsEmpty = isEmpty;

		if (wasEmpty && !isEmpty) {
			transitioningFromEmpty = true;
			const timer = setTimeout(() => {
				transitioningFromEmpty = false;
			}, 450);
			return () => clearTimeout(timer);
		}
	});

	// Handle permission response
	function handlePermissionResponse(detail: {
		request_id: string;
		decision: 'allow' | 'deny';
		remember?: 'none' | 'session' | 'profile';
		pattern?: string;
	}) {
		if (!tab) return;
		const { request_id, decision, remember, pattern } = detail;
		tabs.sendPermissionResponse(tab.id, request_id, decision, remember, pattern);
	}

	// Handle question response
	function handleQuestionResponse(detail: {
		request_id: string;
		tool_use_id: string;
		answers: Record<string, string | string[]>;
	}) {
		if (!tab) return;
		const { request_id, tool_use_id, answers } = detail;
		tabs.sendUserQuestionResponse(tab.id, request_id, tool_use_id, answers);
	}

	// Handle project selection from WorkspaceSelector
	function handleProjectSelect(projectId: string) {
		if (!tab) return;
		tabs.setTabProject(tab.id, projectId);
	}

	// Loading timeout: show error after 5 seconds if tab never resolves
	let loadingTimedOut = $state(false);
	$effect(() => {
		if (tab) {
			loadingTimedOut = false;
			return;
		}
		loadingTimedOut = false;
		const timer = setTimeout(() => {
			loadingTimedOut = true;
		}, 5000);
		return () => clearTimeout(timer);
	});
</script>

<div class="chat-view" class:transitioning-from-empty={transitioningFromEmpty}>
	{#if tab}
		{#if isEmptyState}
			<!-- Empty state: Workspace selector + centered input -->
			<div class="empty-state">
				{#if tab.error}
					<div class="error-banner">
						<span>{tab.error}</span>
						<button onclick={() => tabs.clearTabError(tab.id)}>&times;</button>
					</div>
				{/if}

				<div class="empty-state-content">
					<WorkspaceSelector
						selectedProject={tab.project}
						onSelectProject={handleProjectSelect}
					/>
					<div class="empty-input-wrapper">
						<ChatInput {tab} />
					</div>
					{#if $githubAuthenticated}
						<ExecutionModeBar {tab} />
					{/if}
				</div>
			</div>
		{:else}
			<!-- Active chat: Messages + input at bottom -->
			<div class="message-area-wrapper">
				<!-- Key by tabId so MessageArea is destroyed/recreated on tab switch.
				     Without this, Svelte reuses the same component instance and the
				     onMount/onDestroy scroll-position save/restore logic never fires. -->
				{#key tabId}
					<MessageArea {tab} />
				{/key}
			</div>

			<div class="input-wrapper">
				<ChatInput {tab} compact />
			</div>

			<!-- Modal Overlay for Permission/Question Queues -->
			{#if (tab.pendingPermissions && tab.pendingPermissions.length > 0) || (tab.pendingQuestions && tab.pendingQuestions.length > 0)}
				<div class="modal-overlay">
					<div class="modal-backdrop"></div>
					<div class="modal-content">
						{#if tab.pendingPermissions && tab.pendingPermissions.length > 0}
							<div class="modal-card-wrapper">
								<PermissionQueue
									requests={tab.pendingPermissions}
									onRespond={handlePermissionResponse}
								/>
							</div>
						{/if}

						{#if tab.pendingQuestions && tab.pendingQuestions.length > 0}
							<div class="modal-card-wrapper space-y-3">
								{#each tab.pendingQuestions as question (question.request_id)}
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
		{/if}
	{:else}
		<div class="chat-loading">
			{#if loadingTimedOut}
				<p class="loading-error">Could not load chat</p>
				<p class="loading-error-hint">The tab data could not be found. It may have been closed.</p>
			{:else}
				<div class="spinner"></div>
				<p>Loading chat...</p>
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
	   LOADING STATE
	   ======================================== */

	.chat-loading {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 16px;
		color: var(--muted-foreground);
		padding: 24px;
	}

	.chat-loading p {
		color: var(--foreground);
		font-size: 0.875rem;
		font-weight: 500;
	}

	.loading-error {
		color: var(--destructive) !important;
		font-size: 0.9375rem !important;
		font-weight: 600 !important;
	}

	.loading-error-hint {
		color: var(--muted-foreground) !important;
		font-size: 0.8125rem !important;
		font-weight: 400 !important;
		text-align: center;
		max-width: 300px;
	}

	.spinner {
		width: 24px;
		height: 24px;
		border: 2px solid var(--border);
		border-top-color: var(--primary);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to { transform: rotate(360deg); }
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

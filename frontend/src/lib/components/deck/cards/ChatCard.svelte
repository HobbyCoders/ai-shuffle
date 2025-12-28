<script lang="ts">
	/**
	 * ChatCard - Complete chat experience inside a card
	 *
	 * Integrates all extracted chat components for full feature parity
	 * with the main chat interface.
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { tabs, allTabs } from '$lib/stores/tabs';
	import { MessageArea, ChatInput } from '$lib/components/chat';
	import PermissionQueue from '$lib/components/PermissionQueue.svelte';
	import UserQuestion from '$lib/components/UserQuestion.svelte';
	import ConnectionStatus from '$lib/components/ConnectionStatus.svelte';

	interface Props {
		card: DeckCard;
		tabId: string;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		onFork?: (sessionId: string, messageIndex: number, messageId: string) => void;
		mobile?: boolean;
	}

	let {
		card,
		tabId,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		onFork,
		mobile = false
	}: Props = $props();

	// Get the tab data from the tabs store
	const tab = $derived($allTabs.find((t) => t.id === tabId));

	// Handle permission response
	function handlePermissionResponse(
		event: CustomEvent<{
			request_id: string;
			decision: 'allow' | 'deny';
			remember?: 'none' | 'session' | 'profile';
			pattern?: string;
		}>
	) {
		if (!tab) return;
		const { request_id, decision, remember, pattern } = event.detail;
		tabs.sendPermissionResponse(tab.id, request_id, decision, remember, pattern);
	}

	// Handle question response
	function handleQuestionResponse(
		event: CustomEvent<{
			request_id: string;
			tool_use_id: string;
			answers: Record<string, string | string[]>;
		}>
	) {
		if (!tab) return;
		const { request_id, tool_use_id, answers } = event.detail;
		tabs.sendUserQuestionResponse(tab.id, request_id, tool_use_id, answers);
	}

	// Handle fork action
	function handleFork(sessionId: string, messageIndex: number, messageId: string) {
		onFork?.(sessionId, messageIndex, messageId);
	}
</script>

{#if mobile}
	<!-- Mobile: Full-screen card with no BaseCard wrapper or ChatHeader -->
	<!-- MobileWorkspace provides the header with title and close button -->
	<div class="chat-card-content mobile">
		{#if tab}
			<!-- Messages fill the space -->
			<MessageArea {tab} onFork={handleFork} />

			<!-- Permission Queue -->
			{#if tab.pendingPermissions && tab.pendingPermissions.length > 0}
				<div class="border-t border-warning/30 bg-warning/5 p-3">
					<div class="max-w-full">
						<PermissionQueue
							requests={tab.pendingPermissions}
							on:respond={handlePermissionResponse}
						/>
					</div>
				</div>
			{/if}

			<!-- User Question Queue -->
			{#if tab.pendingQuestions && tab.pendingQuestions.length > 0}
				<div class="border-t border-info/30 bg-info/5 p-3">
					<div class="max-w-full">
						{#each tab.pendingQuestions as question (question.request_id)}
							<UserQuestion
								data={question}
								on:respond={handleQuestionResponse}
							/>
						{/each}
					</div>
				</div>
			{/if}

			<!-- Input at the bottom -->
			<ChatInput {tab} compact />
		{:else}
			<div class="chat-loading">
				<div class="spinner"></div>
				<p>Loading chat...</p>
			</div>
		{/if}
	</div>
{:else}
	<!-- Desktop: Full BaseCard with all features -->
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="chat-card-content">
			{#if tab}
				<!-- Main message area -->
				<MessageArea {tab} onFork={handleFork} />

				<!-- Permission Queue -->
				{#if tab.pendingPermissions && tab.pendingPermissions.length > 0}
					<div class="border-t border-warning/30 bg-warning/5 p-3">
						<div class="max-w-full">
							<PermissionQueue
								requests={tab.pendingPermissions}
								on:respond={handlePermissionResponse}
							/>
						</div>
					</div>
				{/if}

				<!-- User Question Queue -->
				{#if tab.pendingQuestions && tab.pendingQuestions.length > 0}
					<div class="border-t border-info/30 bg-info/5 p-3">
						<div class="max-w-full">
							{#each tab.pendingQuestions as question (question.request_id)}
								<UserQuestion
									data={question}
									on:respond={handleQuestionResponse}
								/>
							{/each}
						</div>
					</div>
				{/if}

				<!-- Input area -->
				<ChatInput {tab} compact />
			{:else}
				<div class="chat-loading">
					<div class="spinner"></div>
					<p>Loading chat...</p>
				</div>
			{/if}
		</div>
	</BaseCard>
{/if}

<style>
	.chat-card-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
		background: var(--card);
		color: var(--card-foreground);
		/* Improved text rendering for message readability */
		-webkit-font-smoothing: antialiased;
		-moz-osx-font-smoothing: grayscale;
		text-rendering: optimizeLegibility;
		/* Clip internal content (messages) but allow fixed-position elements to escape */
		border-radius: 0 0 12px 12px;
	}

	.chat-card-content.mobile {
		height: 100%;
	}

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

	.spinner {
		width: 24px;
		height: 24px;
		border: 2px solid var(--border);
		border-top-color: var(--primary);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	/* Message area takes remaining space */
	.chat-card-content :global(.flex-1) {
		flex: 1;
		min-height: 0;
	}

	/* User message styling - distinct teal/primary tint with good contrast */
	.chat-card-content :global(.user-message) {
		background: color-mix(in oklch, var(--primary) 10%, var(--card));
		border: 1px solid color-mix(in oklch, var(--primary) 25%, var(--border));
		border-radius: 12px;
		padding: 12px 16px;
		box-shadow: var(--shadow-s);
		transition: box-shadow 0.2s ease, transform 0.2s ease, background-color 0.2s ease;
	}

	.chat-card-content :global(.user-message:hover) {
		box-shadow: var(--shadow-m);
		background: color-mix(in oklch, var(--primary) 14%, var(--card));
	}

	/* Assistant messages - no bubble, clean layout */

	/* Ensure text readability in messages - improved line height and color */
	.chat-card-content :global(.user-message p),
	.chat-card-content :global(.prose) {
		color: var(--foreground);
		line-height: 1.6;
	}

	.chat-card-content :global(.user-message p) {
		font-size: 0.9375rem;
	}

	/* Fix prose link colors for better visibility */
	.chat-card-content :global(.prose a) {
		color: var(--primary);
		text-decoration-color: color-mix(in oklch, var(--primary) 50%, transparent);
	}

	.chat-card-content :global(.prose a:hover) {
		text-decoration-color: var(--primary);
		opacity: 0.9;
	}

	/* Code block styling in messages */
	.chat-card-content :global(.prose pre) {
		background: var(--muted);
		border: 1px solid var(--border);
		box-shadow: var(--shadow-s);
		border-radius: 8px;
		margin: 12px 0;
		padding: 12px 16px;
	}

	.chat-card-content :global(.prose code:not(pre code)) {
		background: var(--muted);
		color: var(--foreground);
		border: 1px solid var(--border);
		padding: 2px 6px;
		border-radius: 4px;
		font-size: 0.875em;
		font-weight: 500;
	}

	/* Error message styling */
	.chat-card-content :global(.bg-destructive\/10) {
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
	}

	/* Permission and question queue sections */
	.chat-card-content :global(.border-warning\/30) {
		border-color: color-mix(in oklch, var(--warning) 30%, transparent);
	}

	.chat-card-content :global(.bg-warning\/5) {
		background: color-mix(in oklch, var(--warning) 8%, transparent);
	}

	.chat-card-content :global(.border-info\/30) {
		border-color: color-mix(in oklch, var(--info) 30%, transparent);
	}

	.chat-card-content :global(.bg-info\/5) {
		background: color-mix(in oklch, var(--info) 8%, transparent);
	}

	/* Scrollbar styling for dark mode readability */
	.chat-card-content :global(::-webkit-scrollbar-thumb) {
		background: var(--scrollbar-thumb);
	}

	.chat-card-content :global(::-webkit-scrollbar-thumb:hover) {
		background: var(--scrollbar-thumb-hover);
	}

	/* Action buttons hover state */
	.chat-card-content :global(button) {
		transition: all 0.15s ease;
	}

	.chat-card-content :global(button:hover:not(:disabled)) {
		background-color: var(--accent);
	}

	.chat-card-content :global(button:focus-visible) {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Muted foreground text should be readable */
	.chat-card-content :global(.text-muted-foreground) {
		color: var(--muted-foreground);
	}

	/* Ensure proper foreground colors */
	.chat-card-content :global(.text-foreground) {
		color: var(--foreground);
	}

	/* Input and select styling for dropdowns */
	.chat-card-content :global(select),
	.chat-card-content :global(input[type="text"]),
	.chat-card-content :global(textarea) {
		background-color: var(--input);
		color: var(--foreground);
		border-color: var(--border);
	}

	.chat-card-content :global(select:focus),
	.chat-card-content :global(input[type="text"]:focus),
	.chat-card-content :global(textarea:focus) {
		border-color: var(--ring);
		outline: none;
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 25%, transparent);
	}

	/* Select option styling */
	.chat-card-content :global(select option) {
		background-color: var(--popover);
		color: var(--popover-foreground);
	}

	/* Dropdown/popover styling */
	.chat-card-content :global([data-radix-popper-content-wrapper]),
	.chat-card-content :global(.dropdown-content),
	.chat-card-content :global([role="listbox"]),
	.chat-card-content :global([role="menu"]) {
		background-color: var(--popover);
		color: var(--popover-foreground);
		border: 1px solid var(--border);
		box-shadow: var(--shadow-l);
	}

	/* Dropdown item hover states */
	.chat-card-content :global([role="option"]:hover),
	.chat-card-content :global([role="menuitem"]:hover) {
		background-color: var(--accent);
		color: var(--accent-foreground);
	}
</style>

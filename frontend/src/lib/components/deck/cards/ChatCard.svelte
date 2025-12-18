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
	import { ChatHeader, MessageArea, ChatInput } from '$lib/components/chat';
	import PermissionQueue from '$lib/components/PermissionQueue.svelte';
	import UserQuestion from '$lib/components/UserQuestion.svelte';
	import ConnectionStatus from '$lib/components/ConnectionStatus.svelte';

	interface Props {
		card: DeckCard;
		tabId: string;
		onClose: () => void;
		onMinimize: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onFork?: (sessionId: string, messageIndex: number, messageId: string) => void;
		mobile?: boolean;
	}

	let {
		card,
		tabId,
		onClose,
		onMinimize,
		onMaximize,
		onFocus,
		onMove,
		onResize,
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
	<!-- Mobile: No BaseCard wrapper, just the content -->
	<div class="chat-card-content mobile">
		{#if tab}
			<ChatHeader {tab} compact />
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
	<BaseCard {card} {onClose} {onMinimize} {onMaximize} {onFocus} {onMove} {onResize}>
		<div class="chat-card-content">
			{#if tab}
				<!-- Header with context usage, profile/project selectors -->
				<div class="chat-header-section">
					<ChatHeader {tab} compact />
					<div class="connection-status-wrapper">
						<ConnectionStatus wsConnected={tab.wsConnected} />
					</div>
				</div>

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
	}

	.chat-card-content.mobile {
		height: 100%;
	}

	.chat-header-section {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 8px 12px;
		border-bottom: 1px solid hsl(var(--border) / 0.5);
		flex-shrink: 0;
	}

	.connection-status-wrapper {
		flex-shrink: 0;
	}

	.chat-loading {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		color: hsl(var(--muted-foreground));
	}

	.spinner {
		width: 24px;
		height: 24px;
		border: 2px solid hsl(var(--border));
		border-top-color: hsl(var(--primary));
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
</style>

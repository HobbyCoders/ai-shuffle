<script lang="ts">
	/**
	 * ChatCardContent - The content area of a chat card
	 *
	 * This component bridges the workspace card system with the existing
	 * tabs store. Each chat card represents one "tab" from the original system.
	 *
	 * For the initial implementation, this shows a placeholder with session info.
	 * The full chat UI will be migrated incrementally.
	 */

	import { onMount, onDestroy } from 'svelte';
	import {
		tabs,
		allTabs,
		profiles,
		projects,
		type ChatTab,
		type ChatMessage
	} from '$lib/stores/tabs';
	import { workspace } from '$lib/stores/workspace';
	import { marked } from 'marked';

	interface Props {
		cardId: string;
		sessionId: string | null;
	}

	let { cardId, sessionId }: Props = $props();

	// Find or create the tab for this card
	let tabId = $state<string | null>(null);
	let tab = $state<ChatTab | null>(null);

	// Input state
	let inputValue = $state('');
	let inputRef: HTMLTextAreaElement;

	// Messages container for auto-scroll
	let messagesContainer: HTMLDivElement;

	// Derive current tab from allTabs
	const currentTab = $derived(tabId ? $allTabs.find(t => t.id === tabId) : null);

	onMount(async () => {
		// Check if there's already a tab for this session
		if (sessionId) {
			const existingTab = $allTabs.find(t => t.sessionId === sessionId);
			if (existingTab) {
				tabId = existingTab.id;
				return;
			}
		}

		// Create a new tab for this card
		const newTabId = tabs.createTab();
		tabId = newTabId;

		// If we have a session ID, load it
		if (sessionId) {
			await tabs.loadSession(newTabId, sessionId);
			// Update card title based on session
			const loadedTab = $allTabs.find(t => t.id === newTabId);
			if (loadedTab?.title) {
				workspace.setCardTitle(cardId, loadedTab.title);
			}
		}

		// Connect WebSocket for this tab
		tabs.connect(newTabId);
	});

	onDestroy(() => {
		// Disconnect WebSocket when card is destroyed
		if (tabId) {
			tabs.disconnect(tabId);
		}
	});

	// Auto-scroll to bottom when messages change
	$effect(() => {
		if (currentTab?.messages && messagesContainer) {
			// Small delay to let DOM update
			setTimeout(() => {
				messagesContainer.scrollTop = messagesContainer.scrollHeight;
			}, 10);
		}
	});

	// Handle sending a message
	async function handleSubmit() {
		if (!tabId || !inputValue.trim()) return;

		const message = inputValue.trim();
		inputValue = '';

		await tabs.sendMessage(tabId, message);

		// Update card title if this was the first message
		const updatedTab = $allTabs.find(t => t.id === tabId);
		if (updatedTab?.sessionId && !sessionId) {
			// First message created a session - update dataId and title
			workspace.setCardDataId(cardId, updatedTab.sessionId);
			if (updatedTab.title !== 'New Chat') {
				workspace.setCardTitle(cardId, updatedTab.title);
			}
		}
	}

	// Handle keyboard shortcuts
	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
			e.preventDefault();
			handleSubmit();
		}
	}

	// Simple markdown rendering
	function renderMarkdown(content: string): string {
		return marked.parse(content) as string;
	}

	// Format message role
	function getRoleLabel(role: string): string {
		switch (role) {
			case 'user':
				return 'You';
			case 'assistant':
				return 'Claude';
			case 'system':
				return 'System';
			default:
				return role;
		}
	}
</script>

<div class="chat-card-content">
	{#if currentTab}
		<!-- Messages area -->
		<div class="messages-area" bind:this={messagesContainer}>
			{#if currentTab.messages.length === 0}
				<div class="empty-state">
					<svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
						<path stroke-linecap="round" stroke-linejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
					</svg>
					<p class="empty-text">Start a conversation</p>
					<p class="empty-hint">Type a message below to begin</p>
				</div>
			{:else}
				{#each currentTab.messages as message (message.id)}
					<div class="message {message.role}">
						<div class="message-header">
							<span class="message-role {message.role}">{getRoleLabel(message.role)}</span>
							{#if message.streaming}
								<span class="streaming-indicator"></span>
							{/if}
						</div>
						<div class="message-content prose">
							{#if message.type === 'tool_use'}
								<div class="tool-call">
									<span class="tool-name">{message.toolName}</span>
									{#if message.toolStatus === 'running'}
										<span class="tool-status running">Running...</span>
									{:else if message.toolStatus === 'complete'}
										<span class="tool-status complete">Done</span>
									{:else if message.toolStatus === 'error'}
										<span class="tool-status error">Error</span>
									{/if}
								</div>
							{:else}
								{@html renderMarkdown(message.content)}
							{/if}
						</div>
					</div>
				{/each}
			{/if}
		</div>

		<!-- Input area -->
		<div class="input-area">
			<form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
				<div class="input-wrapper">
					<textarea
						bind:this={inputRef}
						bind:value={inputValue}
						onkeydown={handleKeydown}
						placeholder="Type a message... (Cmd+Enter to send)"
						rows="1"
						disabled={currentTab.isStreaming}
					></textarea>
					<button
						type="submit"
						class="send-button"
						disabled={!inputValue.trim() || currentTab.isStreaming}
					>
						{#if currentTab.isStreaming}
							<svg class="spinner" viewBox="0 0 24 24">
								<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="60" stroke-dashoffset="20" />
							</svg>
						{:else}
							<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
								<path stroke-linecap="round" stroke-linejoin="round" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
							</svg>
						{/if}
					</button>
				</div>
			</form>

			<!-- Status bar -->
			<div class="status-bar">
				{#if currentTab.wsConnected}
					<span class="status connected">Connected</span>
				{:else}
					<span class="status disconnected">Disconnected</span>
				{/if}
				{#if currentTab.profile}
					<span class="status-item">
						{$profiles.find(p => p.id === currentTab.profile)?.name || 'Unknown Profile'}
					</span>
				{/if}
				{#if currentTab.project}
					<span class="status-item">
						{$projects.find(p => p.id === currentTab.project)?.name || 'Unknown Project'}
					</span>
				{/if}
			</div>
		</div>
	{:else}
		<div class="loading">
			<div class="spinner"></div>
			<p>Loading chat...</p>
		</div>
	{/if}
</div>

<style>
	.chat-card-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: #1a1a1a;
	}

	.messages-area {
		flex: 1;
		overflow-y: auto;
		padding: 16px;
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.empty-state {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		color: rgba(255, 255, 255, 0.4);
		text-align: center;
	}

	.empty-icon {
		width: 48px;
		height: 48px;
		margin-bottom: 12px;
		opacity: 0.5;
	}

	.empty-text {
		font-size: 1rem;
		font-weight: 500;
		margin-bottom: 4px;
	}

	.empty-hint {
		font-size: 0.875rem;
		opacity: 0.7;
	}

	.message {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.message-header {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.message-role {
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.message-role.user {
		color: #60a5fa;
	}

	.message-role.assistant {
		color: #fb923c;
	}

	.message-role.system {
		color: #a78bfa;
	}

	.streaming-indicator {
		width: 6px;
		height: 6px;
		background: #fb923c;
		border-radius: 50%;
		animation: pulse 1s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.4; }
	}

	.message-content {
		font-size: 0.9375rem;
		line-height: 1.6;
		color: rgba(255, 255, 255, 0.9);
	}

	.message-content :global(p) {
		margin: 0 0 0.5em;
	}

	.message-content :global(p:last-child) {
		margin-bottom: 0;
	}

	.message-content :global(code) {
		background: rgba(255, 255, 255, 0.1);
		padding: 0.125em 0.375em;
		border-radius: 4px;
		font-size: 0.875em;
	}

	.message-content :global(pre) {
		background: rgba(0, 0, 0, 0.3);
		padding: 12px;
		border-radius: 8px;
		overflow-x: auto;
		margin: 0.5em 0;
	}

	.message-content :global(pre code) {
		background: none;
		padding: 0;
	}

	.tool-call {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 12px;
		background: rgba(255, 255, 255, 0.05);
		border-radius: 6px;
		border: 1px solid rgba(255, 255, 255, 0.1);
	}

	.tool-name {
		font-family: monospace;
		font-size: 0.875rem;
		color: #a78bfa;
	}

	.tool-status {
		font-size: 0.75rem;
		padding: 2px 6px;
		border-radius: 4px;
	}

	.tool-status.running {
		background: rgba(251, 146, 60, 0.2);
		color: #fb923c;
	}

	.tool-status.complete {
		background: rgba(34, 197, 94, 0.2);
		color: #22c55e;
	}

	.tool-status.error {
		background: rgba(239, 68, 68, 0.2);
		color: #ef4444;
	}

	.input-area {
		border-top: 1px solid rgba(255, 255, 255, 0.1);
		padding: 12px;
		background: rgba(0, 0, 0, 0.2);
	}

	.input-wrapper {
		display: flex;
		gap: 8px;
		align-items: flex-end;
	}

	textarea {
		flex: 1;
		resize: none;
		background: rgba(255, 255, 255, 0.05);
		border: 1px solid rgba(255, 255, 255, 0.1);
		border-radius: 8px;
		padding: 10px 12px;
		font-size: 0.9375rem;
		color: white;
		font-family: inherit;
		min-height: 40px;
		max-height: 200px;
	}

	textarea:focus {
		outline: none;
		border-color: rgba(255, 165, 0, 0.5);
	}

	textarea:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	textarea::placeholder {
		color: rgba(255, 255, 255, 0.4);
	}

	.send-button {
		width: 40px;
		height: 40px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: #fb923c;
		border: none;
		border-radius: 8px;
		color: #1a1a1a;
		cursor: pointer;
		transition: background 0.15s ease;
		flex-shrink: 0;
	}

	.send-button:hover:not(:disabled) {
		background: #f97316;
	}

	.send-button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.send-button svg {
		width: 20px;
		height: 20px;
	}

	.send-button .spinner {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	.status-bar {
		display: flex;
		gap: 12px;
		margin-top: 8px;
		font-size: 0.75rem;
		color: rgba(255, 255, 255, 0.5);
	}

	.status {
		display: flex;
		align-items: center;
		gap: 4px;
	}

	.status.connected::before {
		content: '';
		width: 6px;
		height: 6px;
		background: #22c55e;
		border-radius: 50%;
	}

	.status.disconnected::before {
		content: '';
		width: 6px;
		height: 6px;
		background: #ef4444;
		border-radius: 50%;
	}

	.status-item {
		padding-left: 12px;
		border-left: 1px solid rgba(255, 255, 255, 0.1);
	}

	.loading {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		color: rgba(255, 255, 255, 0.5);
	}

	.loading .spinner {
		width: 24px;
		height: 24px;
		border: 2px solid rgba(255, 255, 255, 0.1);
		border-top-color: #fb923c;
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}
</style>

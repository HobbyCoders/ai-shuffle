<script lang="ts">
	/**
	 * MessageArea - Scrollable message container with smart auto-scroll behavior
	 *
	 * Auto-scroll behavior:
	 * - Scrolls to bottom when user is at/near the bottom and new content arrives
	 * - Pauses when user scrolls up to read older messages
	 * - Resumes when user manually scrolls back to the bottom
	 * - Does NOT force scroll when user is reading older messages, even on new messages
	 */
	import { tick } from 'svelte';
	import { tabs, profiles, type ChatTab, type ChatMessage } from '$lib/stores/tabs';
	import UserMessage from './UserMessage.svelte';
	import AssistantMessage from './AssistantMessage.svelte';
	import ToolUseMessage from './ToolUseMessage.svelte';
	import ToolResultMessage from './ToolResultMessage.svelte';
	import SystemMessage from '$lib/components/SystemMessage.svelte';
	import SubagentMessage from '$lib/components/SubagentMessage.svelte';
	import TodoList from '$lib/components/TodoList.svelte';

	interface Props {
		tab: ChatTab;
		onFork?: (sessionId: string, messageIndex: number, messageId: string) => void;
	}

	let { tab, onFork }: Props = $props();

	let containerRef = $state<HTMLDivElement | null>(null);
	let shouldAutoScroll = $state(true); // Start with auto-scroll enabled
	let isScrollingProgrammatically = $state(false);
	let previousTabId = $state<string | null>(null); // Track previous tab to detect actual tab switches
	let savedScrollPositions = $state<Map<string, number>>(new Map()); // Preserve scroll positions per tab

	// Current profile for message settings
	const currentProfile = $derived($profiles.find(p => p.id === tab.profile));
	const hasPartialMessages = $derived(currentProfile?.config?.include_partial_messages !== false);

	// Check if near bottom of scroll container (within threshold)
	function isNearBottom(): boolean {
		if (!containerRef) return true;
		const threshold = 150; // Slightly larger threshold for better UX
		return containerRef.scrollHeight - containerRef.scrollTop - containerRef.clientHeight < threshold;
	}

	function scrollToBottom() {
		if (containerRef) {
			isScrollingProgrammatically = true;
			containerRef.scrollTop = containerRef.scrollHeight;
			// Reset flag after scroll animation completes
			requestAnimationFrame(() => {
				requestAnimationFrame(() => {
					isScrollingProgrammatically = false;
				});
			});
		}
	}

	function handleScroll() {
		if (!containerRef || isScrollingProgrammatically) return;

		// Check current position
		const nearBottom = isNearBottom();

		if (nearBottom) {
			// User is at/near the bottom - enable auto-scroll
			shouldAutoScroll = true;
		} else {
			// User scrolled away from bottom - disable auto-scroll
			// This allows reading older messages without being pulled back
			shouldAutoScroll = false;
		}
	}

	// Track previous values to detect actual content changes (not just re-renders)
	let prevMessagesLength = $state(0);
	let prevLastContent = $state('');

	// Get last message content for streaming detection
	const lastMessageContent = $derived(() => {
		const lastMsg = tab.messages[tab.messages.length - 1];
		return lastMsg?.content || '';
	});

	// Watch for message changes and streaming content updates
	$effect(() => {
		// Get current values
		const currentLength = tab.messages.length;
		const currentContent = lastMessageContent();

		// Only scroll if there's an actual change (new message or content update)
		const hasNewMessage = currentLength !== prevMessagesLength;
		const hasContentChange = currentContent !== prevLastContent;

		if ((hasNewMessage || hasContentChange) && shouldAutoScroll && containerRef) {
			tick().then(scrollToBottom);
		}

		// Update previous values
		prevMessagesLength = currentLength;
		prevLastContent = currentContent;
	});

	// Scroll on tab change - only scroll to bottom on ACTUAL tab switches, not remounts
	$effect(() => {
		const currentTabId = tab.id;

		// Check if this is an actual tab switch (different tab) vs a remount of the same tab
		if (previousTabId !== null && previousTabId !== currentTabId) {
			// Actual tab switch: save old position, restore new position or scroll to bottom
			if (containerRef && previousTabId) {
				savedScrollPositions.set(previousTabId, containerRef.scrollTop);
			}

			const savedPosition = savedScrollPositions.get(currentTabId);
			if (savedPosition !== undefined && containerRef) {
				// Restore saved scroll position for this tab
				tick().then(() => {
					if (containerRef) {
						isScrollingProgrammatically = true;
						containerRef.scrollTop = savedPosition;
						// Check if we're near bottom to set auto-scroll appropriately
						shouldAutoScroll = isNearBottom();
						requestAnimationFrame(() => {
							isScrollingProgrammatically = false;
						});
					}
				});
			} else {
				// New tab or no saved position: scroll to bottom
				shouldAutoScroll = true;
				tick().then(scrollToBottom);
			}
		} else if (previousTabId === null) {
			// Initial mount: scroll to bottom for new conversations, but don't force it
			// This prevents scroll reset on component remount during card transforms
			tick().then(() => {
				if (containerRef && containerRef.scrollTop === 0 && tab.messages.length > 0) {
					// Only scroll to bottom if we're at the top with messages (likely initial load)
					scrollToBottom();
				}
			});
		}
		// If same tab remounting (previousTabId === currentTabId), don't change scroll position

		previousTabId = currentTabId;
	});
</script>

<div
	bind:this={containerRef}
	onscroll={handleScroll}
	class="flex-1 overflow-y-auto"
>
	{#if tab.messages.length === 0}
		<!-- Empty State -->
		<div class="h-full flex items-center justify-center">
			<div class="text-center max-w-md px-6">
				<div class="w-16 h-16 mx-auto mb-6 rounded-2xl bg-primary/10 flex items-center justify-center shadow-s">
					<svg class="w-8 h-8 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="1.5"
							d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
						/>
					</svg>
				</div>
				<h2 class="text-xl font-semibold text-foreground mb-2">Start a Conversation</h2>
				<p class="text-muted-foreground mb-4">Ask Claude anything - code, questions, ideas, or just chat.</p>
				{#if currentProfile}
					<div class="text-sm text-muted-foreground">
						<span>Profile:</span>
						<span class="text-foreground ml-1">{currentProfile.name}</span>
					</div>
				{/if}
			</div>
		</div>
	{:else}
		<!-- Messages -->
		<div class="max-w-5xl mx-auto px-4 sm:px-8 py-4 space-y-4">
			{#each tab.messages as message, messageIndex (message.id)}
				{#if message.role === 'user'}
					<UserMessage content={message.content} />
				{:else if message.type === 'text' || !message.type}
					<AssistantMessage
						content={message.content}
						messageIndex={messageIndex}
						messageId={message.id}
						sessionId={tab.sessionId}
						streaming={message.streaming}
						{hasPartialMessages}
						metadata={message.metadata}
						onFork={onFork}
					/>
				{:else if message.type === 'tool_use' && message.toolName === 'TodoWrite'}
					<!-- TodoWrite - Render as TodoList component -->
					{#if message.toolInput?.todos && Array.isArray(message.toolInput.todos)}
						<div class="w-full">
							<div class="min-w-0">
								<TodoList todos={message.toolInput.todos} />
							</div>
						</div>
					{/if}
				{:else if message.type === 'tool_use'}
					<ToolUseMessage
						name={message.toolName || 'Tool'}
						input={message.toolInput}
						partialInput={message.partialToolInput}
						result={message.toolResult}
						status={message.toolStatus || 'running'}
						streaming={message.streaming}
					/>
				{:else if message.type === 'tool_result'}
					<ToolResultMessage
						content={message.content}
						toolId={message.toolId}
					/>
				{:else if message.type === 'system'}
					<div class="w-full">
						<div class="min-w-0">
							<SystemMessage {message} />
						</div>
					</div>
				{:else if message.type === 'subagent'}
					<div class="w-full">
						<div class="min-w-0">
							<SubagentMessage {message} />
						</div>
					</div>
				{/if}
			{/each}

			<!-- Error -->
			{#if tab.error}
				<div class="bg-destructive/10 border border-destructive/30 text-destructive px-4 py-3 rounded-lg flex items-center justify-between shadow-s">
					<span class="text-sm">{tab.error}</span>
					<button onclick={() => tabs.clearTabError(tab.id)} class="text-destructive hover:opacity-80">&times;</button>
				</div>
			{/if}
		</div>
	{/if}
</div>

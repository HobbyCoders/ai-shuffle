<script lang="ts">
	/**
	 * MessageArea - Scrollable message container with smart auto-scroll behavior
	 *
	 * Auto-scroll behavior:
	 * - Scrolls to bottom when user is at/near the bottom and new content arrives
	 * - Pauses when user scrolls up to read older messages
	 * - Resumes when user manually scrolls back to the bottom
	 * - Does NOT force scroll when user is reading older messages, even on new messages
	 *
	 * Scroll persistence:
	 * - Uses a non-reactive Map to avoid triggering effects when storing scroll position
	 * - Scroll position is restored on component mount (not on every tab prop change)
	 * - This prevents scroll jumping when switching between cards
	 */
	import { tick, onMount, onDestroy } from 'svelte';
	import { tabs, profiles, type ChatTab, type ChatMessage, type TodoItem } from '$lib/stores/tabs';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences';
	import UserMessage from './UserMessage.svelte';
	import AssistantMessage from './AssistantMessage.svelte';
	import ToolUseMessage from './ToolUseMessage.svelte';
	import ThinkingBlock from './ThinkingBlock.svelte';
	// Note: ToolResultMessage is intentionally not imported - tool_result messages are
	// always merged into tool_use messages to prevent flickering during streaming
	import SystemMessage from '$lib/components/SystemMessage.svelte';
	import SubagentMessage from '$lib/components/SubagentMessage.svelte';
	import TodoList from '$lib/components/TodoList.svelte';

	// Non-reactive scroll position cache to avoid triggering effects
	// This is module-level so it persists across component instances
	const scrollPositionCache = new Map<string, number>();

	interface Props {
		tab: ChatTab;
	}

	let { tab }: Props = $props();

	/**
	 * Reload the session after a rewind completes.
	 * This is a backup mechanism in case the WebSocket event doesn't trigger a refresh.
	 */
	function handleRewindComplete() {
		console.log('[MessageArea] Rewind complete, reloading session');
		if (tab.sessionId) {
			tabs.loadSessionInTab(tab.id, tab.sessionId);
		}
	}

	let containerRef = $state<HTMLDivElement | null>(null);
	let shouldAutoScroll = $state(true); // Start with auto-scroll enabled
	let isScrollingProgrammatically = $state(false);
	let isMounted = $state(false);

	// Current profile for message settings (uses current UI selection)
	const currentProfile = $derived($profiles.find(p => p.id === tab.profile));
	const hasPartialMessages = $derived(currentProfile?.config?.include_partial_messages !== false);
	const fileCheckpointingEnabled = $derived(currentProfile?.config?.enable_file_checkpointing === true);

	// Session profile for fallback display (legacy messages without profileId)
	// Falls back to current profile for new sessions before first message is sent
	const sessionProfile = $derived($profiles.find(p => p.id === (tab.sessionProfileId || tab.profile)));

	/**
	 * Get the profile name for a specific message.
	 * Prioritizes the message's own profileId, then falls back to session profile.
	 * This enables per-query profile tracking while supporting legacy messages.
	 */
	function getProfileName(message: ChatMessage): string {
		// First check if the message has its own profileId
		if (message.profileId) {
			const profile = $profiles.find(p => p.id === message.profileId);
			if (profile) return profile.name;
		}
		// Fallback to session profile for legacy messages
		return sessionProfile?.name ?? 'Assistant';
	}

	// Chat preferences (reactive subscription)
	let chatPrefs = $state<ChatPreferences | null>(null);
	$effect(() => {
		const unsubscribe = chatPreferences.subscribe(p => {
			chatPrefs = p;
		});
		return unsubscribe;
	});

	// Find the last streaming message index - only that message should show the streaming indicator
	// This prevents multiple bouncing dots from appearing after each tool call
	const lastStreamingMessageIndex = $derived(() => {
		for (let i = tab.messages.length - 1; i >= 0; i--) {
			if (tab.messages[i].streaming) {
				return i;
			}
		}
		return -1;
	});

	/**
	 * Extract todos from a TodoWrite message, handling both complete and partial input.
	 * During streaming, the toolInput might not be fully parsed yet, so we also try
	 * to extract from partialToolInput.
	 */
	function extractTodosFromMessage(message: ChatMessage): TodoItem[] | null {
		// Helper to validate and cast status
		const isValidStatus = (s: string): s is TodoItem['status'] =>
			s === 'pending' || s === 'in_progress' || s === 'completed';

		// First, try the complete toolInput
		if (message.toolInput?.todos && Array.isArray(message.toolInput.todos)) {
			return message.toolInput.todos as TodoItem[];
		}

		// During streaming, try to extract from partialToolInput
		if (message.streaming && message.partialToolInput) {
			try {
				// Try to parse the partial JSON
				const parsed = JSON.parse(message.partialToolInput);
				if (parsed?.todos && Array.isArray(parsed.todos)) {
					return parsed.todos as TodoItem[];
				}
			} catch {
				// Partial JSON not yet complete - try to extract todos array using regex
				// Look for "todos": [ followed by todo objects
				const todosMatch = message.partialToolInput.match(/"todos"\s*:\s*\[/);
				if (todosMatch) {
					// Find the start of the array
					const arrayStart = message.partialToolInput.indexOf('[', todosMatch.index);
					if (arrayStart !== -1) {
						// Try to extract any complete todo objects
						// This regex finds complete todo objects with content, status, and activeForm
						const todoRegex = /\{\s*"content"\s*:\s*"([^"]*)"[^}]*"status"\s*:\s*"([^"]*)"[^}]*"activeForm"\s*:\s*"([^"]*)"\s*\}/g;
						const todos: TodoItem[] = [];
						let match;
						const searchStr = message.partialToolInput.slice(arrayStart);
						while ((match = todoRegex.exec(searchStr)) !== null) {
							const status = match[2];
							todos.push({
								content: match[1],
								status: isValidStatus(status) ? status : 'pending',
								activeForm: match[3]
							});
						}
						if (todos.length > 0) {
							return todos;
						}
					}
				}
			}
		}

		return null;
	}

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
			// Use double RAF to ensure scroll events from this programmatic scroll are ignored
			// Single RAF isn't enough - browser may fire scroll events across multiple frames
			requestAnimationFrame(() => {
				requestAnimationFrame(() => {
					isScrollingProgrammatically = false;
				});
			});
		}
	}

	function handleScroll() {
		if (!containerRef || isScrollingProgrammatically) return;

		// Save scroll position to non-reactive cache (doesn't trigger effects)
		scrollPositionCache.set(tab.id, containerRef.scrollTop);
		// Also save to tab store for cross-session persistence
		tabs.setTabScrollTop(tab.id, containerRef.scrollTop);

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

	// Restore scroll position only on mount - not on every reactive update
	onMount(() => {
		isMounted = true;
		// Wait for DOM to be ready
		requestAnimationFrame(() => {
			if (!containerRef) return;

			// Check non-reactive cache first (fastest, for switching between cards)
			const cachedPosition = scrollPositionCache.get(tab.id);
			// Fall back to tab store (for page reload persistence)
			const storedPosition = cachedPosition ?? tab.scrollTop;

			if (storedPosition > 0) {
				isScrollingProgrammatically = true;
				containerRef.scrollTop = storedPosition;
				shouldAutoScroll = isNearBottom();
				requestAnimationFrame(() => {
					isScrollingProgrammatically = false;
				});
			} else if (tab.messages.length > 0) {
				// New tab with messages but no saved position - scroll to bottom
				scrollToBottom();
			}
		});
	});

	// Save scroll position when component is destroyed (critical for card switching)
	onDestroy(() => {
		if (containerRef) {
			scrollPositionCache.set(tab.id, containerRef.scrollTop);
			tabs.setTabScrollTop(tab.id, containerRef.scrollTop);
		}
	});

	// Track previous values to detect actual content changes (not just re-renders)
	let prevMessagesLength = $state(0);
	let prevLastContent = $state('');
	let prevLastMetadata = $state<string | null>(null);

	// Get last message content for streaming detection (includes toolInput for TodoWrite)
	const lastMessageContent = $derived(() => {
		const lastMsg = tab.messages[tab.messages.length - 1];
		if (!lastMsg) return '';
		// For tool_use messages, track toolInput changes (especially TodoWrite)
		if (lastMsg.type === 'tool_use' && lastMsg.toolInput) {
			return JSON.stringify(lastMsg.toolInput);
		}
		return lastMsg.content || '';
	});

	// Get last message metadata for tracking changes (cost/duration appear here)
	const lastMessageMetadata = $derived(() => {
		const lastMsg = tab.messages[tab.messages.length - 1];
		if (!lastMsg?.metadata) return null;
		// Only track specific fields that affect rendering
		const m = lastMsg.metadata as Record<string, unknown>;
		return JSON.stringify({
			cost: m.cost_usd ?? m.total_cost_usd,
			duration: m.duration_ms
		});
	});

	// Watch for message changes and streaming content updates
	$effect(() => {
		// Only run after mount and when there's actual content change
		if (!isMounted) return;

		// Get current values
		const currentLength = tab.messages.length;
		const currentContent = lastMessageContent();
		const currentMetadata = lastMessageMetadata();

		// Only scroll if there's an actual change (new message, content update, or metadata added)
		const hasNewMessage = currentLength !== prevMessagesLength;
		const hasContentChange = currentContent !== prevLastContent;
		const hasMetadataChange = currentMetadata !== prevLastMetadata;

		if ((hasNewMessage || hasContentChange || hasMetadataChange) && shouldAutoScroll && containerRef) {
			tick().then(scrollToBottom);
		}

		// Update previous values
		prevMessagesLength = currentLength;
		prevLastContent = currentContent;
		prevLastMetadata = currentMetadata;
	});
</script>

<div
	bind:this={containerRef}
	onscroll={handleScroll}
	class="flex-1 overflow-y-auto overscroll-contain"
>
	{#if tab.messages.length === 0}
		<!-- Empty State -->
		<div class="h-full flex flex-col">
			<!-- Error display for empty state (e.g., worktree creation failures) -->
			{#if tab.error}
				<div class="max-w-5xl mx-auto w-full px-4 sm:px-8 pt-4">
					<div class="bg-destructive/10 border border-destructive/30 text-destructive px-4 py-3 rounded-lg flex items-center justify-between shadow-s">
						<span class="text-sm">{tab.error}</span>
						<button onclick={() => tabs.clearTabError(tab.id)} class="text-destructive hover:opacity-80">&times;</button>
					</div>
				</div>
			{/if}
			<div class="flex-1 flex items-center justify-center">
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
					<p class="text-muted-foreground mb-4">Ask anything - code, questions, ideas, or just chat.</p>
					{#if currentProfile}
						<div class="text-sm text-muted-foreground">
							<span>Profile:</span>
							<span class="text-foreground ml-1">{currentProfile.name}</span>
						</div>
					{/if}
				</div>
			</div>
		</div>
	{:else}
		<!-- Messages -->
		<div class="max-w-5xl mx-auto px-4 sm:px-8 py-4 space-y-4">
			{#each tab.messages as message, messageIndex (message.id)}
				{#if message.role === 'user'}
					<UserMessage
						content={message.content}
						sessionId={tab.sessionId}
						messageUuid={message.jsonlUuid || message.id}
						{fileCheckpointingEnabled}
						onRewindComplete={handleRewindComplete}
					/>
				{:else if message.type === 'thinking' && chatPrefs?.showThinkingBlocks !== false}
					<!-- Thinking block - shown based on user preference -->
					<ThinkingBlock
						content={message.content}
						streaming={message.streaming}
						profileName={getProfileName(message)}
					/>
				{:else if message.type === 'text' || !message.type}
					<AssistantMessage
						content={message.content}
						messageIndex={messageIndex}
						messageId={message.id}
						messageUuid={message.jsonlUuid || message.id}
						sessionId={tab.sessionId}
						streaming={message.streaming && messageIndex === lastStreamingMessageIndex()}
						{hasPartialMessages}
						{fileCheckpointingEnabled}
						metadata={message.metadata}
						onRewindComplete={handleRewindComplete}
						profileName={getProfileName(message)}
					/>
				{:else if message.type === 'tool_use' && message.toolName === 'TodoWrite'}
					<!-- TodoWrite - Render as TodoList component (also handles streaming partial input) -->
					{@const todos = extractTodosFromMessage(message)}
					{#if todos && todos.length > 0}
						<div class="w-full">
							<div class="min-w-0">
								<TodoList {todos} />
							</div>
						</div>
					{/if}
				{:else if message.type === 'tool_use' && message.toolName === 'Task'}
					<!-- Task tool_use - Skip rendering if a linked subagent message exists -->
					<!-- The SubagentMessage component handles displaying the agent's work -->
					{@const hasLinkedSubagent = tab.messages.some(m => m.type === 'subagent' && m.toolId === message.toolId)}
					{#if !hasLinkedSubagent}
						<ToolUseMessage
							name={message.toolName || 'Tool'}
							input={message.toolInput}
							partialInput={message.partialToolInput}
							result={message.toolResult}
							status={message.toolStatus || 'running'}
							streaming={message.streaming}
						/>
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
					<!-- Skip standalone tool_result messages - they should be merged into tool_use -->
					<!-- This prevents flickering where result briefly appears separately -->
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

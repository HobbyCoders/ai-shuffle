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
	import { tick, untrack, onMount, onDestroy } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { profiles } from '$lib/stores/app-data';
	import type { ChatMessage, TodoItem } from '$lib/stores/types';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences';
	import { messageCache } from '$lib/stores/message-cache';
	import UserMessage from './UserMessage.svelte';
	import AssistantMessage from './AssistantMessage.svelte';
	import ToolUseMessage from './ToolUseMessage.svelte';
	import ThinkingBlock from './ThinkingBlock.svelte';
	import CommandMessage from './CommandMessage.svelte';
	// Note: ToolResultMessage is intentionally not imported - tool_result messages are
	// always merged into tool_use messages to prevent flickering during streaming
	import SystemMessage from '$lib/components/SystemMessage.svelte';
	import SubagentMessage from '$lib/components/SubagentMessage.svelte';
	import TodoList from '$lib/components/TodoList.svelte';

	// Non-reactive scroll position cache to avoid triggering effects
	// This is module-level so it persists across component instances
	const scrollPositionCache = new Map<string, number>();
	// Persist auto-scroll state across component mount/destroy cycles (e.g.
	// navigating away from chat to settings and returning, or transitioning
	// between empty and active states) so the component scrolls to bottom
	// instead of restoring a now-stale scroll position
	const autoScrollCache = new Map<string, boolean>();

	/**
	 * Reload the session after a rewind completes.
	 * Clears stale messages and cache first so the reload shows the correct
	 * post-rewind state (which may be empty if all messages were rewound).
	 * @param warning - Optional warning message for partial success (e.g., file restore failed)
	 */
	function handleRewindComplete(warning?: string) {
		// If there's a warning (partial success), show it as a system message
		if (warning) {
			activeSession.addSystemMessage(warning, 'rewind_warning');
		}

		// Clear stale messages from the store and IndexedDB cache.
		// Without this, the defensive check in the history handler would
		// preserve old (broken) messages when the reload returns fewer/zero.
		const sessionId = $activeSession.sessionId;
		activeSession.clearMessages();
		if (sessionId) {
			messageCache.delete(sessionId);
		}

		activeSession.reloadSession(true);
	}

	let containerRef = $state<HTMLDivElement | null>(null);
	let shouldAutoScroll = $state(true); // Start with auto-scroll enabled
	let isScrollingProgrammatically = $state(false);
	let isMounted = $state(false);
	let scrollRafPending = false; // Throttle scroll handler to one per frame

	// Current profile for message settings (uses current UI selection)
	const currentProfile = $derived($profiles.find(p => p.id === $activeSession.profile));
	const hasPartialMessages = $derived(currentProfile?.config?.include_partial_messages !== false);
	const fileCheckpointingEnabled = $derived(currentProfile?.config?.enable_file_checkpointing === true);

	// Session profile for fallback display (legacy messages without profileId)
	// Falls back to current profile for new sessions before first message is sent
	const sessionProfile = $derived($profiles.find(p => p.id === ($activeSession.sessionProfileId || $activeSession.profile)));

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
		for (let i = $activeSession.messages.length - 1; i >= 0; i--) {
			if ($activeSession.messages[i].streaming) {
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

		// Throttle to one handler per animation frame to prevent main thread blocking
		if (scrollRafPending) return;
		scrollRafPending = true;
		requestAnimationFrame(() => {
			scrollRafPending = false;
			if (!containerRef) return;

			const cacheKey = $activeSession.sessionId ?? 'new';

			// Save scroll position to non-reactive cache (doesn't trigger effects)
			scrollPositionCache.set(cacheKey, containerRef.scrollTop);
			// Also save to session store for cross-session persistence
			activeSession.setScrollTop(containerRef.scrollTop);

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
		});
	}

	// Restore scroll position only on mount - not on every reactive update
	onMount(() => {
		isMounted = true;
		// Wait for DOM to be ready
		requestAnimationFrame(() => {
			if (!containerRef) return;

			const cacheKey = $activeSession.sessionId ?? 'new';

			// Check if user had explicitly scrolled up before this component
			// was destroyed. autoScrollCache tracks shouldAutoScroll state:
			//   true      = was at/near bottom (auto-scroll active)
			//   false     = user had scrolled up (auto-scroll paused)
			//   undefined = no prior state (first mount)
			// Default to scrolling to bottom for true and undefined.
			const wasAutoScrolling = autoScrollCache.get(cacheKey);
			const userScrolledUp = wasAutoScrolling === false;

			if (userScrolledUp) {
				// User intentionally scrolled up - restore their position
				const cachedPosition = scrollPositionCache.get(cacheKey);
				const storedPosition = cachedPosition ?? $activeSession.scrollTop;

				if (storedPosition > 0) {
					isScrollingProgrammatically = true;
					containerRef.scrollTop = storedPosition;
					shouldAutoScroll = false;
					requestAnimationFrame(() => {
						isScrollingProgrammatically = false;
					});
				} else {
					// No saved position even though they scrolled up - scroll to bottom
					shouldAutoScroll = true;
					scrollToBottom();
				}
			} else if ($activeSession.messages.length > 0) {
				// Auto-scroll was on, or first visit - always show latest content
				shouldAutoScroll = true;
				scrollToBottom();
			}
		});
	});

	// Save scroll position when component is destroyed (critical for card switching)
	let resizeObserver: ResizeObserver | null = null;

	// Handle visibility change (mobile: minimize Chrome and return)
	// When the page becomes visible again, if auto-scroll was enabled, re-scroll to bottom.
	// Mobile browsers often restore a stale scroll position from their history state,
	// causing the chat to appear scrolled to the middle instead of the bottom.
	function handleVisibilityChange() {
		if (document.visibilityState === 'visible' && shouldAutoScroll && containerRef) {
			// Use double RAF to ensure browser has finished any scroll restoration
			requestAnimationFrame(() => {
				requestAnimationFrame(() => {
					scrollToBottom();
				});
			});
		}
	}

	// Track visibility listener for cleanup
	let visibilityListenerAdded = false;

	$effect(() => {
		if (!isMounted) return;

		if (!visibilityListenerAdded) {
			document.addEventListener('visibilitychange', handleVisibilityChange);
			visibilityListenerAdded = true;
		}

		return () => {
			if (visibilityListenerAdded) {
				document.removeEventListener('visibilitychange', handleVisibilityChange);
				visibilityListenerAdded = false;
			}
		};
	});

	onDestroy(() => {
		const cacheKey = $activeSession.sessionId ?? 'new';

		if (containerRef) {
			scrollPositionCache.set(cacheKey, containerRef.scrollTop);
			activeSession.setScrollTop(containerRef.scrollTop);
			// Persist auto-scroll state so remounting this component
			// (e.g. returning from settings) resumes at the right position
			autoScrollCache.set(cacheKey, shouldAutoScroll);
		}
		// Clean up resize observer
		if (resizeObserver) {
			resizeObserver.disconnect();
			resizeObserver = null;
		}
	});

	// ── Session-switch scroll save/restore ─────────────────────────────
	// Replaces the old {#key $activeSession.sessionId} pattern in ChatView
	// that destroyed/recreated this component on every sessionId change.
	// That pattern caused a Svelte 5 reactivity edge case where the
	// recreated component lost reactive store updates during new session
	// creation (sessionId: undefined → new ID).
	let prevTrackedSessionId = $state<string | null | undefined>(undefined);

	$effect(() => {
		const currentId = $activeSession.sessionId;
		const prevId = untrack(() => prevTrackedSessionId);

		if (currentId === prevId) return;

		// Save scroll state for the old session (if any)
		if (prevId && containerRef) {
			scrollPositionCache.set(prevId, containerRef.scrollTop);
			autoScrollCache.set(prevId, shouldAutoScroll);
			activeSession.setScrollTop(containerRef.scrollTop);
		}

		prevTrackedSessionId = currentId;

		// Skip scroll restore for initial sessionId assignment on new sessions
		// (undefined → new ID). The component is already at the correct position
		// (bottom) from onMount, and restoring would fight the initial render.
		if (!prevId) return;

		// Restore scroll state for the new session
		const cacheKey = currentId ?? 'new';
		const wasAutoScrolling = autoScrollCache.get(cacheKey);
		const userScrolledUp = wasAutoScrolling === false;

		// Wait for DOM to update with new messages before restoring scroll
		tick().then(() => {
			if (!containerRef) return;

			if (userScrolledUp) {
				const cachedPosition = scrollPositionCache.get(cacheKey);
				const storedPosition = cachedPosition ?? $activeSession.scrollTop;

				if (storedPosition > 0) {
					isScrollingProgrammatically = true;
					containerRef.scrollTop = storedPosition;
					shouldAutoScroll = false;
					requestAnimationFrame(() => {
						isScrollingProgrammatically = false;
					});
				} else {
					shouldAutoScroll = true;
					scrollToBottom();
				}
			} else {
				// Default: scroll to bottom for the new session
				shouldAutoScroll = true;
				scrollToBottom();
			}
		});
	});

	// Observe container size changes (e.g., when input area expands/contracts)
	// This maintains auto-scroll when the chat input island grows, which would
	// otherwise change the scrollbar and break the "near bottom" detection
	$effect(() => {
		if (!containerRef || !isMounted) return;

		// Clean up previous observer
		if (resizeObserver) {
			resizeObserver.disconnect();
		}

		resizeObserver = new ResizeObserver(() => {
			// If auto-scroll is enabled and we're supposed to be at bottom,
			// ensure we stay at bottom after resize
			if (shouldAutoScroll && containerRef) {
				// Use requestAnimationFrame to ensure layout is complete
				requestAnimationFrame(() => {
					scrollToBottom();
				});
			}
		});

		resizeObserver.observe(containerRef);

		return () => {
			if (resizeObserver) {
				resizeObserver.disconnect();
			}
		};
	});

	// Track previous values to detect actual content changes (not just re-renders)
	let prevMessagesLength = $state(0);
	let prevLastContent = $state('');
	let prevLastMetadata = $state<string | null>(null);

	// Get last message content for streaming detection (includes toolInput for TodoWrite)
	const lastMessageContent = $derived(() => {
		const lastMsg = $activeSession.messages[$activeSession.messages.length - 1];
		if (!lastMsg) return '';
		// For tool_use messages, track toolInput changes (especially TodoWrite)
		if (lastMsg.type === 'tool_use' && lastMsg.toolInput) {
			return JSON.stringify(lastMsg.toolInput);
		}
		return lastMsg.content || '';
	});

	// Get metadata for the last assistant message that has metadata (cost/duration
	// rendered there). This message may not be the absolute last in the array
	// (e.g. a TodoWrite tool_use could follow it).
	const lastMessageMetadata = $derived(() => {
		const msg = $activeSession.messages.findLast(m => m.role === 'assistant' && m.metadata);
		if (!msg?.metadata) return null;
		const m = msg.metadata as Record<string, unknown>;
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
		const currentLength = $activeSession.messages.length;
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
	{#if $activeSession.messages.length === 0}
		<!-- Empty State -->
		<div class="h-full flex flex-col">
			<!-- Error display for empty state -->
			{#if $activeSession.error}
				<div class="max-w-5xl mx-auto w-full px-4 sm:px-8 pt-4">
					<div class="bg-destructive/10 border border-destructive/30 text-destructive px-4 py-3 rounded-lg flex items-center justify-between shadow-s">
						<span class="text-sm">{$activeSession.error}</span>
						<button onclick={() => activeSession.clearError()} class="text-destructive hover:opacity-80">&times;</button>
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
			{#each $activeSession.messages as message, messageIndex (message.id)}
				<!-- content-visibility: auto lets the browser skip paint/layout for off-screen messages -->
				<div class="msg-virtualized">
				{#if message.type === 'command'}
					<!-- Slash command invocation (e.g., /commit, /ralph-loop) -->
					<CommandMessage
						commandName={message.commandName || message.content}
						commandArgs={message.commandArgs}
					/>
				{:else if message.role === 'user'}
					<UserMessage
						content={message.content}
						sessionId={$activeSession.sessionId}
						messageUuid={message.jsonlUuid}
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
						messageUuid={message.jsonlUuid}
						sessionId={$activeSession.sessionId}
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
					{@const hasLinkedSubagent = $activeSession.messages.some(m => m.type === 'subagent' && m.toolId === message.toolId)}
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
			</div>
			{/each}

			<!-- Error -->
			{#if $activeSession.error}
				<div class="bg-destructive/10 border border-destructive/30 text-destructive px-4 py-3 rounded-lg flex items-center justify-between shadow-s">
					<span class="text-sm">{$activeSession.error}</span>
					<button onclick={() => activeSession.clearError()} class="text-destructive hover:opacity-80">&times;</button>
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	/* Browser-native virtualization: skip paint/layout for off-screen messages.
	   `auto` in contain-intrinsic-size remembers the last rendered height so
	   the scrollbar stays stable when scrolling back through history. */
	.msg-virtualized {
		content-visibility: auto;
		contain-intrinsic-size: auto 80px;
	}
</style>

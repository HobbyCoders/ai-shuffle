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
	import { profiles } from '$lib/stores/app-data.svelte';
	import type { ChatMessage, TodoItem } from '$lib/stores/types';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { messageCache } from '$lib/stores/message-cache';
	import { STREAM_DEBUG } from '$lib/utils/debug-log';
	import UserMessage from './UserMessage.svelte';
	import AssistantMessage from './AssistantMessage.svelte';
	import StreamingIndicator from './StreamingIndicator.svelte';
	import { deriveIndicatorState, waitingLabel } from '$lib/stores/session/indicator-state';
	import ToolUseMessage from './ToolUseMessage.svelte';
	import ToolUseGroup from './ToolUseGroup.svelte';
	import SystemMessageGroup from './SystemMessageGroup.svelte';
	import ThinkingBlock from './ThinkingBlock.svelte';
	import CommandMessage from './CommandMessage.svelte';
	// Note: ToolResultMessage is intentionally not imported - tool_result messages are
	// always merged into tool_use messages to prevent flickering during streaming
	import SystemMessage from '$lib/components/SystemMessage.svelte';
	import SubagentMessage from '$lib/components/SubagentMessage.svelte';
	import TeamMemberMessage from '$lib/components/TeamMemberMessage.svelte';
	import TeamStatusBar from '$lib/components/TeamStatusBar.svelte';
	import TodoList from '$lib/components/TodoList.svelte';

	// Non-reactive scroll position cache to avoid triggering effects.
	// Module-level so it persists across component instances.
	// Capped at 100 entries to prevent unbounded growth over long sessions.
	const SCROLL_CACHE_MAX = 100;
	const scrollPositionCache = new Map<string, number>();
	// Persist auto-scroll state across component mount/destroy cycles
	const autoScrollCache = new Map<string, boolean>();

	function evictOldest(map: Map<string, unknown>, max: number) {
		if (map.size <= max) return;
		const it = map.keys();
		let excess = map.size - max;
		while (excess-- > 0) {
			const { value, done } = it.next();
			if (done) break;
			map.delete(value);
		}
	}

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
	// Counter (not boolean) to handle overlapping programmatic scrolls correctly.
	// Multiple concurrent scrollToBottom() calls (ResizeObserver + content effect +
	// scrollToBottomUntilStable) each increment on entry and decrement after double-RAF.
	// handleScroll ignores events while any programmatic scroll is in-flight.
	let programmaticScrollDepth = 0;
	let isMounted = $state(false);
	let scrollRafPending = false; // Throttle scroll handler to one per frame
	let lastKnownScrollTop = 0; // For scroll-direction detection
	// Set on session switch — ensures scroll-to-bottom fires after messages load,
	// even if intermediate DOM changes set shouldAutoScroll to false.
	let pendingScrollToBottom = false;
	let pendingScrollTimer: ReturnType<typeof setInterval> | null = null;

	/**
	 * Aggressively scroll to bottom over multiple frames until stable.
	 * Large messages (tables, code blocks) may take several frames to fully render,
	 * so a single scrollToBottom() call isn't enough.
	 */
	function scrollToBottomUntilStable() {
		if (pendingScrollTimer) clearInterval(pendingScrollTimer);
		let lastScrollHeight = 0;
		let stableCount = 0;
		let tick = 0;
		// eslint-disable-next-line no-console
		if (STREAM_DEBUG) console.log(`[AS] stable START initSH=${containerRef?.scrollHeight} initST=${containerRef?.scrollTop} msgLen=${$activeSession.messages.length}`);
		pendingScrollTimer = setInterval(() => {
			if (!containerRef) {
				if (pendingScrollTimer) { clearInterval(pendingScrollTimer); pendingScrollTimer = null; }
				return;
			}
			tick++;
			const shBefore = containerRef.scrollHeight;
			scrollToBottom();
			const shAfter = containerRef.scrollHeight;
			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] stable tick=${tick} shBefore=${shBefore} shAfter=${shAfter} ST=${containerRef.scrollTop} CH=${containerRef.clientHeight} stable=${stableCount} msgLen=${$activeSession.messages.length}`);
			if (containerRef.scrollHeight === lastScrollHeight) {
				stableCount++;
				// Require 5 stable ticks (~250ms) before declaring done. Large content
				// (tables, code blocks, markdown trees) can render in bursts, so brief
				// stability does not mean the layout has settled.
				if (stableCount >= 5) {
					// eslint-disable-next-line no-console
					if (STREAM_DEBUG) console.log(`[AS] stable DONE finalSH=${containerRef.scrollHeight} finalST=${containerRef.scrollTop} tick=${tick}`);
					if (pendingScrollTimer) { clearInterval(pendingScrollTimer); pendingScrollTimer = null; }
				}
			} else {
				stableCount = 0;
				lastScrollHeight = containerRef.scrollHeight;
			}
		}, 50);
		// Safety: stop after 3s regardless. Prior 1s was too aggressive — on large
		// sessions the scrollHeight kept growing past the 1s mark (e.g. 18k → 30k px),
		// leaving the viewport anchored at stale content.
		setTimeout(() => {
			if (pendingScrollTimer) {
				// eslint-disable-next-line no-console
				if (STREAM_DEBUG) console.log('[AS] stable TIMEOUT (3s)', { finalSH: containerRef?.scrollHeight, finalST: containerRef?.scrollTop });
				clearInterval(pendingScrollTimer); pendingScrollTimer = null;
			}
		}, 3000);
	}

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

	// Chat preferences (reactive via .current getter)
	const chatPrefs = $derived(chatPreferences.current);

	// Find the last streaming message index - only that message should show the streaming indicator
	// This prevents multiple bouncing dots from appearing after each tool call
	const lastStreamingMessageIndex = $derived.by(() => {
		for (let i = $activeSession.messages.length - 1; i >= 0; i--) {
			if ($activeSession.messages[i].streaming) {
				return i;
			}
		}
		return -1;
	});

	// Show the existing streaming indicator at the bottom of the conversation
	// whenever the turn is still active (isStreaming) but nothing else is
	// currently signaling activity. This fills the dead air in the gap between a
	// completed text block and the next tool call (the model generating the
	// tool_use, or hidden thinking), where no message is streaming and no tool
	// card exists yet — which otherwise looks frozen for many seconds. We suppress
	// it when something already shows progress: a streaming message (cursor), a
	// running tool (spinner), or a running subagent — to avoid a double indicator.
	const showGapIndicator = $derived.by(() => {
		if (!$activeSession.isStreaming) return false;
		const msgs = $activeSession.messages;
		if (msgs.length === 0) return false;
		const last = msgs[msgs.length - 1];
		if (last.streaming) {
			// A streaming thinking block hidden by user preference renders nothing,
			// so the indicator is still needed; otherwise the cursor covers it.
			return last.type === 'thinking' && chatPrefs?.showThinkingBlocks === false;
		}
		if (last.type === 'tool_use' && last.toolStatus === 'running') return false;
		if (last.type === 'subagent' && (last.agentStatus === 'running' || last.agentStatus === 'pending')) return false;
		return true;
	});

	// Bottom-of-chat activity indicator, derived purely from streaming + active
	// task count (see indicator-state.ts):
	//   - streaming                       → 'dots'    (handled by showGapIndicator / per-message cursor)
	//   - parked (!streaming) + tasks > 0 → 'waiting' (calm "Waiting for N task(s)…")
	//   - parked + no tasks               → 'none'
	// Bug 1: this guarantees the bouncing "..." never persists after a turn parks
	// waiting on background work — the calm waiting text shows instead.
	const activeTaskCount = $derived($activeSession.activeTasks?.length ?? 0);
	const indicatorState = $derived(
		deriveIndicatorState($activeSession.isStreaming, activeTaskCount)
	);
	const waitingText = $derived(waitingLabel(activeTaskCount));

	// --- Tool call grouping ---
	// Groups 2+ consecutive "regular" tool_use messages into a single display item.
	// TodoWrite, Task, and Agent tools are excluded from grouping.
	type DisplayItem =
		| { kind: 'message'; message: ChatMessage; index: number }
		| { kind: 'tool_group'; tools: ChatMessage[]; indices: number[] }
		| { kind: 'system_group'; messages: ChatMessage[]; indices: number[] };

	function isGroupableTool(msg: ChatMessage): boolean {
		return msg.type === 'tool_use'
			&& msg.toolName !== 'TodoWrite'
			&& msg.toolName !== 'Task'
			&& msg.toolName !== 'Agent';
	}

	// Subtypes considered low-signal / hook-related — group into a collapsible
	// summary when they arrive consecutively. Important system messages
	// (errors, compact boundaries, mode changes, /context, local commands,
	// agent launches, rewind notices) are intentionally excluded so they stay
	// visible as full cards.
	const GROUPABLE_SYSTEM_SUBTYPES = new Set([
		'hook_started',
		'hook_response',
		'stop_hook_summary',
		'turn_duration',
		'task_started',
		'task_progress',
		'task_updated',
		'task_notification'
	]);

	function isGroupableSystem(msg: ChatMessage): boolean {
		return msg.type === 'system'
			&& GROUPABLE_SYSTEM_SUBTYPES.has(msg.systemSubtype || '');
	}

	// Memoize the grouped display list by (messages, isStreaming) identity.
	// The active-session store emits a fresh state object on every WS stream
	// delta (it updates `wsStreamContent`, which the streaming bubble reads via
	// its own prop). Those deltas arrive faster than the 150ms poll and do NOT
	// change the `messages` array reference — yet without this guard the derived
	// rebuilt the entire O(N) grouped list and returned a new array each time,
	// forcing the {#each} to re-diff every message. That made streaming get
	// progressively choppier as a session grew. A plain object (not $state)
	// avoids Svelte 5 compiling these into reactive signals inside the derived.
	const _displayCache: { messages: ChatMessage[] | null; isStreaming: boolean; items: DisplayItem[] } =
		{ messages: null, isStreaming: false, items: [] };

	const displayItems: DisplayItem[] = $derived.by(() => {
		const messages = $activeSession.messages;
		const isStreamingNow = $activeSession.isStreaming;
		if (messages === _displayCache.messages && isStreamingNow === _displayCache.isStreaming) {
			// Same message identities (e.g. a WS-delta-only update) — reuse the
			// cached array reference so downstream {#each} skips re-diffing.
			return _displayCache.items;
		}
		const items: DisplayItem[] = [];
		// Diagnostic: log when displayItems re-evaluates after streaming ends
		if (STREAM_DEBUG && !isStreamingNow && messages.length > 0) {
			const aTexts = messages.filter((m: ChatMessage) => m.role === 'assistant' && m.type === 'text');
			const hasStrmMsg = messages.some((m: ChatMessage) => m.streaming);
			console.debug(`[displayItems] re-eval: ${messages.length} msgs, streaming=${isStreamingNow}, hasStreamingMsg=${hasStrmMsg}, aTexts=${aTexts.length} [${aTexts.map((m: ChatMessage) => `len=${(m.content||'').length}`).join(',')}]`);
		}
		// Deduplicate by message ID to prevent Svelte 5 each_key_duplicate crashes.
		// Duplicates can originate from delta merge edge cases or corrupted caches.
		const seenIds = new Set<string>();
		let i = 0;

		while (i < messages.length) {
			const msg = messages[i];
			if (msg.id && seenIds.has(msg.id)) { i++; continue; }

			if (msg.id) seenIds.add(msg.id);

			if (isGroupableTool(msg)) {
				// Only group strictly consecutive groupable tools.
				// Any assistant text/thinking between tools breaks the group.
				const tools: ChatMessage[] = [msg];
				const indices: number[] = [i];
				let j = i + 1;
				while (j < messages.length && isGroupableTool(messages[j])) {
					const toolMsg = messages[j];
					if (toolMsg.id && seenIds.has(toolMsg.id)) { j++; continue; }
					if (toolMsg.id) seenIds.add(toolMsg.id);
					tools.push(toolMsg);
					indices.push(j);
					j++;
				}

				if (tools.length >= 2) {
					items.push({ kind: 'tool_group', tools, indices });
				} else {
					items.push({ kind: 'message', message: msg, index: i });
				}
				i = j;
			} else if (isGroupableSystem(msg)) {
				// Group consecutive low-signal system/hook messages.
				const sysMsgs: ChatMessage[] = [msg];
				const indices: number[] = [i];
				let j = i + 1;
				while (j < messages.length && isGroupableSystem(messages[j])) {
					const sysMsg = messages[j];
					if (sysMsg.id && seenIds.has(sysMsg.id)) { j++; continue; }
					if (sysMsg.id) seenIds.add(sysMsg.id);
					sysMsgs.push(sysMsg);
					indices.push(j);
					j++;
				}

				if (sysMsgs.length >= 2) {
					items.push({ kind: 'system_group', messages: sysMsgs, indices });
				} else {
					items.push({ kind: 'message', message: msg, index: i });
				}
				i = j;
			} else {
				items.push({ kind: 'message', message: msg, index: i });
				i++;
			}
		}

		_displayCache.messages = messages;
		_displayCache.isStreaming = isStreamingNow;
		_displayCache.items = items;
		return items;
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
			programmaticScrollDepth++;
			const preSH = containerRef.scrollHeight;
			const preST = containerRef.scrollTop;
			const preCH = containerRef.clientHeight;
			containerRef.scrollTop = containerRef.scrollHeight;
			lastKnownScrollTop = containerRef.scrollTop;
			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] scrollToBottom preSH=${preSH} preST=${preST} preCH=${preCH} postST=${containerRef.scrollTop} postSH=${containerRef.scrollHeight} depth=${programmaticScrollDepth}`);
			// Use double RAF to ensure scroll events from this programmatic scroll are ignored
			// Single RAF isn't enough - browser may fire scroll events across multiple frames
			requestAnimationFrame(() => {
				requestAnimationFrame(() => {
					programmaticScrollDepth = Math.max(0, programmaticScrollDepth - 1);
				});
			});
		}
	}

	function handleScroll() {
		if (!containerRef) return;
		if (programmaticScrollDepth > 0) {
			// Scroll event triggered by (or racing with) a programmatic scroll or a
			// resize-induced scrollTop clamp. Make no auto-scroll decision, but keep
			// the baseline current — otherwise a later genuine user scroll is measured
			// against a stale pre-scroll position and falsely flagged as a scroll-up,
			// disabling auto-scroll mid-stream (the "chat scrolls up after send" bug).
			lastKnownScrollTop = containerRef.scrollTop;
			return;
		}

		// Throttle to one handler per animation frame to prevent main thread blocking
		if (scrollRafPending) return;
		scrollRafPending = true;
		requestAnimationFrame(() => {
			scrollRafPending = false;
			if (!containerRef) return;
			if (programmaticScrollDepth > 0) {
				lastKnownScrollTop = containerRef.scrollTop;
				return;
			}

			const cacheKey = $activeSession.sessionId ?? 'new';
			const currentScrollTop = containerRef.scrollTop;

			// Save scroll position to non-reactive cache (doesn't trigger effects)
			scrollPositionCache.set(cacheKey, currentScrollTop);
			// Also save to session store for cross-session persistence
			activeSession.setScrollTop(currentScrollTop);

			const nearBottom = isNearBottom();

			if (nearBottom) {
				// User is at/near the bottom - enable auto-scroll
				shouldAutoScroll = true;
			} else if (currentScrollTop < lastKnownScrollTop) {
				// Scroll position decreased while not near bottom.
				// Require a minimum delta to distinguish intentional user scroll-up
				// from layout-induced jitter (e.g., input area resize after sending
				// a message causes the browser to adjust scrollTop by a few pixels,
				// which would falsely disable auto-scroll for the entire stream).
				const scrollUpDelta = lastKnownScrollTop - currentScrollTop;
				// Threshold must exceed browser scroll-anchoring drift (~10-20px)
				// that occurs when late-rendering content (tables, code blocks) shifts
				// the scroll container after scrollToBottomUntilStable stops. A value
				// too low causes the handler to treat post-switch layout drift as a
				// user scroll-up and silently disable auto-scroll mid-render.
				if (scrollUpDelta > 50) {
					// eslint-disable-next-line no-console
					if (STREAM_DEBUG) console.log(`[AS] handleScroll DISABLE currST=${currentScrollTop} lastST=${lastKnownScrollTop} delta=${scrollUpDelta} SH=${containerRef.scrollHeight} CH=${containerRef.clientHeight}`);
					shouldAutoScroll = false;
				}
			}
			// else: scrollTop increased or unchanged but not near bottom —
			// likely content grew below viewport. Don't change shouldAutoScroll.

			lastKnownScrollTop = currentScrollTop;
		});
	}

	// Restore scroll position only on mount - not on every reactive update
	onMount(() => {
		isMounted = true;
		// eslint-disable-next-line no-console
		if (STREAM_DEBUG) console.log(`[AS] onMount sid=${$activeSession.sessionId} msgLen=${$activeSession.messages.length}`);
		// Wait for DOM to be ready
		requestAnimationFrame(() => {
			if (!containerRef) return;

			const cacheKey = $activeSession.sessionId ?? 'new';
			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] onMount-raf cacheKey=${cacheKey} cachedPos=${scrollPositionCache.get(cacheKey)} cachedAutoS=${autoScrollCache.get(cacheKey)} sessionScrollTop=${$activeSession.scrollTop} SH=${containerRef.scrollHeight} ST=${containerRef.scrollTop} CH=${containerRef.clientHeight}`);

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
					programmaticScrollDepth++;
					containerRef.scrollTop = storedPosition;
					lastKnownScrollTop = containerRef.scrollTop;
					shouldAutoScroll = false;
					requestAnimationFrame(() => {
						programmaticScrollDepth = Math.max(0, programmaticScrollDepth - 1);
					});
				} else {
					// No saved position even though they scrolled up - scroll to bottom
					shouldAutoScroll = true;
					scrollToBottom();
				}
			} else if ($activeSession.messages.length > 0) {
				// Auto-scroll was on, or first visit - always show latest content.
				// Use scrollToBottomUntilStable so late-rendering content (tables,
				// code blocks) doesn't leave the viewport pinned above the bottom.
				// Covers the session-switch SKIP (no prevId) path where the switch
				// effect returns early and only this onMount scroll runs.
				shouldAutoScroll = true;
				pendingScrollToBottom = true;
				scrollToBottomUntilStable();
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
		// eslint-disable-next-line no-console
		if (STREAM_DEBUG) console.log(`[AS] visibility=${document.visibilityState} autoS=${shouldAutoScroll} SH=${containerRef?.scrollHeight} ST=${containerRef?.scrollTop} CH=${containerRef?.clientHeight}`);
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
		if (pendingScrollTimer) { clearInterval(pendingScrollTimer); pendingScrollTimer = null; }
		const cacheKey = $activeSession.sessionId ?? 'new';

		if (containerRef) {
			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] onDestroy saving cacheKey=${cacheKey} ST=${containerRef.scrollTop} autoS=${shouldAutoScroll} SH=${containerRef.scrollHeight} CH=${containerRef.clientHeight}`);
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

		// eslint-disable-next-line no-console
		if (STREAM_DEBUG) console.log(`[AS] session-switch effect from=${prevId} to=${currentId} msgLen=${$activeSession.messages.length} lastId=${$activeSession.messages[$activeSession.messages.length - 1]?.id} lastRole=${$activeSession.messages[$activeSession.messages.length - 1]?.role} SH=${containerRef?.scrollHeight} ST=${containerRef?.scrollTop} CH=${containerRef?.clientHeight}`);

		// Save scroll state for the old session (if any)
		if (prevId && containerRef) {
			scrollPositionCache.set(prevId, containerRef.scrollTop);
			autoScrollCache.set(prevId, shouldAutoScroll);
			activeSession.setScrollTop(containerRef.scrollTop);
			// Prevent unbounded growth over long usage
			evictOldest(scrollPositionCache, SCROLL_CACHE_MAX);
			evictOldest(autoScrollCache, SCROLL_CACHE_MAX);
		}

		prevTrackedSessionId = currentId;

		// Skip scroll restore for initial sessionId assignment on new sessions
		// (undefined → new ID). The component is already at the correct position
		// (bottom) from onMount, and restoring would fight the initial render.
		if (!prevId) {
			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] session-switch SKIP (no prevId) to=${currentId}`);
			return;
		}

		// Always scroll to bottom on session switch. The autoScrollCache can
		// have stale false entries from race conditions (DOM shifts during async
		// message loading caused handleScroll to wrongly set shouldAutoScroll=false).
		// Users expect to see the latest messages when switching sessions.
		pendingScrollToBottom = true;
		shouldAutoScroll = true;
		// Reset message tracking so the message-change effect fires even if
		// the new session has the same message count as the previous one.
		prevMessagesLength = -1;
		prevLastContent = '';
		prevLastMetadata = null;

		// Immediate scroll attempt — may run on empty/partial container.
		// pendingScrollToBottom ensures another scroll after messages load.
		// Use scrollToBottomUntilStable to handle large content that takes
		// multiple frames to render (tables, code blocks, etc).
		tick().then(() => {
			if (!containerRef) return;
			scrollToBottomUntilStable();
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
				// eslint-disable-next-line no-console
				if (STREAM_DEBUG) console.log(`[AS] resizeObserver → scrollToBottom SH=${containerRef.scrollHeight} ST=${containerRef.scrollTop} CH=${containerRef.clientHeight}`);
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
	// Also tracks thinking block content — thinking blocks are inserted before the
	// streaming assistant message, so only watching the last message misses them.
	const lastMessageContent = $derived.by(() => {
		const msgs = $activeSession.messages;
		if (msgs.length === 0) return '';

		const lastMsg = msgs[msgs.length - 1];
		let content = '';

		// For tool_use messages, track toolInput changes (especially TodoWrite)
		if (lastMsg.type === 'tool_use') {
			if (lastMsg.toolInput) content = JSON.stringify(lastMsg.toolInput);
			// During streaming, toolInput is not yet parsed — track partialToolInput
			// so auto-scroll triggers as content streams in (e.g. TodoWrite).
			else if (lastMsg.partialToolInput) content = lastMsg.partialToolInput;
		} else {
			content = lastMsg.content || '';
		}

		// Track thinking block content length so growing thinking triggers scroll.
		// Thinking blocks sit before the streaming assistant message, so their
		// content changes are invisible if we only watch the last message.
		// Scan backwards (max 3 messages) to find the nearest thinking block.
		for (let i = msgs.length - 1; i >= Math.max(0, msgs.length - 3); i--) {
			if (msgs[i].type === 'thinking') {
				content += `|t:${(msgs[i].content || '').length}`;
				break;
			}
		}

		return content;
	});

	// Get metadata for the last assistant message that has metadata (cost/duration
	// rendered there). This message may not be the absolute last in the array
	// (e.g. a TodoWrite tool_use could follow it).
	const lastMessageMetadata = $derived.by(() => {
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
		const currentContent = lastMessageContent;
		const currentMetadata = lastMessageMetadata;

		// Only scroll if there's an actual change (new message, content update, or metadata added)
		const hasNewMessage = currentLength !== prevMessagesLength;
		const hasContentChange = currentContent !== prevLastContent;
		const hasMetadataChange = currentMetadata !== prevLastMetadata;

		// Bug fix: when user sends a message, the ChatInput shrinks (clears text),
		// causing the scroll container to grow. The browser adjusts scrollTop downward,
		// which handleScroll misinterprets as a user scroll-up and disables autoscroll.
		// Force autoscroll back on when the new message is from the user — they always
		// want to see the response to what they just sent.
		const newUserMessage =
			hasNewMessage &&
			currentLength > 0 &&
			$activeSession.messages[currentLength - 1]?.role === 'user';
		if (newUserMessage) {
			shouldAutoScroll = true;
		}

		if (hasNewMessage || hasContentChange) {
			const newest = $activeSession.messages[currentLength - 1];
			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] effect len=${currentLength} prevLen=${prevMessagesLength} newMsg=${hasNewMessage} contentChg=${hasContentChange} autoS=${shouldAutoScroll} pend=${pendingScrollToBottom} newestId=${newest?.id} newestRole=${newest?.role} newestType=${newest?.type} newestStreaming=${newest?.streaming} cLen=${(newest?.content || '').length} SH=${containerRef?.scrollHeight} ST=${containerRef?.scrollTop} CH=${containerRef?.clientHeight}`);
		}

		// Scroll to bottom on content changes if auto-scroll is active,
		// OR if a session switch is pending scroll-to-bottom (messages just loaded).
		const shouldScroll = shouldAutoScroll || pendingScrollToBottom;
		if ((hasNewMessage || hasContentChange || hasMetadataChange) && shouldScroll && containerRef) {
			const wasPending = pendingScrollToBottom;
			if (pendingScrollToBottom) {
				pendingScrollToBottom = false;
				shouldAutoScroll = true;
			}
			if (wasPending) {
				// Session switch: use aggressive scroll that retries until DOM is stable.
				// Large content (tables, code blocks) may take multiple frames to render.
				tick().then(() => scrollToBottomUntilStable());
			} else if (newUserMessage) {
				// Just-sent user message: its content (attachment previews, markdown)
				// and the input-shrink reflow can land over several frames, so a single
				// scroll can leave the message above the fold. Scroll across a few frames
				// to stay pinned to the bottom without the long-running stable loop.
				tick().then(() => {
					let frames = 0;
					const pin = () => {
						scrollToBottom();
						if (++frames < 4) requestAnimationFrame(pin);
					};
					pin();
				});
			} else {
				// Normal streaming: single scroll is enough.
				tick().then(() => requestAnimationFrame(scrollToBottom));
			}
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
			{#each displayItems as item (item.kind === 'tool_group' ? 'tg-' + item.tools[0].id : item.kind === 'system_group' ? 'sg-' + item.messages[0].id : item.message.id)}
				<!-- content-visibility: auto lets the browser skip paint/layout for off-screen messages -->
				<div class="msg-virtualized">
				{#if item.kind === 'tool_group'}
					<!-- Grouped tool calls (2+ consecutive regular tools from the same turn) -->
					<ToolUseGroup tools={item.tools} />
				{:else if item.kind === 'system_group'}
					<!-- Grouped system/hook messages (2+ consecutive low-signal events) -->
					<SystemMessageGroup messages={item.messages} />
				{:else}
					{@const message = item.message}
					{@const messageIndex = item.index}
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
							thinkingTokens={message.thinkingTokens}
							profileName={getProfileName(message)}
						/>
					{:else if message.type === 'text' || !message.type}
						<AssistantMessage
							content={message.content}
							wsContent={message.streaming && messageIndex === lastStreamingMessageIndex ? $activeSession.wsStreamContent : undefined}
							messageIndex={messageIndex}
							messageId={message.id}
							messageUuid={message.jsonlUuid}
							sessionId={$activeSession.sessionId}
							streaming={message.streaming && messageIndex === lastStreamingMessageIndex}
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
					{:else if message.type === 'tool_use' && (message.toolName === 'Task' || message.toolName === 'Agent')}
						<!-- Subagent tool_use (SDK "Task" or "Agent") - Skip if a linked SubagentMessage exists -->
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
							serverSide={message.serverSide ?? message.server_side ?? false}
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
					{:else if message.type === 'team_member'}
						<div class="w-full">
							<div class="min-w-0">
								<TeamMemberMessage {message} parentSessionId={$activeSession.sessionId ?? ''} />
							</div>
						</div>
					{/if}
				{/if}
			</div>
			{/each}

			<!-- Reuse the existing streaming indicator to fill the gap between steps
			     (e.g. after a text block, while the model generates the next tool
			     call) so the agent never appears frozen while it's still working. -->
			{#if showGapIndicator}
				<div class="pl-2 py-1">
					<StreamingIndicator />
				</div>
			{:else if indicatorState === 'waiting'}
				<!-- Bug 1: turn parked waiting on background tasks. Show a distinct,
				     calm indicator instead of the bouncing "..." dots so the UI
				     doesn't look frozen or like the model is still typing. -->
				<div class="pl-2 py-1 flex items-center gap-2 text-xs text-muted-foreground">
					<span class="waiting-pulse w-1.5 h-1.5 rounded-full"></span>
					<span>{waitingText}</span>
				</div>
			{/if}

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

	/* Calm "breathing" dot for the parked waiting indicator — distinct from the
	   bouncing StreamingIndicator dots so a parked turn reads as waiting, not
	   actively typing. */
	.waiting-pulse {
		background: var(--muted-foreground);
		animation: waitingBreath 1.8s ease-in-out infinite;
	}

	@keyframes waitingBreath {
		0%, 100% { opacity: 0.35; }
		50% { opacity: 0.9; }
	}

	@media (prefers-reduced-motion: reduce) {
		.waiting-pulse {
			animation: none;
			opacity: 0.7;
		}
	}
</style>

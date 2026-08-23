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
	import { STREAM_DEBUG } from '$lib/utils/debug-log';
	import UserMessage from './UserMessage.svelte';
	import AssistantMessage from './AssistantMessage.svelte';
	import TurnActivity from './TurnActivity.svelte';
	import TurnNavigator from './TurnNavigator.svelte';
	import { findStreamingAnchorIndex, shouldShowGapIndicator, shouldRenderAssistantText } from '$lib/stores/session/gap-indicator';
	import {
		JUMP_TO_LATEST_DELAY_MS,
		MIN_TRANSCRIPT_TAIL_RESERVE,
		isUserScrollUpGesture,
		nextStickToBottom,
		resolveAnchoredRevealDelta,
		resolveAnchoredTailReserve,
		resolveCurrentTurnAnchorScrollTop,
		shouldAutoScrollOnChange,
		shouldShowJumpToLatest,
		type ScrollMetrics
	} from '$lib/stores/session/auto-scroll';
	import ToolUseMessage from './ToolUseMessage.svelte';
	import WorkLogGroup from './WorkLogGroup.svelte';
	import { buildDisplayItems, type DisplayItem } from '$lib/utils/work-log';
	import {
		deriveUserTurnNavigationItems,
		isNavigableUserTurn,
		resolveCurrentTurnIndex,
		resolveLiveTurnStartedAt,
		turnAnchorId
	} from '$lib/utils/transcript-navigation';
	import { getModelLabel } from '$lib/constants/models.svelte';
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
	import MediaLightbox from './MediaLightbox.svelte';
	import PendingMessages from './PendingMessages.svelte';
	import ActiveTasks from './ActiveTasks.svelte';
	import { stopSessionTask } from '$lib/api/sessions';
	import { notify } from '$lib/stores/notifications';

	// Non-reactive scroll position cache to avoid triggering effects.
	// Module-level so it persists across component instances.
	// Capped at 100 entries to prevent unbounded growth over long sessions.
	const SCROLL_CACHE_MAX = 100;
	// How many trailing messages are kept OUT of content-visibility virtualization.
	// The actively-growing tail (the streaming message + its immediate predecessors)
	// must never be virtualized/re-estimated, or the browser re-guesses their
	// intrinsic size against the 80px placeholder as the stream grows and
	// scrollHeight jitters — the "scroll jumps around while streaming" bug. Far-up
	// off-screen messages keep content-visibility for the paint/layout perf win.
	const LIVE_TAIL = 8;
	const scrollPositionCache = new Map<string, number>();
	// Persist auto-scroll state across component mount/destroy cycles
	const autoScrollCache = new Map<string, boolean>();

	/** Exhaustive keyed identity for every presentation-model variant. */
	function displayItemKey(item: DisplayItem): string {
		switch (item.kind) {
			case 'message':
				return `message-${item.message.id || item.index}`;
			case 'system_group':
				return `system-group-${item.messages[0]?.id || item.indices[0] || 0}`;
			case 'work_log':
				return item.key;
		}
	}

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
	function handleRewindComplete(warning?: string, rewoundPrompt?: string) {
		// If there's a warning (partial success), show it as a system message
		if (warning) {
			activeSession.addSystemMessage(warning, 'rewind_warning');
		}

		// A rewind to a user message removed that very message — queue its text
		// so ChatInput refills the composer after the reload settles, letting the
		// user edit or resend what they rewound away.
		if (rewoundPrompt) {
			activeSession.setPendingRestorePrompt(rewoundPrompt);
		}

		// Clear stale messages from the store before reloading.
		// Without this, the defensive check in the history handler would
		// preserve old (broken) messages when the reload returns fewer/zero.
		activeSession.clearMessages();

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
	// Full metrics from the previous handled scroll event — fed to the pure
	// nextStickToBottom() so it can measure scroll direction against the same
	// geometry the user actually saw.
	let lastScrollMetrics: ScrollMetrics | null = null;
	// Set on session switch — ensures scroll-to-bottom fires after messages load,
	// even if intermediate DOM changes set shouldAutoScroll to false.
	let pendingScrollToBottom = false;
	let pendingScrollTimer: ReturnType<typeof setInterval> | null = null;

	// Reader recovery affordance. Away-from-end waits briefly before appearing so
	// session switches and layout settles never flash a transient button.
	let showJumpToLatest = $state(false);
	let jumpRevealTimer: ReturnType<typeof setTimeout> | null = null;

	// Non-virtualized current-turn anchoring. The spacer grows only for the latest
	// sent turn, then shrinks one-for-one beneath the response as it fills the view.
	let tailSpacerRef = $state<HTMLDivElement | null>(null);
	let currentTurnAnchorId: string | null = null;
	let tailReservePx = $state(MIN_TRANSCRIPT_TAIL_RESERVE);
	let anchorMaintenanceRaf: number | null = null;
	// Passive reserve for the LATEST user turn once the sent-turn anchor has
	// released. Without it the deepest possible scroll bottoms out right after
	// the last response, so a short final response can never bring its prompt to
	// the viewport top — scroll-to-bottom AND timeline navigation to the newest
	// tick both fall short. Mirrors the anchored-turn reserve; recomputed from
	// live geometry so it shrinks back as content grows.
	let latestTurnTailPx = $state(MIN_TRANSCRIPT_TAIL_RESERVE);

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

	/**
	 * Display name for the model that produced a message.
	 *
	 * The reducer stamps `metadata.model` on a closed assistant message when the
	 * turn's event carried one; that is the only per-message model attribution
	 * that exists, so messages without it fall back to the profile name (which
	 * IS per-message, via profileId).
	 */
	function getModelDisplayName(message: ChatMessage): string | null {
		const model = (message.metadata as Record<string, unknown> | undefined)?.model;
		if (typeof model !== 'string' || !model) return null;
		return getModelLabel(model);
	}

	// Chat preferences (reactive via .current getter)
	const chatPrefs = $derived(chatPreferences.current);

	// Find the message that owns the single streaming indicator/cursor — only
	// that one shows the bouncing dots, otherwise a row of them appears after
	// every tool call. Trailing EMPTY streaming assistant-text (the turn-start
	// synthetic phantom, re-appended by the WS-merge) is skipped so it can't
	// steal the anchor from a real streaming message; with no real streaming
	// message it returns -1, letting the phantom render invisibly and the bottom
	// gap indicator take over. See gap-indicator.ts.
	const lastStreamingMessageIndex = $derived(findStreamingAnchorIndex($activeSession.messages));

	// Show the streaming indicator at the bottom of the conversation whenever the
	// turn is still active (isStreaming) but nothing else is currently signaling
	// activity — the dead air after a completed tool call and before the next
	// assistant message, where the model is thinking/generating the next tool_use
	// and the chat otherwise looks frozen for many seconds. A trailing empty
	// streaming synthetic is treated as transparent so it can't suppress this.
	// Background tasks do NOT gate the dots — the main turn streams concurrently
	// with running tasks, which have their own "Running N task" panel. See
	// gap-indicator.ts for the full contract + tests.
	const showGapIndicator = $derived(
		shouldShowGapIndicator(
			$activeSession.messages,
			$activeSession.isStreaming,
			chatPrefs?.showThinkingBlocks !== false
		)
	);

	// Launch tool_use ids of background tasks the registry reports as currently
	// running (from the /live `activeTasks`). Passed to <SubagentMessage> so a
	// card whose `toolId` matches a running task is forced to the spinner even
	// when the server-supplied agentStatus is stale (e.g. fresh loads of parked
	// sessions). See utils/subagent-status.ts.
	const runningToolIds = $derived(
		new Set(
			($activeSession.activeTasks ?? [])
				.filter(t => t.status === 'running' && t.toolUseId)
				.map(t => t.toolUseId as string)
		)
	);

	// --- Transcript grouping ---
	// The turn-aware work-log builder lives in $lib/utils/work-log so it can be
	// reasoned about (and tested) without a DOM. This component only decides
	// which component each surviving message becomes.

	// Rewind fallback occurrence: 0-based index of a user PROMPT among the
	// PRIOR user prompts with IDENTICAL (trimmed) content — usually 0. The
	// backend resolves the target by content match against the SDK transcript,
	// so this only disambiguates duplicate prompts. Content anchoring instead
	// of a global ordinal because the SDK transcript contains user-role lines
	// we never render (synthetic "[Request interrupted by user]" markers, task
	// notifications, prompts retained after an empty-turn discard) — global
	// positions skew in any session that has seen an interrupt. No-match still
	// fails closed server-side (content guard).
	function userPromptOccurrence(messages: ChatMessage[], index: number): number {
		const want = (messages[index]?.content ?? '').trim();
		let occurrence = 0;
		for (let k = 0; k < index; k++) {
			const m = messages[k];
			if (m.role === 'user' && m.type !== 'command' && (m.content ?? '').trim() === want) {
				occurrence++;
			}
		}
		return occurrence;
	}

	const latestTurnUnsettled = $derived(
		$activeSession.isStreaming ||
		Boolean($activeSession.liveTurnId) ||
		($activeSession.activeTasks ?? []).some((task) => task.status === 'running') ||
		($activeSession.pendingPermissions ?? []).length > 0 ||
		($activeSession.pendingQuestions ?? []).length > 0
	);

	// Memoize the grouped display list by message and lifecycle identity.
	// The active-session store emits a fresh state object on every SSE stream
	// delta (the reducer coalesces deltas into `messages` via rAF). Many of
	// those emissions do NOT change the `messages` array reference — yet without
	// this guard the derived rebuilt the entire O(N) grouped list and returned a
	// new array each time, forcing the {#each} to re-diff every message. That
	// made streaming get progressively choppier as a session grew. A plain
	// object (not $state) avoids Svelte 5 compiling these into reactive signals
	// inside the derived.
	const _displayCache: { messages: ChatMessage[] | null; isStreaming: boolean; latestTurnUnsettled: boolean; groupingEnabled: boolean; items: DisplayItem[] } =
		{ messages: null, isStreaming: false, latestTurnUnsettled: false, groupingEnabled: true, items: [] };

	const displayItems: DisplayItem[] = $derived.by(() => {
		const messages = $activeSession.messages;
		const isStreamingNow = $activeSession.isStreaming;
		const latestIsUnsettled = latestTurnUnsettled;
		// When grouping is off, groupable tools fall through to the plain
		// single-message path (system grouping is unaffected by this toggle).
		const groupingEnabled = chatPrefs?.groupToolCalls !== false;
		if (
			messages === _displayCache.messages
			&& isStreamingNow === _displayCache.isStreaming
			&& latestIsUnsettled === _displayCache.latestTurnUnsettled
			&& groupingEnabled === _displayCache.groupingEnabled
		) {
			// Same message identities (e.g. a WS-delta-only update) — reuse the
			// cached array reference so downstream {#each} skips re-diffing.
			return _displayCache.items;
		}
		// Diagnostic: log when displayItems re-evaluates after streaming ends
		if (STREAM_DEBUG && !isStreamingNow && messages.length > 0) {
			const aTexts = messages.filter((m: ChatMessage) => m.role === 'assistant' && m.type === 'text');
			const hasStrmMsg = messages.some((m: ChatMessage) => m.streaming);
			console.debug(`[displayItems] re-eval: ${messages.length} msgs, streaming=${isStreamingNow}, hasStreamingMsg=${hasStrmMsg}, aTexts=${aTexts.length} [${aTexts.map((m: ChatMessage) => `len=${(m.content||'').length}`).join(',')}]`);
		}

		const items = buildDisplayItems(messages, {
			isStreaming: isStreamingNow,
			latestTurnUnsettled: latestIsUnsettled,
			groupingEnabled
		});

		_displayCache.messages = messages;
		_displayCache.isStreaming = isStreamingNow;
		_displayCache.latestTurnUnsettled = latestIsUnsettled;
		_displayCache.groupingEnabled = groupingEnabled;
		_displayCache.items = items;
		return items;
	});

	// Transcript-local navigation: one stable anchor per user prompt. The left
	// timeline is only mounted when it can move somewhere (two or more turns).
	const turnNavigationItems = $derived(deriveUserTurnNavigationItems($activeSession.messages));
	const turnNavigationSignature = $derived(
		turnNavigationItems.map((item) => item.id).join('\u0000')
	);
	let currentTurnIndex = $state(0);
	let manualNavigationReleaseUntil = 0;

	// Anchored to the prompt that OPENED the live turn: a message steered into a
	// turn already running must not restart its clock.
	const liveTurnStartedAt = $derived.by(() => {
		if (!$activeSession.isStreaming) return null;
		return resolveLiveTurnStartedAt($activeSession.messages, $activeSession.liveTurnId);
	});

	function updateCurrentTurnIndex() {
		if (!containerRef || turnNavigationItems.length === 0) {
			currentTurnIndex = 0;
			return;
		}
		const containerTop = containerRef.getBoundingClientRect().top;
		const readingLine = Math.min(140, containerRef.clientHeight * 0.3);
		const tops = turnNavigationItems.map((item) => {
			const anchor = document.getElementById(turnAnchorId(item.id));
			return anchor && containerRef?.contains(anchor)
				? anchor.getBoundingClientRect().top - containerTop
				: Number.POSITIVE_INFINITY;
		});
		currentTurnIndex = resolveCurrentTurnIndex(tops, readingLine);
	}

	function navigateToTurn(index: number) {
		if (!containerRef) return;
		const item = turnNavigationItems[index];
		if (!item) return;
		const anchor = document.getElementById(turnAnchorId(item.id));
		if (!anchor || !containerRef.contains(anchor)) return;

		disengageAutoScroll();
		manualNavigationReleaseUntil = Date.now() + 600;
		currentTurnIndex = index;
		const containerBox = containerRef.getBoundingClientRect();
		const anchorBox = anchor.getBoundingClientRect();
		// Navigating to the newest tick: grow the trailing reserve first so the
		// prompt can actually reach the viewport top instead of the browser
		// clamping the scroll at the current content end. The spacer sits BELOW
		// the content, so the pre-growth target offset stays valid; the scroll
		// itself must wait one tick for the taller DOM or it clamps anyway.
		let needsNewestTickRoom = false;
		if (!currentTurnAnchorId && turnNavigationItems[index + 1] === undefined && tailSpacerRef) {
			const anchorTop = containerRef.scrollTop + anchorBox.top - containerBox.top;
			const contentEnd = containerRef.scrollTop + tailSpacerRef.getBoundingClientRect().top - containerBox.top;
			const desired = resolveAnchoredTailReserve({
				clientHeight: containerRef.clientHeight,
				contentAfterAnchor: Math.max(0, contentEnd - anchorTop)
			});
			if (desired > Math.max(tailReservePx, latestTurnTailPx)) {
				latestTurnTailPx = desired;
				needsNewestTickRoom = true;
			}
		}
		const top = containerRef.scrollTop + anchorBox.top - containerBox.top - 24;
		const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		const applyScroll = () => {
			containerRef?.scrollTo({ top: Math.max(0, top), behavior: reduceMotion ? 'auto' : 'smooth' });
		};
		if (needsNewestTickRoom) void tick().then(applyScroll);
		else applyScroll();
	}

	$effect(() => {
		// Key the effect to turn identities rather than the whole message array so
		// token-by-token assistant deltas do not scan every historical anchor.
		const navigationVersion = `${$activeSession.sessionId ?? 'new'}:${turnNavigationSignature}`;
		tick().then(() => {
			void navigationVersion;
			updateCurrentTurnIndex();
		});
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

	/**
	 * Map a fluid Note tool call onto the todo progress row. Notes have no
	 * per-step status — every mutation returns the full list — so earlier notes
	 * read as done and the newest one as the current step. A cleared or empty
	 * list renders nothing.
	 */
	function extractNotesFromMessage(message: ChatMessage): TodoItem[] | null {
		const lines: string[] = [];
		const result = typeof message.toolResult === 'string' ? message.toolResult : '';
		if (result && !result.includes('(empty)')) {
			for (const raw of result.split('\n')) {
				const line = raw.trim();
				if (line.startsWith('- ')) lines.push(line.slice(2).trim());
			}
		}
		// No result yet (still running): show the note being added, if any.
		if (lines.length === 0) {
			const text =
				typeof message.toolInput?.text === 'string'
					? message.toolInput.text
					: (message.partialToolInput?.match(/"text"\s*:\s*"([^"]*)/)?.[1] ?? '');
			if (text.trim()) lines.push(text.trim());
		}
		if (lines.length === 0) return null;
		return lines.map((content, i) => ({
			content,
			status: i < lines.length - 1 ? 'completed' : 'in_progress',
			activeForm: content
		}));
	}

	// Read the current scroll geometry as plain metrics for the pure decision
	// helpers in auto-scroll.ts.
	function readScrollMetrics(): ScrollMetrics | null {
		if (!containerRef) return null;
		return {
			scrollTop: containerRef.scrollTop,
			scrollHeight: containerRef.scrollHeight,
			clientHeight: containerRef.clientHeight
		};
	}

	function clearJumpRevealTimer() {
		if (jumpRevealTimer) {
			clearTimeout(jumpRevealTimer);
			jumpRevealTimer = null;
		}
	}

	function updateJumpToLatestVisibility(metrics: ScrollMetrics | null = readScrollMetrics()) {
		if (!metrics || !shouldShowJumpToLatest(metrics, programmaticScrollDepth > 0, currentTurnAnchorId !== null)) {
			clearJumpRevealTimer();
			showJumpToLatest = false;
			return;
		}
		if (showJumpToLatest || jumpRevealTimer) return;
		jumpRevealTimer = setTimeout(() => {
			jumpRevealTimer = null;
			const latest = readScrollMetrics();
			if (latest && shouldShowJumpToLatest(latest, programmaticScrollDepth > 0, currentTurnAnchorId !== null)) {
				showJumpToLatest = true;
			}
		}, JUMP_TO_LATEST_DELAY_MS);
	}

	function markProgrammaticScrollSettling() {
		programmaticScrollDepth++;
		requestAnimationFrame(() => {
			requestAnimationFrame(() => {
				programmaticScrollDepth = Math.max(0, programmaticScrollDepth - 1);
				updateJumpToLatestVisibility();
			});
		});
	}

	function setProgrammaticScrollTop(top: number) {
		if (!containerRef) return;
		markProgrammaticScrollSettling();
		containerRef.scrollTop = Math.max(0, top);
		lastKnownScrollTop = containerRef.scrollTop;
		lastScrollMetrics = readScrollMetrics();
	}

	function cancelCurrentTurnAnchor(resetTail = true) {
		if (!currentTurnAnchorId && tailReservePx === MIN_TRANSCRIPT_TAIL_RESERVE) return;
		currentTurnAnchorId = null;
		if (anchorMaintenanceRaf !== null) {
			cancelAnimationFrame(anchorMaintenanceRaf);
			anchorMaintenanceRaf = null;
		}
		if (resetTail) {
			markProgrammaticScrollSettling();
			// Hand the anchored reserve to the passive latest-turn reserve in the
			// same frame: collapsing straight to the minimum would clamp scrollTop
			// downward for a frame before the refresh recomputes the height.
			if (tailReservePx > latestTurnTailPx) latestTurnTailPx = tailReservePx;
			tailReservePx = MIN_TRANSCRIPT_TAIL_RESERVE;
		}
	}

	async function maintainCurrentTurnAnchor(initial = false) {
		if (!containerRef || !tailSpacerRef || !currentTurnAnchorId) return;
		const anchor = document.getElementById(turnAnchorId(currentTurnAnchorId));
		if (!anchor || !containerRef.contains(anchor)) {
			cancelCurrentTurnAnchor();
			return;
		}

		const containerBox = containerRef.getBoundingClientRect();
		const anchorTop = containerRef.scrollTop + anchor.getBoundingClientRect().top - containerBox.top;
		const contentEnd = containerRef.scrollTop + tailSpacerRef.getBoundingClientRect().top - containerBox.top;
		const contentAfterAnchor = Math.max(0, contentEnd - anchorTop);
		tailReservePx = resolveAnchoredTailReserve({
			clientHeight: containerRef.clientHeight,
			contentAfterAnchor
		});
		await tick();
		if (!containerRef || !currentTurnAnchorId) return;

		if (initial) {
			setProgrammaticScrollTop(resolveCurrentTurnAnchorScrollTop(anchorTop));
		} else {
			const delta = resolveAnchoredRevealDelta(
				containerRef.scrollTop,
				containerRef.clientHeight,
				contentEnd
			);
			if (delta > 0) setProgrammaticScrollTop(containerRef.scrollTop + delta);
		}
		updateJumpToLatestVisibility();
	}

	function scheduleCurrentTurnAnchorMaintenance(initial = false) {
		if (!currentTurnAnchorId) return;
		if (anchorMaintenanceRaf !== null) cancelAnimationFrame(anchorMaintenanceRaf);
		anchorMaintenanceRaf = requestAnimationFrame(() => {
			anchorMaintenanceRaf = null;
			void maintainCurrentTurnAnchor(initial);
		});
	}

	/**
	 * Keep enough trailing room below the transcript for the LATEST user turn to
	 * sit at the viewport top when scrolled fully down. The anchored-turn reserve
	 * only covers a freshly-sent prompt; once it releases (or on session load),
	 * this recomputes the same geometry from the last navigable user message so
	 * scroll-to-bottom and timeline navigation can both reach "prompt first on
	 * screen". Shrinks one-for-one as the response grows, bottoming out at the
	 * normal breathing-room reserve.
	 */
	function refreshLatestTurnTailReserve() {
		if (!containerRef || !tailSpacerRef || currentTurnAnchorId) return;
		const messages = $activeSession.messages;
		const latestTurn = messages.findLast((message) => isNavigableUserTurn(message) && message.id);
		if (!latestTurn?.id) {
			if (latestTurnTailPx !== MIN_TRANSCRIPT_TAIL_RESERVE) latestTurnTailPx = MIN_TRANSCRIPT_TAIL_RESERVE;
			return;
		}
		const anchor = document.getElementById(turnAnchorId(latestTurn.id));
		if (!anchor || !containerRef.contains(anchor)) {
			if (latestTurnTailPx !== MIN_TRANSCRIPT_TAIL_RESERVE) latestTurnTailPx = MIN_TRANSCRIPT_TAIL_RESERVE;
			return;
		}
		const containerBox = containerRef.getBoundingClientRect();
		const anchorTop = containerRef.scrollTop + anchor.getBoundingClientRect().top - containerBox.top;
		const contentEnd = containerRef.scrollTop + tailSpacerRef.getBoundingClientRect().top - containerBox.top;
		latestTurnTailPx = resolveAnchoredTailReserve({
			clientHeight: containerRef.clientHeight,
			contentAfterAnchor: Math.max(0, contentEnd - anchorTop)
		});
	}

	function scheduleLatestTurnTailRefresh() {
		if (currentTurnAnchorId) return;
		requestAnimationFrame(() => { void tick().then(refreshLatestTurnTailReserve); });
	}

	function startCurrentTurnAnchor(messageId: string) {
		currentTurnAnchorId = messageId;
		shouldAutoScroll = true;
		clearJumpRevealTimer();
		showJumpToLatest = false;
	}

	function jumpToLatest() {
		clearJumpRevealTimer();
		showJumpToLatest = false;
		manualNavigationReleaseUntil = 0;
		pendingScrollToBottom = false;
		shouldAutoScroll = true;
		cancelCurrentTurnAnchor();
		tick().then(() => requestAnimationFrame(() => {
			scrollToBottom();
			scheduleLatestTurnTailRefresh();
		}));
	}

	function scrollToBottom() {
		if (containerRef) {
			clearJumpRevealTimer();
			showJumpToLatest = false;
			programmaticScrollDepth++;
			const preSH = containerRef.scrollHeight;
			const preST = containerRef.scrollTop;
			const preCH = containerRef.clientHeight;
			containerRef.scrollTop = containerRef.scrollHeight;
			lastKnownScrollTop = containerRef.scrollTop;
			// Refresh the scroll-direction baseline so a later genuine user scroll
			// is measured against where WE left the viewport (the bottom), not a
			// stale pre-scroll position that would read as a false scroll-up.
			lastScrollMetrics = readScrollMetrics();
			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] scrollToBottom preSH=${preSH} preST=${preST} preCH=${preCH} postST=${containerRef.scrollTop} postSH=${containerRef.scrollHeight} depth=${programmaticScrollDepth}`);
			// Use double RAF to ensure scroll events from this programmatic scroll are ignored
			// Single RAF isn't enough - browser may fire scroll events across multiple frames
			requestAnimationFrame(() => {
				requestAnimationFrame(() => {
					programmaticScrollDepth = Math.max(0, programmaticScrollDepth - 1);
					updateJumpToLatestVisibility();
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
			lastScrollMetrics = readScrollMetrics();
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
				lastScrollMetrics = readScrollMetrics();
				return;
			}

			const cacheKey = $activeSession.sessionId ?? 'new';
			const currentScrollTop = containerRef.scrollTop;
			const currentMetrics = readScrollMetrics();

			// Save scroll position to non-reactive cache (doesn't trigger effects)
			scrollPositionCache.set(cacheKey, currentScrollTop);
			// Also save to session store for cross-session persistence
			activeSession.setScrollTop(currentScrollTop);

			// Decide the next stick-to-bottom flag via the pure, unit-tested helper.
			// It enables when the user is at/near the bottom, disables on a
			// deliberate scroll-up, and ignores sub-threshold layout jitter and
			// content-growth-below-the-fold (which would otherwise falsely toggle
			// the flag). On the very first event we have no prior baseline, so just
			// trust the near-bottom geometry.
			if (currentMetrics) {
				const prev = lastScrollMetrics ?? currentMetrics;
				const next = nextStickToBottom(shouldAutoScroll, prev, currentMetrics);
				// Smooth turn navigation must not accidentally re-arm follow mode merely
				// because its destination happens to be near the live edge.
				const resolvedNext = Date.now() < manualNavigationReleaseUntil ? false : next;
				// eslint-disable-next-line no-console
				if (STREAM_DEBUG && resolvedNext !== shouldAutoScroll) console.log(`[AS] handleScroll ${resolvedNext ? 'ENABLE' : 'DISABLE'} currST=${currentScrollTop} prevST=${prev.scrollTop} SH=${currentMetrics.scrollHeight} CH=${currentMetrics.clientHeight}`);
				shouldAutoScroll = resolvedNext;
				lastScrollMetrics = currentMetrics;
				// Scrollbar, keyboard, and accessibility scrolling do not emit our
				// wheel/touch intent hooks. Release the controlled prompt anchor as
				// soon as ordinary scroll geometry says the reader left the live edge.
				if (!resolvedNext && currentTurnAnchorId) cancelCurrentTurnAnchor();
				updateJumpToLatestVisibility(currentMetrics);
			}

			lastKnownScrollTop = currentScrollTop;
			updateCurrentTurnIndex();
		});
	}

	/**
	 * Release stick-to-bottom in response to a genuine user scroll-up gesture.
	 *
	 * This is THE fix for "streaming yanks me back to the bottom even though I
	 * scrolled up to read". While streaming, scrollToBottom() fires on every
	 * delta, so programmaticScrollDepth is elevated almost continuously and
	 * handleScroll keeps swallowing the user's scroll-up (the programmatic-scroll
	 * guard) — shouldAutoScroll never flips false and the next message/tool call
	 * drags them back down. Wheel/touch events are emitted ONLY for real user
	 * input (never by a programmatic scrollTop assignment), so they let us
	 * disengage immediately, regardless of the guard.
	 *
	 * Re-engagement is left to handleScroll: once disengaged, no programmatic
	 * scroll fires, so the guard clears and a normal scroll back to the bottom
	 * re-sticks via nextStickToBottom().
	 */
	function disengageAutoScroll() {
		cancelCurrentTurnAnchor();
		if (!shouldAutoScroll && !pendingScrollToBottom) return;
		// Stop any in-flight programmatic scroll loop so it can't keep fighting
		// the user after they've expressed intent to read earlier content.
		if (pendingScrollTimer) { clearInterval(pendingScrollTimer); pendingScrollTimer = null; }
		pendingScrollToBottom = false;
		shouldAutoScroll = false;
		// eslint-disable-next-line no-console
		if (STREAM_DEBUG) console.log(`[AS] disengageAutoScroll (user gesture) ST=${containerRef?.scrollTop} SH=${containerRef?.scrollHeight} CH=${containerRef?.clientHeight}`);
	}

	// Wheel/trackpad: an upward wheel is an unambiguous intent to read earlier
	// content. Passive — we never preventDefault, just observe direction.
	function handleWheel(e: WheelEvent) {
		if (isUserScrollUpGesture(e.deltaY, 0)) disengageAutoScroll();
	}

	// Touch: dragging the finger DOWN reveals earlier content (scroll-up). Track
	// the finger delta between touchmove events to detect that direction. Wheel
	// events don't fire on touch devices, so this is the mobile equivalent.
	let touchLastY = 0;
	function handleTouchStart(e: TouchEvent) {
		touchLastY = e.touches[0]?.clientY ?? 0;
	}
	function handleTouchMove(e: TouchEvent) {
		const y = e.touches[0]?.clientY ?? touchLastY;
		const touchDeltaY = y - touchLastY; // finger moved down (>0) ⇒ scrolling up
		touchLastY = y;
		if (isUserScrollUpGesture(0, touchDeltaY)) disengageAutoScroll();
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
					lastScrollMetrics = readScrollMetrics();
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
			if (!currentTurnAnchorId) scheduleLatestTurnTailRefresh();
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
		clearJumpRevealTimer();
		if (anchorMaintenanceRaf !== null) cancelAnimationFrame(anchorMaintenanceRaf);
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
		if (prevId) {
			cancelCurrentTurnAnchor();
			clearJumpRevealTimer();
			showJumpToLatest = false;
		}

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
			scheduleLatestTurnTailRefresh();
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
			updateCurrentTurnIndex();
			if (currentTurnAnchorId) {
				scheduleCurrentTurnAnchorMaintenance();
				return;
			}
			scheduleLatestTurnTailRefresh();
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

	// Re-pin to the bottom once a late-loading image/video actually decodes.
	//
	// Screenshots (tool results) and generated media render via {@html} with no
	// intrinsic size until the browser decodes them. The content effect's
	// scrollToBottom() fires against the PRE-decode layout — zero-height media —
	// so the new image lands below the fold. `load`/`loadeddata` don't bubble,
	// but they DO fire during the capture phase on ancestors, so a single
	// capture-phase listener on the container catches every child <img>/<video>
	// the instant it finishes loading. We only re-pin while stick-to-bottom is
	// still engaged, so a user who scrolled up to read isn't yanked back down.
	$effect(() => {
		if (!containerRef || !isMounted) return;
		const el = containerRef;
		const onMediaLoad = (e: Event) => {
			const t = e.target as HTMLElement | null;
			if (!t || (t.tagName !== 'IMG' && t.tagName !== 'VIDEO')) return;
			if (!shouldAutoScroll) return;
			if (currentTurnAnchorId) {
				scheduleCurrentTurnAnchorMaintenance();
				return;
			}
			scheduleLatestTurnTailRefresh();
			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] media load → scrollToBottom tag=${t.tagName} SH=${el.scrollHeight} ST=${el.scrollTop} CH=${el.clientHeight}`);
			requestAnimationFrame(() => { if (shouldAutoScroll) scrollToBottom(); });
		};
		// capture: load/loadeddata do not bubble, so a delegated listener must
		// observe the capture phase to receive them from descendant media.
		el.addEventListener('load', onMediaLoad, true);
		el.addEventListener('loadeddata', onMediaLoad, true);
		return () => {
			el.removeEventListener('load', onMediaLoad, true);
			el.removeEventListener('loadeddata', onMediaLoad, true);
		};
	});

	// Keep the bottom gap indicator in view when it appears.
	//
	// The gap dots render BELOW the last message, and they appear on transitions
	// the content-scroll effect does NOT track as a "content change" — most often a
	// tail tool flipping running→complete (its toolInput, the tracked signal, is
	// unchanged; toolStatus is not tracked) or a trailing empty phantom clearing.
	// So no scrollToBottom() fires for that transition and the freshly-shown dots
	// land ~one indicator-height below the fold: they ARE in the DOM but sit just
	// past the viewport bottom (captured live as ~30px short of true bottom). A
	// page refresh re-pins to the true bottom, which is why "refresh brings the
	// indicator back" — the dots were never missing, just off-screen.
	//
	// When the indicator is showing and we're still stuck to the bottom, pin across
	// two frames (the StreamingIndicator's dots can lay out a frame late) so its
	// full height is revealed. Gated on shouldAutoScroll so a user who scrolled up
	// to read is not yanked back down.
	$effect(() => {
		if (!showGapIndicator || !containerRef || !isMounted) return;
		if (!shouldAutoScroll) return;
		if (currentTurnAnchorId) {
			tick().then(() => scheduleCurrentTurnAnchorMaintenance());
			return;
		}
		tick().then(() => {
			if (!shouldAutoScroll || !containerRef) return;
			scrollToBottom();
			requestAnimationFrame(() => {
				if (shouldAutoScroll) scrollToBottom();
			});
		});
	});

	// Prompts sent but not yet handed to the model — rendered as bubbles below the
	// transcript (see PendingMessages).
	const pendingMessages = $derived($activeSession.queuedMessages ?? []);

	// Keep a freshly-sent pending bubble in view. The content effect below tracks
	// `messages` only, so appending to `queuedMessages` grows the column without
	// scheduling any scroll. Mirrors the gap-indicator effect: respect the current
	// turn anchor when one is live, and never yank a user who scrolled up to read.
	$effect(() => {
		if (pendingMessages.length === 0 || !containerRef || !isMounted) return;
		if (!shouldAutoScroll) return;
		if (currentTurnAnchorId) {
			tick().then(() => scheduleCurrentTurnAnchorMaintenance());
			return;
		}
		tick().then(() => {
			if (!shouldAutoScroll || !containerRef) return;
			scrollToBottom();
			requestAnimationFrame(() => {
				if (shouldAutoScroll) scrollToBottom();
			});
		});
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
			// A screenshot (e.g. browser automation) landing on the last tool call
			// grows the layout but never touches toolInput — fold its presence in
			// so the scroll effect re-fires when it arrives. The image-load listener
			// then re-pins once the browser has actually decoded it.
			if (lastMsg.toolScreenshot) content += `|s:${lastMsg.toolScreenshot.length}`;
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

	// Stop one in-flight background task (the "x" on the ActiveTasks tail line).
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
		} catch {
			notify.error('Failed to stop task. Please try again.');
			return false;
		}
	}

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
		// A new user message ALWAYS re-engages stick-to-bottom — they always want to
		// see the response to what they just sent, regardless of where they'd scrolled
		// (see shouldAutoScrollOnChange in auto-scroll.ts).
		const newestMessage = currentLength > 0
			? $activeSession.messages[currentLength - 1]
			: null;
		const newUserMessage = hasNewMessage && !!newestMessage && isNavigableUserTurn(newestMessage);
		const newUserCommand = hasNewMessage && newestMessage?.role === 'user' && newestMessage.type === 'command';
		if (newUserMessage) {
			shouldAutoScroll = true;
			if (newestMessage.id) startCurrentTurnAnchor(newestMessage.id);
		} else if (newUserCommand) {
			// Command rows are user-authored but intentionally have no turn-navigation
			// anchor. Preserve the previous bottom-pinning behavior for them.
			shouldAutoScroll = true;
		}

		if (hasNewMessage || hasContentChange) {
			const newest = $activeSession.messages[currentLength - 1];
			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] effect len=${currentLength} prevLen=${prevMessagesLength} newMsg=${hasNewMessage} contentChg=${hasContentChange} autoS=${shouldAutoScroll} pend=${pendingScrollToBottom} newestId=${newest?.id} newestRole=${newest?.role} newestType=${newest?.type} newestStreaming=${newest?.streaming} cLen=${(newest?.content || '').length} SH=${containerRef?.scrollHeight} ST=${containerRef?.scrollTop} CH=${containerRef?.clientHeight}`);
		}

		// Scroll to bottom on content changes when the pure decision says so
		// (stuck to bottom, or a new user message), OR if a session switch is
		// pending scroll-to-bottom (messages just loaded).
		const shouldScroll =
			shouldAutoScrollOnChange(shouldAutoScroll, newUserMessage) || pendingScrollToBottom;
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
				// The newly-sent prompt owns a stable top anchor. Its responsive tail
				// reserve leaves the response room to grow beneath it.
				tick().then(() => scheduleCurrentTurnAnchorMaintenance(true));
			} else if (currentTurnAnchorId) {
				// While the reserve remains, growth consumes blank room without moving
				// the prompt. Once it is exhausted, reveal only the new overflow.
				tick().then(() => scheduleCurrentTurnAnchorMaintenance());
			} else {
				// Normal streaming: single scroll is enough. Re-check shouldAutoScroll
				// in the deferred callback — the user may have scrolled up (wheel/touch
				// disengage) in the frames between scheduling this and it running, and
				// we must not yank them back down after they've released.
				tick().then(() => requestAnimationFrame(() => {
					if (shouldAutoScroll) scrollToBottom();
				}));
			}
		}
		if (!currentTurnAnchorId && (hasNewMessage || hasContentChange)) {
			scheduleLatestTurnTailRefresh();
		}

		// Update previous values
		prevMessagesLength = currentLength;
		prevLastContent = currentContent;
		prevLastMetadata = currentMetadata;
	});
</script>

<div class="transcript-shell">
<div
	bind:this={containerRef}
	onscroll={handleScroll}
	onwheel={handleWheel}
	ontouchstart={handleTouchStart}
	ontouchmove={handleTouchMove}
	role="log"
	aria-label="Conversation messages"
	class="msg-scroll flex-1 overflow-y-auto overscroll-contain"
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
							<span>Agent:</span>
							<span class="text-foreground ml-1">{currentProfile.name}</span>
						</div>
					{/if}
				</div>
			</div>
		</div>
	{:else}
		<!-- Messages -->
		<div class="chat-column">
			{#each displayItems as item, i (displayItemKey(item))}
				<!-- content-visibility: auto lets the browser skip paint/layout for off-screen
				     messages. The last LIVE_TAIL items opt OUT (.msg-live) so the growing
				     streaming region near the viewport bottom is never re-estimated. -->
				<div
					class="msg-virtualized"
					class:msg-live={i >= displayItems.length - LIVE_TAIL}
					id={item.kind === 'message' && item.message.id && isNavigableUserTurn(item.message) ? turnAnchorId(item.message.id) : undefined}
					data-user-turn-id={item.kind === 'message' && item.message.id && isNavigableUserTurn(item.message) ? item.message.id : undefined}
				>
				{#if item.kind === 'work_log'}
					<!-- Routine steps of one turn, folded (see $lib/utils/work-log) -->
					<WorkLogGroup
						mode={item.mode}
						folded={item.folded}
						tail={item.tail}
						sequence={item.sequence}
						durationLabel={item.durationLabel}
						summary={item.summary}
						compactSummary={item.compactSummary}
						row={messageBody}
					/>
				{:else if item.kind === 'system_group'}
					<!-- Grouped system/hook messages (2+ consecutive low-signal events) -->
					<SystemMessageGroup messages={item.messages} />
				{:else}
					{@render messageBody(item.message, item.index)}
				{/if}
			</div>
			{/each}

			<!-- Prompts already sent but not yet delivered to the model. They sit
			     below the transcript, above the activity indicator, until the
			     backend hands them over (mid-turn steer or next-turn start). -->
			{#if pendingMessages.length > 0}
				<PendingMessages
					messages={pendingMessages}
					onRemove={(id) => activeSession.removeQueuedMessage(id)}
					onInterrupt={() => activeSession.stopGeneration()}
				/>
			{/if}

			<!-- The gap predicate guarantees this does not duplicate a prose cursor,
			     running-tool spinner, visible thinking state, or active subagent row. -->
			{#if showGapIndicator}
				<TurnActivity startedAt={liveTurnStartedAt} />
			{/if}

			<!-- Error -->
			{#if $activeSession.error}
				<div class="bg-destructive/10 border border-destructive/30 text-destructive px-4 py-3 rounded-lg flex items-center justify-between shadow-s">
					<span class="text-sm">{$activeSession.error}</span>
					<button onclick={() => activeSession.clearError()} class="text-destructive hover:opacity-80">&times;</button>
				</div>
			{/if}

			<!-- In-flight background tasks: a collapsible line pinned to the history
			     tail (spinner + "N running tasks"; expands to a |__ tree with per-task
			     stop). Renders nothing when there are no tasks. -->
			<ActiveTasks tasks={$activeSession.activeTasks ?? []} onStop={handleStopTask} />

			<!-- Responsive room belongs only to the live edge, never historical turns. -->
			<div
				bind:this={tailSpacerRef}
				class="transcript-tail-spacer"
				style:height={`calc(${Math.max(tailReservePx, latestTurnTailPx)}px + env(safe-area-inset-bottom, 0px))`}
				aria-hidden="true"
			></div>
		</div>
	{/if}
</div>

	{#if showJumpToLatest}
		<div class="jump-latest-layer">
			<button
				type="button"
				class="jump-latest"
				onclick={jumpToLatest}
				aria-label="Jump to latest message"
				title="Jump to latest message"
			>
				<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
					<path d="m6 9 6 6 6-6" />
				</svg>
				<span class="sr-only">Jump to latest message</span>
			</button>
		</div>
	{/if}

	{#if turnNavigationItems.length >= 2}
		<TurnNavigator
			items={turnNavigationItems}
			currentIndex={currentTurnIndex}
			onSelect={navigateToTurn}
		/>
	{/if}
</div>

<!-- The one dispatch table: message → component. Shared with WorkLogGroup so a
     folded row renders exactly as it would inline. -->
{#snippet messageBody(message: ChatMessage, messageIndex: number)}
	{#if message.type === 'command'}
		<!-- Slash command invocation (e.g., /commit, /ralph-loop) -->
		<CommandMessage
			commandName={message.commandName || message.content}
			commandArgs={message.commandArgs}
		/>
	{:else if message.role === 'user'}
		<UserMessage
			content={message.content}
			timestamp={message.timestamp}
			sessionId={$activeSession.sessionId}
			messageUuid={message.jsonlUuid}
			userPromptOccurrence={userPromptOccurrence($activeSession.messages, messageIndex)}
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
		{@const isStreamAnchor = !!(message.streaming && messageIndex === lastStreamingMessageIndex)}
		<!-- Suppress empty phantom assistant-text bubbles. The turn-start
		     synthetic (empty streaming text) is re-appended to the tail during
		     post-tool think-gaps and while parked on a subagent; when it is not
		     the streaming anchor it renders only a bare "Name • time" header —
		     no body, no indicator — the confusing empty "Principal Engineer …"
		     bubble. The single activity signal is the bottom gap dots or the
		     real streaming message's own inline cursor. -->
		{#if shouldRenderAssistantText(message.content, isStreamAnchor)}
			<AssistantMessage
				content={message.content}
				timestamp={message.timestamp}
				messageIndex={messageIndex}
				messageId={message.id}
				messageUuid={message.jsonlUuid}
				sessionId={$activeSession.sessionId}
				streaming={isStreamAnchor}
				{hasPartialMessages}
				{fileCheckpointingEnabled}
				metadata={message.metadata}
				onRewindComplete={handleRewindComplete}
				profileName={getProfileName(message)}
				modelLabel={getModelDisplayName(message)}
			/>
		{/if}
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
	{:else if message.type === 'tool_use' && message.toolName === 'Note'}
		<!-- Fluid Note tool - the todo-style progress row (notes are fluid's plan tool) -->
		{@const notes = extractNotesFromMessage(message)}
		{#if notes && notes.length > 0}
			<div class="w-full">
				<div class="min-w-0">
					<TodoList todos={notes} />
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
	{:else if message.type === 'tool_use' && message.toolName === 'run_python' && message.streaming}
		<!-- A cell that is still STREAMING is the visible work surface: while the
		     model writes it, nothing else has happened yet, and hiding it made the
		     transcript go silent for the whole write. Once it executes, its inner
		     tool cards take over and the finished/succeeded cell hides again. -->
		<ToolUseMessage
			name="Python"
			input={message.toolInput}
			partialInput={message.partialToolInput}
			result={message.toolResult}
			status={message.toolStatus || 'running'}
			streaming={message.streaming}
		/>
	{:else if message.type === 'tool_use' && message.toolName === 'run_python' && message.toolStatus !== 'error'}
		<!-- Code mode: a run_python cell is plumbing, not work. The tool calls it
		     makes are reported by their own cards as they fire, so the cell itself
		     adds a wall of Python and nothing else - while it runs as much as once
		     it succeeds. Only a failed cell keeps its card: that one is a problem
		     worth reading. -->
	{:else if message.type === 'tool_use'}
		<ToolUseMessage
			name={message.toolName || 'Tool'}
			input={message.toolInput}
			partialInput={message.partialToolInput}
			result={message.toolResult}
			status={message.toolStatus || 'running'}
			streaming={message.streaming}
			serverSide={message.serverSide ?? message.server_side ?? false}
			screenshot={message.toolScreenshot}
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
				<SubagentMessage {message} {runningToolIds} />
			</div>
		</div>
	{:else if message.type === 'team_member'}
		<div class="w-full">
			<div class="min-w-0">
				<TeamMemberMessage {message} parentSessionId={$activeSession.sessionId ?? ''} />
			</div>
		</div>
	{/if}
{/snippet}

<!-- Fullscreen media preview overlay (driven by the lightbox store) -->
<MediaLightbox />

<style>
	/* Keep the browser's scroll anchoring active so a resize of off-screen
	   content ABOVE the viewport (e.g. a virtualized message being re-estimated
	   while the tail streams) adjusts scrollTop to hold the visible content
	   still, instead of letting earlier content shift under the reader. This is
	   the default, but we assert it so nothing downstream defeats it. */
	.transcript-shell {
		position: relative;
		flex: 1 1 0%;
		min-width: 0;
		min-height: 0;
	}

	.msg-scroll {
		height: 100%;
		overflow-anchor: auto;
		scroll-padding-block: 24px;
	}

	/* The reading column. A fixed 768px measure keeps prose at a comfortable
	   line length no matter how wide the window gets, and every transcript
	   element (bubbles, work-log rows, subagent cards) aligns to it. */
	.chat-column {
		max-width: 768px;
		margin: 0 auto;
		padding: 26px 24px 0;
		display: flex;
		flex-direction: column;
		gap: 22px;
	}

	@media (max-width: 640px) {
		.chat-column {
			padding: 18px 14px 0;
			gap: 18px;
		}
	}

	/* Browser-native virtualization: skip paint/layout for off-screen messages.
	   `auto` in contain-intrinsic-size remembers the last rendered height so
	   the scrollbar stays stable when scrolling back through history. */
	.msg-virtualized {
		content-visibility: auto;
		contain-intrinsic-size: auto 80px;
	}

	/* The trailing live region (streaming message + immediate predecessors) opts
	   out of content-visibility entirely: it is always on/near screen and
	   actively growing, so virtualizing it re-estimates its height every delta
	   and jitters scrollHeight. Rendering it in full also makes it a valid
	   scroll-anchor target for the pinned-to-bottom path. */
	.msg-virtualized.msg-live {
		content-visibility: visible;
		contain-intrinsic-size: auto;
	}

	.transcript-tail-spacer {
		flex: 0 0 auto;
		min-height: calc(24px + env(safe-area-inset-bottom, 0px));
		pointer-events: none;
	}

	.jump-latest-layer {
		position: absolute;
		z-index: 20;
		left: 50%;
		bottom: 12px;
		transform: translateX(-50%);
		pointer-events: none;
	}

	.jump-latest {
		width: 40px;
		height: 40px;
		display: grid;
		place-items: center;
		padding: 0;
		border: 1px solid color-mix(in oklch, var(--border) 84%, transparent);
		border-radius: 999px;
		background: color-mix(in oklch, var(--background) 86%, transparent);
		color: var(--muted-foreground);
		box-shadow: var(--shadow-m);
		backdrop-filter: blur(12px) saturate(140%);
		-webkit-backdrop-filter: blur(12px) saturate(140%);
		pointer-events: auto;
		cursor: pointer;
		animation: jump-latest-in 160ms ease-out both;
		transition: color 150ms ease, border-color 150ms ease, background-color 150ms ease, transform 150ms ease;
	}

	.jump-latest svg {
		width: 17px;
		height: 17px;
	}

	.jump-latest:hover {
		color: var(--foreground);
		border-color: var(--border-strong, var(--border));
		background: color-mix(in oklch, var(--background) 94%, var(--scrim));
		transform: translateY(-2px);
	}

	.jump-latest:active { transform: translateY(0) scale(0.94); }
	.jump-latest:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
	.jump-latest:disabled { opacity: 0.45; pointer-events: none; }

	@keyframes jump-latest-in {
		from { opacity: 0; transform: translateY(5px) scale(0.94); }
		to { opacity: 1; transform: translateY(0) scale(1); }
	}

	@media (max-width: 639px), (pointer: coarse) {
		.jump-latest-layer { bottom: 8px; }
		.jump-latest { width: 44px; height: 44px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.jump-latest { animation: none; transition: none; }
	}

</style>

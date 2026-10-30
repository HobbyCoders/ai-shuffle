<script lang="ts" module>
	// Reading position per session, kept OUTSIDE the component instance so it
	// survives MessageArea being destroyed and remounted (opening Settings or a
	// full-page panel and coming back). Inside the instance these maps were
	// recreated empty on every mount, so the remount saw "no prior state" and
	// dropped a reader who had scrolled up back at the bottom.
	// Non-reactive so saving a position never triggers effects; capped at 100
	// entries to prevent unbounded growth over long sessions.
	const SCROLL_CACHE_MAX = 100;
	const scrollPositionCache = new Map<string, number>();
	// Whether each session was following the bottom (true) or the reader had
	// scrolled up (false) when it was last on screen.
	const autoScrollCache = new Map<string, boolean>();
	const mountedRangeCache = new Map<string, { start: number; end: number; leadingGap?: number }>();
</script>

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
	import { tick, untrack, onMount, onDestroy, getContext } from 'svelte';
	import { X } from 'lucide-svelte';
	import { derived as derivedStore, get } from 'svelte/store';
	import { activeSession as liveActiveSession, backgroundPermissions } from '$lib/stores/active-session';
	import { TRANSCRIPT_PRESENTATION, type TranscriptPresentation } from './TranscriptHydrationBoundary.svelte';
	import TranscriptLoadingSkeleton from './TranscriptLoadingSkeleton.svelte';
	import { sessionEphemeralCache } from '$lib/stores/session-cache';
	import { createScrollTopPersist, readLeavingScrollTop } from './scroll-top-persist';
	import { privateAccessPending } from '$lib/stores/chat-access';
	import { isTranscriptProseDismissed, proseQuestionDismissals, proseQuestionSessions } from '$lib/stores/session/prose-questions';
	import { needsYouCount, openSessionNeeds } from '$lib/components/layout/session-card-data';
	import { swarms } from '$lib/stores/swarms';
	import type { SwarmSnapshot } from '$lib/utils/swarm';
	import { forkOfferedSessionHere } from './fork-here';
	import { profiles, sessions } from '$lib/stores/app-data.svelte';
	import type { ChatMessage, TodoItem } from '$lib/stores/types';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { STREAM_DEBUG } from '$lib/utils/debug-log';
	import UserMessage from './UserMessage.svelte';
	import { agentDelivery, agentSender, agentSenderNames } from '$lib/utils/agent-delivery';
	import { createAgentMessageProjection } from '$lib/utils/agent-message-projection';
	import AgentMessage from './AgentMessage.svelte';
	import AssistantMessage from './AssistantMessage.svelte';
	import TurnActivity from './TurnActivity.svelte';
	import TurnNavigator from './TurnNavigator.svelte';
	import { findStreamingAnchorIndex, shouldShowGapIndicator, shouldRenderAssistantText } from '$lib/stores/session/gap-indicator';
	import {
		JUMP_TO_LATEST_DELAY_MS,
		MIN_TRANSCRIPT_TAIL_RESERVE,
		isNearBottom,
		isUserScrollUpGesture,
		nextStickToBottom,
		readingAnchorOf,
		resolveAnchoredRevealDelta,
		resolveAnchoredTailReserve,
		resolveCurrentTurnAnchorScrollTop,
		resolveReadingAnchorDelta,
		type ReadingAnchor,
		shouldAutoScrollOnChange,
		shouldPinLatestTurnOnSettle,
		shouldShowJumpToLatest,
		type ScrollMetrics
	} from '$lib/stores/session/auto-scroll';
	import ToolUseMessage from './ToolUseMessage.svelte';
	import HandoffCard from './HandoffCard.svelte';
	import GenerationChoiceCard from './GenerationChoiceCard.svelte';
	import GenerationSavedChoice from './GenerationSavedChoice.svelte';
	import { chooseResult, rememberedResult } from '$lib/utils/generation-choice';
	import { EMPTY_HANDOFFS, settleByOwner } from '$lib/utils/handoff-episodes';
	import { handoffPending, pollHandoffPending, refreshHandoffPending } from '$lib/stores/handoff-pending';
	import { HANDOFF_PROGRESS_POLL_MS, progressForEpisode } from '$lib/utils/handoff-progress';
	import WorkLogGroup from './WorkLogGroup.svelte';
	import {
		createTranscriptModelBuilder,
		createLatestTurnKeyTracker,
		hasLinkedSubagent as hasLinkedSubagentIn,
		type DisplayItem,
		type TranscriptModel
	} from '$lib/utils/work-log';
	import { createSettleHold, createTurnHandoffHold } from '$lib/utils/settle-hold';
	import {
		createTranscriptWindow,
		selectedRowRange,
		dominantReadingIndex,
		transcriptItemCost,
		TRANSCRIPT_CONTENT_BUDGET,
		classifyTurnEntry,
		settleSkippedTurns,
		turnTopsFromVisibility,
		type TranscriptRange,
		type TurnVisibility
	} from './transcript-window';
	import { beforeTranscriptCovered, exposedReadingViewport } from './transcript-reading-area';
	import { classifyTranscriptRender } from '$lib/components/layout/chat-empty-state';
	import { pendingDeepLink, deepLinkLoading } from '$lib/stores/pending-deep-link';
	import { applyPerformanceProfile } from '$lib/utils/perf-profile';
	import {
		createTurnNavigationSignature,
		deriveUserTurnNavigationItems,
		isNavigableUserTurn,
		resolveCurrentTurnIndex,
		resolveLiveTurnStartedAt,
		turnAnchorId,
		needsYouTurnIds
	} from '$lib/utils/transcript-navigation';
	import { getModelLabel } from '$lib/constants/models.svelte';
	import SystemMessageGroup from './SystemMessageGroup.svelte';
	import ThinkingBlock from './ThinkingBlock.svelte';
	import CommandMessage from './CommandMessage.svelte';
	// tool_result messages are always merged into tool_use messages to prevent
	// flickering during streaming, so they have no component of their own.
	import SystemMessage from '$lib/components/SystemMessage.svelte';
	import SubagentMessage from '$lib/components/SubagentMessage.svelte';
	import TeamMemberMessage from '$lib/components/TeamMemberMessage.svelte';
	import TodoList from '$lib/components/TodoList.svelte';
	import NotesList from '$lib/components/NotesList.svelte';
	import { notesFromToolMessage } from '$lib/utils/note-tool';
	import { UI_MOUNT } from '$lib/utils/ui-signature';
	import MediaLightbox from './MediaLightbox.svelte';
	import PendingMessages from './PendingMessages.svelte';
	import PendingStatusAnnouncer from './PendingStatusAnnouncer.svelte';
	import RunningTasksList from './RunningTasksList.svelte';
	import {
		approvalCardData,
		currentAssistantQuestion,
		isApprovalCard,
		isApprovalOutcome
	} from '$lib/utils/assistant-confirm';
	import ApprovalRequestCard from './ApprovalRequestCard.svelte';
	import ApprovalOutcome from './ApprovalOutcome.svelte';

	// Only this subtree can read a frozen outgoing transcript. Mutations and
	// persistence still address the real incoming session below.
	const activeSession = getContext<TranscriptPresentation>(TRANSCRIPT_PRESENTATION) ?? liveActiveSession;

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
	function handleRewindComplete(warning?: string, rewoundPrompt?: string, uploadPaths?: string[]) {
		// If there's a warning (partial success), show it as a system message
		if (warning) {
			liveActiveSession.addSystemMessage(warning, 'rewind_warning');
		}

		// A rewind to a user message removed that very message — queue its text
		// so ChatInput refills the composer after the reload settles, letting the
		// user edit or resend what they rewound away.
		if (rewoundPrompt || uploadPaths?.length) {
			liveActiveSession.setPendingRestorePrompt(rewoundPrompt ?? '', uploadPaths);
		}

		// Clear stale messages from the store before reloading.
		// Without this, the defensive check in the history handler would
		// preserve old (broken) messages when the reload returns fewer/zero.
		liveActiveSession.clearMessages();

		liveActiveSession.reloadSession(true);
	}

	// Capture the originating session when the callback is handed to a row, not
	// when an async rewind later completes against a possibly different target.
	function rewindCompleteFor(sessionId: string | null | undefined) {
		return (warning?: string, prompt?: string, uploads?: string[]) => {
			if (!sessionId || get(liveActiveSession).sessionId !== sessionId) return;
			handleRewindComplete(warning, prompt, uploads);
		};
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
	// Content-driven scroll settle (replaces the old 50 ms polling loop, S11).
	let settleObserver: ResizeObserver | null = null;
	let settleIdleTimer: ReturnType<typeof setTimeout> | null = null;
	let settleDeadline = 0;
	/** Quiet period after the last height change before the settle gives up. */
	const SETTLE_IDLE_MS = 400;
	/** Hard ceiling — a transcript that never stops growing must not pin forever. */
	const SETTLE_MAX_MS = 3000;
	/** The message column: what actually changes height while content renders. */
	let chatColumnRef = $state<HTMLDivElement | null>(null);

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
	 * Scroll to the bottom and KEEP it there until the transcript stops growing.
	 *
	 * Large messages (tables, code blocks, images) take several frames to lay
	 * out, so one scrollToBottom() lands short. This used to be a 50 ms interval
	 * running for up to 3 s, reading `scrollHeight` and writing `scrollTop` on
	 * every tick — a forced layout 20×/s, fired twice per session switch (once
	 * against an EMPTY container, before history had even arrived). See
	 * PERFORMANCE_REVIEW §3.1 S11.
	 *
	 * Now it is event-driven: a ResizeObserver on the message column re-pins
	 * exactly when the content actually changes height, and the settle ends
	 * after a quiet period (or the 3 s ceiling). No polling, no speculative
	 * layout reads.
	 */
	function scrollToBottomUntilStable() {
		if (!containerRef || (!shouldAutoScroll && !pendingScrollToBottom)) return;
		stopScrollSettle();
		scrollToBottom();

		const content = chatColumnRef;
		if (!content || typeof ResizeObserver === 'undefined') {
			// No column mounted yet (or no ResizeObserver): one deferred re-pin.
			requestAnimationFrame(() => requestAnimationFrame(() => {
				if (shouldAutoScroll || pendingScrollToBottom) scrollToBottom();
			}));
			return;
		}

		settleDeadline = Date.now() + SETTLE_MAX_MS;
		// eslint-disable-next-line no-console
		if (STREAM_DEBUG) console.log(`[AS] settle START SH=${containerRef.scrollHeight} ST=${containerRef.scrollTop} msgLen=${$activeSession.messages.length}`);
		settleObserver = new ResizeObserver(() => {
			if (!containerRef || Date.now() > settleDeadline) {
				stopScrollSettle();
				return;
			}
			// The reader took over (wheel/touch/scrollbar): stop fighting them.
			if (!shouldAutoScroll && !pendingScrollToBottom) {
				stopScrollSettle();
				return;
			}
			scrollToBottom();
			armSettleIdleTimer();
		});
		settleObserver.observe(content);
		armSettleIdleTimer();
	}

	/** End the settle once the column has been quiet for SETTLE_IDLE_MS. */
	function armSettleIdleTimer() {
		if (settleIdleTimer) clearTimeout(settleIdleTimer);
		settleIdleTimer = setTimeout(() => {
			settleIdleTimer = null;
			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] settle DONE SH=${containerRef?.scrollHeight} ST=${containerRef?.scrollTop}`);
			stopScrollSettle();
		}, SETTLE_IDLE_MS);
	}

	function stopScrollSettle() {
		if (settleObserver) {
			settleObserver.disconnect();
			settleObserver = null;
		}
		if (settleIdleTimer) {
			clearTimeout(settleIdleTimer);
			settleIdleTimer = null;
		}
	}

	// Row props read this, never the store directly (#1282). Every streaming fold
	// publishes a new `$activeSession` object; a row prop written as
	// `$activeSession.sessionId` re-runs that prop's consumers in EVERY mounted row
	// on every fold (each AssistantMessage re-routes its rendered HTML, each user
	// row re-evaluates its actions). A store derived from it emits only when the id
	// itself changes, so a fold does not even mark the rows for a recheck.
	const activeSessionIdStore = derivedStore(activeSession, ($session) => $session.sessionId);
	const activeSessionId = $derived($activeSessionIdStore);
	const presentedRewindComplete = $derived(rewindCompleteFor(activeSessionId));

	// Current profile for message settings (uses current UI selection)
	const currentProfile = $derived($profiles.find(p => p.id === $activeSession.profile));
	const hasPartialMessages = $derived(currentProfile?.config?.include_partial_messages !== false);

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
	// The approval asked in words that the owner's next yes or no answers
	// (assistant_confirm: the oldest still waiting). Its card sits in the
	// transcript where it was raised, keyed by request id (#483); this only
	// marks which card is the one being asked.
	const assistantQuestion = $derived(currentAssistantQuestion($activeSession.pendingPermissions ?? []));
	const activeApprovalId = $derived(assistantQuestion?.request.request_id ?? null);

	// A request waiting on the owner stands in for the dots while it waits.
	const showGapIndicator = $derived(
		!assistantQuestion
		&& shouldShowGapIndicator(
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

	// Worker task names for attributed agent messages, keyed by session id.
	const agentNames = $derived(agentSenderNames($activeSession.messages, $activeSession.activeTasks ?? []));

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
	//
	// Computed ONCE for the whole transcript by buildTranscriptModel (it already
	// walks the array); this is the O(1) read. It used to be an O(N) scan run
	// per rendered user row per streaming fold — see work-log.ts.
	function userPromptOccurrence(messageIndex: number): number {
		return promptOccurrence[messageIndex] ?? 0;
	}

	const rawLatestTurnUnsettled = $derived(
		$activeSession.isStreaming ||
		Boolean($activeSession.liveTurnId) ||
		($activeSession.activeTasks ?? []).some((task) => task.status === 'running') ||
		($activeSession.pendingPermissions ?? []).length > 0 ||
		($activeSession.pendingQuestions ?? []).some(question => !question.nonblocking)
	);
	// The raw signals drop for a moment between turn.completed and turn.parked,
	// and between a parked turn's last task finishing and its wake turn starting.
	// Folding on that blip collapsed the whole turn and popped it back open; the
	// hold keeps the turn as shown unless it stays settled (see settle-hold.ts).
	let settleHoldTick = $state(0);
	const settleHold = createSettleHold(() => settleHoldTick++);
	onDestroy(() => settleHold.dispose());
	const latestTurnUnsettled = $derived.by(() => {
		void settleHoldTick;
		return settleHold.update({
			sessionId: $activeSession.sessionId,
			unsettled: rawLatestTurnUnsettled,
			parked: $activeSession.turnParked === true
		});
	});
	// That hold covers only the latest turn. A send during it opens a newer turn;
	// the turn it interrupted keeps the rest of its hold rather than folding at once.
	let handoffTick = $state(0);
	const turnHandoff = createTurnHandoffHold(() => handoffTick++);
	onDestroy(() => turnHandoff.dispose());
	// The store emits on every stream delta; the tracker answers a repeated
	// array at once and a streaming fold from the rows near the tail (#1282).
	const latestTurnKeyOf = createLatestTurnKeyTracker();
	const heldTurnKeys = $derived.by(() => {
		void handoffTick;
		return turnHandoff.update({
			sessionId: $activeSession.sessionId,
			latestTurnKey: latestTurnKeyOf($activeSession.messages),
			unsettled: latestTurnUnsettled,
			parked: $activeSession.turnParked === true
		});
	});

	// Memoize the grouped display list by message and lifecycle identity.
	// The active-session store emits a fresh state object on every SSE stream
	// delta (the reducer coalesces deltas into `messages` via rAF). Many of
	// those emissions do NOT change the `messages` array reference — yet without
	// this guard the derived rebuilt the entire O(N) grouped list and returned a
	// new array each time, forcing the {#each} to re-diff every message. That
	// made streaming get progressively choppier as a session grew. A plain
	// object (not $state) avoids Svelte 5 compiling these into reactive signals
	// inside the derived.
	const _displayCache: { messages: ChatMessage[] | null; sessionId?: string | null; swarms?: Record<string, SwarmSnapshot>; isStreaming: boolean; latestTurnUnsettled: boolean; runningToolKey: string; heldTurnKeys: ReadonlySet<string> | null; groupingEnabled: boolean; model: TranscriptModel } =
		{
			messages: null,
			isStreaming: false,
			latestTurnUnsettled: false,
			runningToolKey: '',
			heldTurnKeys: null,
			groupingEnabled: true,
			model: {
				items: [],
				promptOccurrence: [],
				rewindEligible: [],
				subagentToolIds: new Set<string>(),
				itemIndexByTurnId: new Map<string, number>(),
				lastAssistantMetadata: null,
				latestUserTurnId: null,
				midTurnUserIds: new Set<string>(),
				handoffs: EMPTY_HANDOFFS
			}
		};

	const buildTranscript = createTranscriptModelBuilder();
	const agentMessageProjection = createAgentMessageProjection();
	const projectedMessages = $derived(agentMessageProjection.update({
		sessionId: $activeSession.sessionId,
		messages: $activeSession.messages,
		pendingPeerMessages: $activeSession.pendingPeerMessages ?? [],
		cancelledTurnIds: $activeSession.cancelledPeerTurnIds,
		sessionActive: rawLatestTurnUnsettled && $activeSession.turnParked !== true
	}));
	const transcript: TranscriptModel = $derived.by(() => {
		const messages = projectedMessages;
		const sessionId = $activeSession.sessionId;
		const sessionSwarms = $swarms.bySession[sessionId ?? ''];
		const isStreamingNow = $activeSession.isStreaming;
		const latestIsUnsettled = latestTurnUnsettled;
		// A turn whose background work is still running stays unsettled even once
		// a later turn opened (a send or a wake must not fold it). Keyed by value:
		// every /live poll hands over a fresh task list.
		const runningIds = runningToolIds;
		const runningToolKey = [...runningIds].sort().join('\0');
		// Same instance until a hand-off starts or ends (see settle-hold.ts).
		const heldKeys = heldTurnKeys;
		// When grouping is off, groupable tools fall through to the plain
		// single-message path (system grouping is unaffected by this toggle).
		const groupingEnabled = chatPrefs?.groupToolCalls !== false;
		if (
			messages === _displayCache.messages
			&& sessionId === _displayCache.sessionId
			&& sessionSwarms === _displayCache.swarms
			&& isStreamingNow === _displayCache.isStreaming
			&& latestIsUnsettled === _displayCache.latestTurnUnsettled
			&& runningToolKey === _displayCache.runningToolKey
			&& heldKeys === _displayCache.heldTurnKeys
			&& groupingEnabled === _displayCache.groupingEnabled
		) {
			// Same message identities (e.g. a WS-delta-only update) — reuse the
			// cached model so downstream {#each} skips re-diffing.
			return _displayCache.model;
		}
		// Diagnostic: log when displayItems re-evaluates after streaming ends
		if (STREAM_DEBUG && !isStreamingNow && messages.length > 0) {
			const aTexts = messages.filter((m: ChatMessage) => m.role === 'assistant' && m.type === 'text');
			const hasStrmMsg = messages.some((m: ChatMessage) => m.streaming);
			console.debug(`[displayItems] re-eval: ${messages.length} msgs, streaming=${isStreamingNow}, hasStreamingMsg=${hasStrmMsg}, aTexts=${aTexts.length} [${aTexts.map((m: ChatMessage) => `len=${(m.content||'').length}`).join(',')}]`);
		}

		const model = buildTranscript(messages, {
			isStreaming: isStreamingNow,
			latestTurnUnsettled: latestIsUnsettled,
			runningToolIds: runningIds,
			heldTurnKeys: heldKeys,
			sessionId,
			swarms: Object.values(sessionSwarms ?? {}),
			groupingEnabled
		});

		_displayCache.messages = messages;
		_displayCache.sessionId = sessionId;
		_displayCache.swarms = sessionSwarms;
		_displayCache.isStreaming = isStreamingNow;
		_displayCache.latestTurnUnsettled = latestIsUnsettled;
		_displayCache.runningToolKey = runningToolKey;
		_displayCache.heldTurnKeys = heldKeys;
		_displayCache.groupingEnabled = groupingEnabled;
		_displayCache.model = model;
		return model;
	});

	// One presentation model for every chat, the assistant's home included
	// (transcript-organization spec §10.5): no second fold path.
	const displayItems: DisplayItem[] = $derived(transcript.items);
	const itemIndexByTurnId: ReadonlyMap<string, number> = $derived(transcript.itemIndexByTurnId);
	// Per-row reads go through these rather than `transcript` (#1282). The
	// streaming fast path in createTranscriptModelBuilder keeps both arrays by
	// identity while only the tail text grows, so these deriveds do not notify and
	// historic user rows are not re-evaluated on every streaming fold.
	const promptOccurrence = $derived(transcript.promptOccurrence);
	const rewindEligible = $derived(transcript.rewindEligible);
	const midTurnUserIds = $derived(transcript.midTurnUserIds);

	// What this column should DRAW right now (PERFORMANCE_REVIEW §3.1 S3).
	// ChatView already suppresses its own hero while history loads, but the
	// message column it renders instead contains this component — whose empty
	// branch keyed purely on `messages.length === 0` and so flashed the full
	// "Start a Conversation" hero on EVERY session switch, indistinguishable
	// from a brand-new chat, before the incoming transcript popped in. The
	// loading window now draws a transcript-shaped skeleton; a genuinely empty
	// session still gets the real hero.
	const transcriptRender = $derived(
		classifyTranscriptRender({
			sessionId: $activeSession.sessionId,
			// An empty recipient can still have a peer waiting for its first turn.
			messageCount: $activeSession.messages.length + ($activeSession.pendingPeerMessages?.length ?? 0),
			isRestoringSession: $activeSession.isRestoringSession,
			isLoadingSession: $activeSession.isLoadingSession || deepLinkLoading($pendingDeepLink, $activeSession.sessionId),
			historyStalled: !!$activeSession.historyStall
		})
	);

	/** True when a Task/Agent tool row already has a SubagentMessage rendering it. */
	function hasLinkedSubagent(toolId: string | undefined | null): boolean {
		return hasLinkedSubagentIn(transcript, toolId);
	}

	// Transcript-local navigation: one stable anchor per turn (its opening prompt). The left
	// timeline is only mounted when it can move somewhere (two or more turns).
	//
	// deriveUserTurnNavigationItems() normalises and previews every prompt
	// (several regexes per turn) and returns a fresh array. Running that on each
	// streaming fold re-rendered the whole rail 60×/s for a list that only
	// changes when a prompt is added. So a cheap identity signature — turn ids
	// plus prompt lengths, no regex — gates the real derivation AND keeps the
	// array reference stable, which is what stops TurnNavigator re-rendering.
	const _navCache: { signature: string; items: ReturnType<typeof deriveUserTurnNavigationItems> } = {
		signature: '\u0000uninitialised',
		items: []
	};

	// The turn id in it decides whether a prompt opens a turn or was steered
	// into one, and it changes when the server reconciles a send. Incremental:
	// a streaming fold re-reads only the rows near the tail (#1282).
	const navigationSignatureOf = createTurnNavigationSignature();
	const turnNavigationSignature = $derived(navigationSignatureOf($activeSession.messages));

	const turnNavigationItems = $derived.by(() => {
		const signature = turnNavigationSignature;
		if (signature === _navCache.signature) return _navCache.items;
		_navCache.signature = signature;
		_navCache.items = deriveUserTurnNavigationItems($activeSession.messages);
		return _navCache.items;
	});

	/** Stable id vector for the observer-driven rail index (no per-call allocation). */
	const turnNavigationIds = $derived(turnNavigationItems.map((item) => item.id));

	// The open session's "Needs you" count (spec §5), through the same
	// derivation and signals as its sidebar row and the tab title: the real
	// row (a mirror's owner state counts before its queues load), the
	// background attention entry a reconnect can restore, private access and
	// the session's own queues and prose question.
	const openSessionId = $derived($activeSession.sessionId);
	const openRow = $derived(openSessionId ? $sessions.find((s) => s.id === openSessionId) ?? null : null);
	// Each hand-off renders as one row on its first record (handoff-episodes.ts);
	// later records patch the episode, so only the anchor rows re-read this.
	// The source worker never sees `moved`: the session row CHANGING to the
	// target settles a move that is sending (settleByOwner keeps identity).
	const handoffOwnerId = $derived(openRow?.owner_device_id ?? null);
	const handoffOwnerName = $derived(openRow?.device_name ?? null);
	const handoffPendingView = $derived(openSessionId ? $handoffPending[openSessionId] ?? null : null);
	const handoffAnchors = $derived(
		settleByOwner(transcript.handoffs, { deviceId: handoffOwnerId, deviceName: handoffOwnerName }, openSessionId, handoffPendingView).byAnchor
	);
	// The owning worker's pending move: read again whenever the open identified
	// move, its phase or the session's owner changes (a finished move then
	// reads null from its new owner, also after a reload).
	const openHandoff = $derived(transcript.handoffs.open);
	const handoffWatchKey = $derived(
		openSessionId && openHandoff?.id && openHandoff.phase === 'sending'
			? `${openHandoff.id}\0${handoffOwnerId ?? ''}`
			: null
	);
	$effect(() => {
		const sid = openSessionId;
		const key = handoffWatchKey;
		if (sid && key) untrack(() => void refreshHandoffPending(sid, key));
	});
	// While it sends, keep reading it: the answer carries the receiver's step
	// and elapsed time for the card (pending.progress).
	$effect(() => {
		const sid = openSessionId;
		const key = handoffWatchKey;
		if (!sid || !key) return;
		return pollHandoffPending(sid, key, HANDOFF_PROGRESS_POLL_MS);
	});
	const openIsRemote = $derived(!!$activeSession.isRemote);
	const openNeeds = $derived(openSessionNeeds(
		$activeSession,
		chatPrefs?.groupToolCalls !== false,
		!!openSessionId && $proseQuestionSessions.has(openSessionId),
		isTranscriptProseDismissed($proseQuestionDismissals, openSessionId, $activeSession.messages)
	));
	const openNeedsYou = $derived(
		openSessionId
			? needsYouCount(
				openRow ?? { status: 'active', is_remote: openIsRemote, remote_runtime_state: null },
				$backgroundPermissions.get(openSessionId),
				$privateAccessPending.get(openSessionId) ?? 0,
				openNeeds
			)
			: 0
	);
	// Reference-stable between folds that leave them alone, so the tick
	// lookup below reruns only when the transcript or the questions change.
	const openMessages = $derived($activeSession.messages);
	const openPendingQuestions = $derived($activeSession.pendingQuestions);
	const pendingQuestionToolIds = $derived((openPendingQuestions ?? []).map((q) => q.tool_use_id).filter(Boolean));
	const newestTurnId = $derived(turnNavigationItems.at(-1)?.id ?? null);
	// The ticks carrying the amber dot: a pending question on the turn that
	// asked it, everything else on the newest turn.
	const needsYouTurns = $derived(needsYouTurnIds(openNeedsYou, openMessages, pendingQuestionToolIds, newestTurnId));

	// A bounded slice, not an idle queue that eventually mounts all history.
	// Explicit navigation remains available while streaming; hidden views do no
	// optional mounting. A keyed DOM anchor survives eviction at either edge.
	function transcriptIndexOfRow(row: HTMLElement): number {
		const key = row.dataset.transcriptKey;
		const index = Number(row.dataset.transcriptIndex);
		return displayItems[index] && displayItemKey(displayItems[index]) === key ? index
			: displayItems.findIndex(item => displayItemKey(item) === key);
	}
	function pinnedTranscriptRange(): TranscriptRange | null {
		return selectedRowRange(chatColumnRef, ':scope > [data-transcript-key]', transcriptIndexOfRow);
	}
	let readingTranscriptKey: string | null = null;
	function readingTranscriptIndex(): number | null {
		if (!containerRef || !chatColumnRef) return null;
		const viewport = exposedReadingViewport(containerRef.getBoundingClientRect());
		if (viewport.bottom <= viewport.top) {
			const previous = displayItems.findIndex(item => displayItemKey(item) === readingTranscriptKey);
			return previous < 0 ? null : previous;
		}
		const reading = dominantReadingIndex(
			chatColumnRef.querySelectorAll<HTMLElement>(':scope > [data-transcript-key]'),
			viewport, transcriptIndexOfRow
		);
		if (reading !== null) readingTranscriptKey = displayItemKey(displayItems[reading]);
		return reading;
	}
	// One lazy capture when a sheet opens, before its coverage is registered.
	// Full-screen covers reuse this stable key; no scroll/token observer is added.
	onMount(() => beforeTranscriptCovered(() => {
		if (!document.hidden) untrack(readingTranscriptIndex);
	}));
	const transcriptWindow = createTranscriptWindow({
		// Real row geometry is kept: no estimated-height placeholders that move
		// the reader after anchor restoration. Dense code/table replies consume
		// more of the bounded range than a short prompt or a collapsed tool.
		cost: import.meta.env.VITE_PERF_TRANSCRIPT_CONTENT_WINDOW !== '0'
			? (index) => transcriptItemCost(displayItems[index]) : undefined,
		budget: TRANSCRIPT_CONTENT_BUDGET,
		getPinnedRange: pinnedTranscriptRange,
		getReadingIndex: readingTranscriptIndex
	});
	let selectionBlocksNavigation = $state(false);
	$effect(() => {
		if (!selectionBlocksNavigation) return;
		const changed = () => { if (!pinnedTranscriptRange()) selectionBlocksNavigation = false; };
		document.addEventListener('selectionchange', changed);
		return () => document.removeEventListener('selectionchange', changed);
	});
	function clearTranscriptSelection() {
		document.getSelection()?.removeAllRanges();
		selectionBlocksNavigation = false;
		void tick().then(() => chatColumnRef?.querySelector<HTMLButtonElement>('.earlier-button')?.focus({ preventScroll: true }));
	}
	let mountedRange = $state<TranscriptRange>({ start: 0, end: 0 });
	const mountedStart = $derived(mountedRange.start);
	const mountedEnd = $derived(mountedRange.end);
	const visibleItems = $derived(displayItems.slice(mountedStart, mountedEnd));
	let windowSessionKey: string | undefined;
	let windowShiftVersion = 0;
	let windowShifting = false;
	let revealedTurnId = $state<string | null>(null);
	let anchorRounding: { scrollTop: number; error: number } | null = null;
	// Space kept above the window when evicting leading rows would need a
	// negative scrollTop (#889): at scroll origin the compensation clamps and
	// the reader moves up. Measured per shift; every later shift re-anchors.
	let leadingGap = $state(0);

	function applyMountedRange(next: TranscriptRange, preserveAnchor = true, keepLeadingSpace = false) {
		// Once the budget was reached, keep the clear affordance stable until
		// the native selection ends, even if this reprice itself fits the window.
		selectionBlocksNavigation = transcriptWindow.selectionLimited ||
			(selectionBlocksNavigation && !!pinnedTranscriptRange());
		if (selectionBlocksNavigation) shouldAutoScroll = false;
		if (!preserveAnchor) anchorRounding = null;
		if (next.start === mountedStart && next.end === mountedEnd) return;
		const container = containerRef;
		const top = container?.getBoundingClientRect().top ?? 0;
		const candidates = preserveAnchor && container
			? Array.from(container.querySelectorAll<HTMLElement>('[data-transcript-index]'))
				.filter(node => {
					const index = Number(node.dataset.transcriptIndex);
					return index >= next.start && index < next.end;
				})
			: [];
		const anchor = candidates.find(node => node.getBoundingClientRect().bottom > top) ?? candidates.at(-1);
		const anchorTop = anchor?.getBoundingClientRect().top;
		// Browsers quantize scrollTop. Separate evictions must not accumulate
		// three <0.5px errors into a >1px reading jump. Carry one subpixel debt
		// only while the scroll position is untouched; user movement resets it.
		const roundingError = anchor && anchorRounding && container?.scrollTop === anchorRounding.scrollTop
			? anchorRounding.error : 0;
		// Oversize adjacent bodies may genuinely have no overlap. In that case
		// absolute scrollTop belongs to the evicted DOM, not the incoming reply.
		const boundaryAtStart = next.start >= mountedEnd;
		const boundaryIndex = preserveAnchor && !anchor
			? boundaryAtStart ? next.start : next.end <= mountedStart ? next.end - 1 : null
			: null;
		const evictsLeading = next.start > mountedStart;
		const shiftSessionKey = windowSessionKey;
		const version = ++windowShiftVersion;
		windowShifting = true;
		mountedRange = next;
		leadingGap = 0;
		if (windowSessionKey) {
			mountedRangeCache.set(windowSessionKey, next);
			evictOldest(mountedRangeCache, SCROLL_CACHE_MAX);
		}
		void tick().then(async () => {
			if (version !== windowShiftVersion) return;
			if (container && container === containerRef) {
				if (preserveAnchor) {
					// Evicting a covered tail can shorten the scroll extent. Publish
					// the EXISTING latest-turn reserve before restoring the key, not
					// in the column ResizeObserver's next animation frame: otherwise
					// scrollTop clamps for a painted frame and the reader jumps back.
					refreshLatestTurnTailReserve();
					await tick();
					if (version !== windowShiftVersion || container !== containerRef) return;
				}
				let delta: number | undefined;
				if (anchor?.isConnected && anchorTop !== undefined) {
					delta = anchor.getBoundingClientRect().top - anchorTop;
				} else if (boundaryIndex !== null) {
					const boundary = container.querySelector<HTMLElement>(`[data-transcript-index="${boundaryIndex}"]`);
					if (boundary) {
						const row = boundary.getBoundingClientRect();
						const top = container.getBoundingClientRect().top + container.clientTop;
						delta = boundaryAtStart ? row.top - top : row.bottom - top - container.clientHeight;
					}
				}
				if (delta !== undefined) {
					// Reading preservation (not explicit paging) keeps a dropped
					// leading row's space instead of clamping at scroll origin.
					const shortfall = -(container.scrollTop + delta + roundingError);
					if (keepLeadingSpace && evictsLeading && anchor && shortfall >= 0.5 && shiftSessionKey === windowSessionKey) {
						leadingGap = shortfall;
						// Saved with the range so a chat switch-back restores the same
						// geometry its cached scrollTop was measured against.
						if (shiftSessionKey) mountedRangeCache.set(shiftSessionKey, { ...next, leadingGap: shortfall });
						await tick();
						if (version !== windowShiftVersion || container !== containerRef) return;
						delta = -container.scrollTop - roundingError;
					}
					markProgrammaticScrollSettling();
					const previousScrollTop = container.scrollTop;
					const scrollDelta = delta + roundingError;
					container.scrollTop += scrollDelta;
					const error = scrollDelta - (container.scrollTop - previousScrollTop);
					// Never carry a real clamp/navigation boundary into a later shift.
					anchorRounding = anchor?.isConnected && Math.abs(error) < 1
						? { scrollTop: container.scrollTop, error } : null;
					lastKnownScrollTop = container.scrollTop;
					lastScrollMetrics = readScrollMetrics();
				}
			}
			windowShifting = false;
		});
	}

	function loadOlderItems() {
		disengageAutoScroll();
		applyMountedRange(transcriptWindow.loadOlder());
	}
	function loadNewerItems() {
		applyMountedRange(transcriptWindow.extendNewer());
	}

	async function ensureItemMounted(itemIndex: number): Promise<void> {
		if (itemIndex < 0 || (itemIndex >= mountedStart && itemIndex < mountedEnd)) return;
		applyMountedRange(transcriptWindow.windowAround(itemIndex), false);
		await tick();
	}

	$effect.pre(() => {
		const sessionKey = $activeSession.sessionId ?? 'new';
		const count = displayItems.length;
		const streaming = $activeSession.isStreaming;
		untrack(() => {
			const isNewSession = sessionKey !== windowSessionKey;
			if (isNewSession) { readingTranscriptKey = null; leadingGap = 0; }
			const saved = isNewSession ? mountedRangeCache.get(sessionKey) : undefined;
			windowSessionKey = sessionKey;
			transcriptWindow.onStreamingChange(streaming);
			let next = transcriptWindow.setItemCount(count, {
				followTail: isNewSession || shouldAutoScroll,
				hidden: typeof document !== 'undefined' && document.hidden
			});
			// Restoring a remount uses the same bounded coordinate system as the
			// saved scrollTop, never a full-history mount.
			if (isNewSession && saved && autoScrollCache.get(sessionKey) === false && count > 0) {
				next = transcriptWindow.restoreRange(saved);
			}
			const reading = !isNewSession && !shouldAutoScroll;
			applyMountedRange(next, reading, reading);
			if (isNewSession && saved?.leadingGap && next.start === saved.start && next.end === saved.end) leadingGap = saved.leadingGap;
		});
	});
	let currentTurnIndex = $state(0);
	let manualNavigationReleaseUntil = 0;
	let settleSessionId: string | null | undefined = undefined;
	let wasLatestTurnUnsettled = false;

	// Anchored to the prompt that OPENED the live turn: a message steered into a
	// turn already running must not restart its clock.
	const liveTurnStartedAt = $derived.by(() => {
		if (!$activeSession.isStreaming) return null;
		return resolveLiveTurnStartedAt($activeSession.messages, $activeSession.liveTurnId);
	});

	// ── Turn tracking (PERFORMANCE_REVIEW §3.2 R12) ────────────────────────
	// The rail's current tick used to be resolved by reading a
	// getBoundingClientRect() for EVERY turn anchor — from the rAF scroll
	// handler, from the ResizeObserver, and after every programmatic scroll.
	// That is O(turns) forced layout per scroll frame, and reading the rect of a
	// `content-visibility: auto` row can force the browser to size it.
	//
	// An IntersectionObserver reports crossings instead: its root is clipped to
	// the band between the container top and the reading line, so an anchor is
	// "inside" exactly while it sits in that band, and the callback's own rects
	// classify the rest. Scrolling then costs O(1) amortised, with zero layout
	// reads on the scroll path. The rect-scan below is kept as the fallback for
	// environments without IntersectionObserver.
	const turnVisibility = new Map<string, TurnVisibility>();
	let turnObserver: IntersectionObserver | null = null;
	let observedReadingLine = 0;
	/** Bumped by the container ResizeObserver: the reading line is height-derived. */
	let viewportHeight = $state(0);

	function readingLineFor(height: number): number {
		return Math.min(140, height * 0.3);
	}

	function handleTurnIntersect(entries: IntersectionObserverEntry[]) {
		const reported = new Set<string>();
		for (const entry of entries) {
			const id = (entry.target as HTMLElement).dataset.userTurnId;
			if (!id) continue;
			const next = classifyTurnEntry(
				entry.isIntersecting,
				entry.boundingClientRect.top,
				entry.rootBounds?.top,
				turnVisibility.get(id)
			);
			if (next) turnVisibility.set(id, next);
			reported.add(id);
		}
		settleSkippedTurns(turnNavigationIds, turnVisibility, reported);
		updateCurrentTurnIndex();
	}

	// Work-log disclosures remount their retained rows on open/close and settle.
	// Register those anchors without rebuilding the observer on every stream delta.
	function observeTurnAnchor(anchor: HTMLElement) {
		if (anchor.dataset.userTurnId) turnObserver?.observe(anchor);
		return { destroy() {
			turnObserver?.unobserve(anchor);
			if (anchor.dataset.userTurnId) turnVisibility.delete(anchor.dataset.userTurnId);
		} };
	}

	/** (Re)build the observer for the mounted anchors and the current geometry. */
	function syncTurnObserver() {
		if (!containerRef || typeof IntersectionObserver === 'undefined') return;
		const container = containerRef;
		const height = container.clientHeight;
		observedReadingLine = readingLineFor(height);
		const bottomInset = Math.max(0, Math.round(height - observedReadingLine));
		turnObserver?.disconnect();
		turnObserver = new IntersectionObserver(handleTurnIntersect, {
			root: container,
			rootMargin: `0px 0px -${bottomInset}px 0px`,
			threshold: 0
		});
		for (const anchor of container.querySelectorAll<HTMLElement>('[data-user-turn-id]')) {
			turnObserver.observe(anchor);
		}
	}

	// Re-observe when the mounted anchor set changes (a new turn, a window
	// expansion) or when the container is resized (the reading line moves).
	// NOT on every fold: streaming deltas change neither.
	$effect(() => {
		if (!containerRef || !isMounted) return;
		if (typeof IntersectionObserver === 'undefined') return;
		const key = `${mountedStart}:${mountedEnd}:${turnNavigationSignature}:${viewportHeight}`;
		void key;
		void tick().then(() => untrack(() => syncTurnObserver()));
	});

	function updateCurrentTurnIndex() {
		if (Date.now() < manualNavigationReleaseUntil) return;
		if (!containerRef || turnNavigationItems.length === 0) {
			currentTurnIndex = 0;
			return;
		}
		if (turnObserver) {
			const readingLine = observedReadingLine || readingLineFor(containerRef.clientHeight);
			const tops = turnTopsFromVisibility(
				turnNavigationIds,
				turnVisibility,
				readingLine,
				(turnId) => {
					const itemIndex = itemIndexByTurnId.get(turnId);
					return itemIndex === undefined || itemIndex >= mountedStart;
				}
			);
			currentTurnIndex = resolveCurrentTurnIndex(tops, readingLine);
			return;
		}
		const containerTop = containerRef.getBoundingClientRect().top;
		const readingLine = readingLineFor(containerRef.clientHeight);
		const tops = turnNavigationItems.map((item) => {
			const anchor = document.getElementById(turnAnchorId(item.id));
			return anchor && containerRef?.contains(anchor)
				? anchor.getBoundingClientRect().top - containerTop
				: Number.POSITIVE_INFINITY;
		});
		currentTurnIndex = resolveCurrentTurnIndex(tops, readingLine);
	}

	async function navigateToTurn(index: number) {
		if (!containerRef) return;
		const item = turnNavigationItems[index];
		if (!item) return;
		// Stop follow mode before mounting: concurrent deltas must not undo the
		// jump. Nested work logs reveal their own bounded row slice as well.
		disengageAutoScroll();
		revealedTurnId = null;
		await tick();
		revealedTurnId = item.id;
		const itemIndex = itemIndexByTurnId.get(item.id);
		if (itemIndex !== undefined) await ensureItemMounted(itemIndex);
		await tick();
		if (!containerRef) return;
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
				updateCurrentTurnIndex();
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
		updateCurrentTurnIndex();
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
		const latestTurnId = transcript.latestUserTurnId;
		if (!latestTurnId) {
			if (latestTurnTailPx !== MIN_TRANSCRIPT_TAIL_RESERVE) latestTurnTailPx = MIN_TRANSCRIPT_TAIL_RESERVE;
			return;
		}
		const anchor = document.getElementById(turnAnchorId(latestTurnId));
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
		applyMountedRange(transcriptWindow.toTail(), false);
		if (transcriptWindow.selectionLimited) return;
		revealedTurnId = null;
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
		// Deferred mount/resize/settle callbacks may outlive the user's release.
		if (selectionBlocksNavigation || (!shouldAutoScroll && !pendingScrollToBottom)) return;
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
					updateCurrentTurnIndex();
				});
			});
		}
	}

	// Trailing write of the reading position into the session store once
	// scrolling pauses (#1282). A switch writes the leaving session's position
	// into its own saved snapshot, never into the store of the one entered.
	const scrollTopPersist = createScrollTopPersist({
		delayMs: 250,
		currentKey: () => $liveActiveSession.sessionId ?? 'new',
		writeCurrent: (position) => liveActiveSession.setScrollTop(position),
		writeLeft: (key, position) => sessionEphemeralCache.patchScrollTop(key, position)
	});

	// #865: a width change (phone rotation, window resize) reflows every row
	// above the reader at the same scrollTop. The browser's own scroll anchoring
	// does not cover it: the column's padding/gap change at the phone breakpoint
	// suppresses it in Chromium, and iOS WebKit has none. So the row under the
	// top edge is remembered per scroll frame, at the width it was measured at,
	// and put back where it was when the container observer sees a new width.
	let readingAnchor: (ReadingAnchor & { width: number }) | null = null;
	let observedWidth = 0;
	let readingAnchorRaf: number | null = null;

	function captureReadingAnchor() {
		if (!containerRef || !chatColumnRef) return;
		// Not reading mid-transcript (following the bottom or the live turn):
		// forget the old row so a later width change can't jump back to it.
		if (shouldAutoScroll || currentTurnAnchorId) { readingAnchor = null; return; }
		const width = containerRef.clientWidth;
		// A width the observer has not handled yet: this layout is already
		// reflowed, so it no longer says where the reader was. Keep the old one.
		if (width !== observedWidth) return;
		const viewTop = containerRef.getBoundingClientRect().top;
		const rows = chatColumnRef.querySelectorAll<HTMLElement>(':scope > [data-transcript-key]');
		let low = 0, high = rows.length - 1, found = -1;
		while (low <= high) {
			const mid = (low + high) >> 1;
			if (rows[mid].getBoundingClientRect().bottom > viewTop + 1) { found = mid; high = mid - 1; } else low = mid + 1;
		}
		const row = rows[found];
		if (!row?.dataset.transcriptKey) { readingAnchor = null; return; }
		const box = row.getBoundingClientRect();
		readingAnchor = { ...readingAnchorOf(row.dataset.transcriptKey, box.top, box.height, viewTop), width };
	}

	function scheduleReadingAnchorCapture() {
		if (readingAnchorRaf !== null) return;
		readingAnchorRaf = requestAnimationFrame(() => {
			readingAnchorRaf = null;
			captureReadingAnchor();
		});
	}

	/** Container observer: after a width change, put the reader back on their row. */
	function restoreReadingAnchorOnWidthChange() {
		if (!containerRef) return;
		const width = containerRef.clientWidth;
		const changed = observedWidth > 0 && width !== observedWidth;
		observedWidth = width;
		const anchor = readingAnchor;
		if (changed && anchor && anchor.width !== width && !shouldAutoScroll && !currentTurnAnchorId && chatColumnRef) {
			const row = chatColumnRef.querySelector<HTMLElement>(`:scope > [data-transcript-key="${CSS.escape(anchor.key)}"]`);
			if (row) {
				const box = row.getBoundingClientRect();
				const delta = resolveReadingAnchorDelta(anchor, box.top, box.height, containerRef.getBoundingClientRect().top);
				if (Math.abs(delta) >= 1) {
					setProgrammaticScrollTop(containerRef.scrollTop + delta);
					lastScrollMetrics = readScrollMetrics();
				}
			}
		}
		scheduleReadingAnchorCapture();
	}

	function handleScroll() {
		if (!containerRef) return;
		if (anchorRounding && containerRef.scrollTop !== anchorRounding.scrollTop) anchorRounding = null;
		scheduleReadingAnchorCapture();
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

			// Shift only in the reader's direction, never in response to our own
			// anchor restoration or a rail jump.
			if (!windowShifting && Date.now() >= manualNavigationReleaseUntil) {
				const previousTop = lastScrollMetrics?.scrollTop ?? currentScrollTop;
				if (currentScrollTop < previousTop && mountedStart > 0 && currentScrollTop < 900) {
					loadOlderItems();
				} else if (currentScrollTop > previousTop && mountedEnd < displayItems.length &&
					containerRef.scrollHeight - currentScrollTop - containerRef.clientHeight < 900) {
					loadNewerItems();
				}
			}

			// Save scroll position to non-reactive cache (doesn't trigger effects)
			scrollPositionCache.set(cacheKey, currentScrollTop);
			// Also save to session store for cross-session persistence — but not
			// per frame: every write publishes a new active-session object to all
			// of its subscribers (#1282). Restores read the cache above first.
			scrollTopPersist.schedule(cacheKey, currentScrollTop);

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
				// Reaching the live edge while text is selected must not re-arm
				// follow mode and re-fill the pinned reading window underneath it.
				const resolvedNext = Date.now() < manualNavigationReleaseUntil || mountedEnd < displayItems.length || pinnedTranscriptRange() !== null ? false : next;
				// eslint-disable-next-line no-console
				if (STREAM_DEBUG && resolvedNext !== shouldAutoScroll) console.log(`[AS] handleScroll ${resolvedNext ? 'ENABLE' : 'DISABLE'} currST=${currentScrollTop} prevST=${prev.scrollTop} SH=${currentMetrics.scrollHeight} CH=${currentMetrics.clientHeight}`);
				const leftBottom = shouldAutoScroll && !resolvedNext;
				shouldAutoScroll = resolvedNext;
				lastScrollMetrics = currentMetrics;
				// #865: the capture queued by this scroll ran while still following
				// the bottom; take the reading row now that the reader has left it.
				if (leftBottom) scheduleReadingAnchorCapture();
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
		// Stop any in-flight scroll settle so it can't keep fighting the user
		// after they've expressed intent to read earlier content.
		stopScrollSettle();
		pendingScrollToBottom = false;
		shouldAutoScroll = false;
		// eslint-disable-next-line no-console
		if (STREAM_DEBUG) console.log(`[AS] disengageAutoScroll (user gesture) ST=${containerRef?.scrollTop} SH=${containerRef?.scrollHeight} CH=${containerRef?.clientHeight}`);
	}

	// Wheel/trackpad: an upward wheel is an unambiguous intent to read earlier
	// content. Passive — we never preventDefault, just observe direction.
	function handleWheel(e: WheelEvent) {
		if (isUserScrollUpGesture(e.deltaY, 0)) disengageAutoScroll();
		// At the boundary the browser cannot emit another scroll event. This
		// also covers a gesture arriving during the last programmatic frame.
		if (e.deltaY < 0 && containerRef && containerRef.scrollTop <= 1 && mountedStart > 0 && !windowShifting) loadOlderItems();
	}

	// Touch: accumulate downward travel from the gesture's lowest clientY.
	// Comparing only adjacent events misses slow drags made of many <8px steps.
	// Reset the baseline when direction reverses so a later scroll-up also releases.
	let touchLastY = 0;
	function handleTouchStart(e: TouchEvent) {
		touchLastY = e.touches[0]?.clientY ?? 0;
	}
	function handleTouchMove(e: TouchEvent) {
		const y = e.touches[0]?.clientY ?? touchLastY;
		const touchDeltaY = y - touchLastY; // finger moved down (>0) ⇒ scrolling up
		touchLastY = Math.min(touchLastY, y);
		if (isUserScrollUpGesture(0, touchDeltaY)) {
			disengageAutoScroll();
			if (containerRef && containerRef.scrollTop <= 1 && mountedStart > 0 && !windowShifting) loadOlderItems();
			touchLastY = y;
		}
	}

	/**
	 * Put a reader who had scrolled up back at their saved offset. The range
	 * effect has already restored the same bounded slice the offset was measured
	 * in. Never mount all history to restore a reading position.
	 */
	function restoreReadingPosition(position: number) {
		shouldAutoScroll = false;
		programmaticScrollDepth++;
		void tick().then(() => {
			if (!containerRef) {
				programmaticScrollDepth = Math.max(0, programmaticScrollDepth - 1);
				return;
			}
			containerRef.scrollTop = position;
			lastKnownScrollTop = containerRef.scrollTop;
			lastScrollMetrics = readScrollMetrics();
			requestAnimationFrame(() => {
				programmaticScrollDepth = Math.max(0, programmaticScrollDepth - 1);
				updateJumpToLatestVisibility();
				updateCurrentTurnIndex();
			});
		});
	}

	// Restore scroll position only on mount - not on every reactive update
	onMount(() => {
		isMounted = true;
		// Stamps data-fx="low" on <html> for the compositing-budget switch in
		// app.css (R13). Idempotent — the first component to mount wins.
		applyPerformanceProfile();
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
					restoreReadingPosition(storedPosition);
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
		applyMountedRange(transcriptWindow.pauseWhenHidden(document.hidden), !shouldAutoScroll);
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
		stopScrollSettle();
		windowShiftVersion++;
		turnObserver?.disconnect();
		turnObserver = null;
		clearJumpRevealTimer();
		if (anchorMaintenanceRaf !== null) cancelAnimationFrame(anchorMaintenanceRaf);
		if (readingAnchorRaf !== null) cancelAnimationFrame(readingAnchorRaf);
		const cacheKey = $activeSession.sessionId ?? 'new';
		scrollTopPersist.cancel();

		if (containerRef) {
			// eslint-disable-next-line no-console
			if (STREAM_DEBUG) console.log(`[AS] onDestroy saving cacheKey=${cacheKey} ST=${containerRef.scrollTop} autoS=${shouldAutoScroll} SH=${containerRef.scrollHeight} CH=${containerRef.clientHeight}`);
			// A container already out of the layout reads 0: keep the reader's
			// last captured position then (readLeavingScrollTop).
			const leftAt = readLeavingScrollTop(containerRef, scrollPositionCache.get(cacheKey));
			if (leftAt !== undefined) {
				scrollPositionCache.set(cacheKey, leftAt);
				scrollTopPersist.destroy(cacheKey, leftAt);
			}
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
			// Anchors from the old transcript are gone; their cached visibility
			// must not decide the new session's rail position.
			turnVisibility.clear();
		}

		// eslint-disable-next-line no-console
		if (STREAM_DEBUG) console.log(`[AS] session-switch effect from=${prevId} to=${currentId} msgLen=${$activeSession.messages.length} lastId=${$activeSession.messages[$activeSession.messages.length - 1]?.id} lastRole=${$activeSession.messages[$activeSession.messages.length - 1]?.role} SH=${containerRef?.scrollHeight} ST=${containerRef?.scrollTop} CH=${containerRef?.clientHeight}`);

		// Save scroll state for the old session (if any). This effect runs after
		// the DOM already shows the session entered, so the container's
		// scrollTop is NOT the leaving session's: a short or still-loading
		// transcript has clamped it, often to 0. Save the position captured from
		// the reader's last scroll frame instead (the per-frame cache holds the
		// same value when no deferred write is pending).
		if (prevId) {
			// The store already holds the session being entered: the position goes
			// into the snapshot the switcher saved for the leaving session.
			const leftAt = scrollTopPersist.leave(prevId, scrollPositionCache.get(prevId));
			if (leftAt !== undefined) scrollPositionCache.set(prevId, leftAt);
			// "Scrolled up" only when the reader's last scroll frame was away from
			// the bottom too, so a flag a loading layout shift flipped by mistake
			// cannot strand the chat above its bottom on the way back (#805).
			autoScrollCache.set(prevId, shouldAutoScroll || (lastScrollMetrics !== null && isNearBottom(lastScrollMetrics)));
			// Prevent unbounded growth over long usage
			evictOldest(scrollPositionCache, SCROLL_CACHE_MAX);
			evictOldest(autoScrollCache, SCROLL_CACHE_MAX);
		} else {
			scrollTopPersist.cancel();
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

		// A chat the reader left scrolled up comes back at the same spot (#805),
		// when its transcript is already here: the range effect restored the
		// slice that offset was measured in. Anything else opens at the bottom.
		// The message tracking takes the current tail as seen, so the change
		// effect does not read the shown transcript as a new prompt to follow.
		const switchedState = $activeSession;
		const returnTo = autoScrollCache.get(currentId ?? 'new') === false && currentId
			? scrollPositionCache.get(currentId) ?? switchedState.scrollTop
			: 0;
		if (returnTo > 0 && switchedState.messages.length > 0 && !switchedState.isLoadingSession && !switchedState.isRestoringSession) {
			pendingScrollToBottom = false;
			prevMessagesLength = switchedState.messages.length;
			prevLastContent = untrack(() => lastMessageContent);
			prevLastMetadata = untrack(() => lastMessageMetadata);
			restoreReadingPosition(returnTo);
			return;
		}

		// Otherwise scroll to bottom on session switch.
		pendingScrollToBottom = true;
		shouldAutoScroll = true;
		// Reset message tracking so the message-change effect fires even if
		// the new session has the same message count as the previous one.
		prevMessagesLength = -1;
		prevLastContent = '';
		prevLastMetadata = null;

		// While the new session's history is still in flight the column is EMPTY:
		// settling it is layout work against nothing, and pendingScrollToBottom
		// already guarantees a settle once the transcript lands (S11). Sessions
		// whose transcript is already in the store still settle immediately.
		tick().then(() => {
			if (!containerRef) return;
			const state = $activeSession;
			if (state.isLoadingSession || state.isRestoringSession || state.messages.length === 0) {
				return;
			}
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

		resizeObserver = new ResizeObserver((entries) => {
			restoreReadingAnchorOnWidthChange();
			// The reading line is a fraction of the viewport height, so the turn
			// observer's root margin has to be rebuilt when the container resizes.
			const height = entries[0]?.contentRect.height;
			if (typeof height === 'number' && Math.abs(height - viewportHeight) >= 1) {
				viewportHeight = height;
			}
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

	// Follow rendered height, not just the last message's data. Tool results,
	// work-log folds and asynchronous Markdown can lay out after the content
	// effect's scroll, without changing the viewport or last-message signal.
	$effect(() => {
		if (!chatColumnRef || !containerRef || !isMounted) return;
		let raf: number | null = null;
		const observer = new ResizeObserver(() => {
			if (raf !== null) return;
			raf = requestAnimationFrame(async () => {
				raf = null;
				if (!containerRef) return;
				// A row the reader expanded or folded changes how far into it they are.
				captureReadingAnchor();
				if (currentTurnAnchorId) {
					scheduleCurrentTurnAnchorMaintenance();
					return;
				}
				refreshLatestTurnTailReserve();
				await tick();
				// Upward input can release following while the layout work is queued.
				if (shouldAutoScroll) scrollToBottom();
				updateJumpToLatestVisibility();
			});
		});
		observer.observe(chatColumnRef);
		return () => {
			observer.disconnect();
			if (raf !== null) cancelAnimationFrame(raf);
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
	const pendingPeerMessages = $derived($activeSession.pendingPeerMessages ?? []);

	// Keep a freshly-sent pending bubble in view. The content effect below tracks
	// `messages` only, so appending to `queuedMessages` grows the column without
	// scheduling any scroll. Mirrors the gap-indicator effect: respect the current
	// turn anchor when one is live, and never yank a user who scrolled up to read.
	$effect(() => {
		if (pendingMessages.length + pendingPeerMessages.length === 0 || !containerRef || !isMounted) return;
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

	// Get metadata for the last assistant message that has metadata (cost/duration
	// rendered there). This message may not be the absolute last in the array
	// (e.g. a TodoWrite tool_use could follow it).
	// Resolved by the single transcript walk rather than its own findLast over
	// the whole array per fold (R3).
	const lastMessageMetadata = $derived.by(() => {
		const m = transcript.lastAssistantMetadata;
		if (!m) return null;
		return JSON.stringify({
			cost: m.cost_usd ?? m.total_cost_usd,
			duration: m.duration_ms
		});
	});

	// Prompts seen waiting in this session's queue, by text, until their row
	// lands in the transcript (#863). Cleared on a session switch.
	const queuedPromptsSeen = new Set<string>();
	let queuedPromptsSession: string | null | undefined;
	$effect.pre(() => {
		const sessionId = $activeSession.sessionId;
		const queued = $activeSession.queuedMessages;
		if (sessionId !== queuedPromptsSession) {
			queuedPromptsSeen.clear();
			queuedPromptsSession = sessionId;
		}
		for (const entry of queued) queuedPromptsSeen.add(entry.content);
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
		// A prompt queued earlier lands here when its turn starts, long after the
		// send (#863): it follows only a reader already at the bottom.
		const fromQueue = hasNewMessage && newestMessage?.role === 'user' && queuedPromptsSeen.delete(newestMessage.content ?? '');
		const newUserMessage = hasNewMessage && !!newestMessage && isNavigableUserTurn(newestMessage) && (!fromQueue || shouldAutoScroll);
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

	// When a followed turn settles, the work log collapses and the live
	// anchor will not scroll back up. Recompute the tail and pin the latest
	// user prompt the same way a remount does. A reader who left the live
	// edge is left alone; we still refresh the timeline so it does not stay
	// stuck on the first tick.
	$effect(() => {
		const sessionId = $activeSession.sessionId;
		const unsettled = latestTurnUnsettled;
		if (sessionId !== settleSessionId) {
			settleSessionId = sessionId;
			readingAnchor = null; // #865: never restore another chat's row
			wasLatestTurnUnsettled = unsettled;
			return;
		}
		const wasUnsettled = wasLatestTurnUnsettled;
		wasLatestTurnUnsettled = unsettled;
		if (!isMounted || !wasUnsettled || unsettled) return;
		const following = untrack(() => shouldAutoScroll);
		if (!shouldPinLatestTurnOnSettle(wasUnsettled, unsettled, following)) {
			scheduleLatestTurnTailRefresh();
			void tick().then(() => updateCurrentTurnIndex());
			return;
		}
		cancelCurrentTurnAnchor(true);
		void tick().then(async () => {
			if (!containerRef || !untrack(() => shouldAutoScroll)) return;
			refreshLatestTurnTailReserve();
			await tick();
			if (!containerRef || !untrack(() => shouldAutoScroll)) return;
			const latestTurnId = transcript.latestUserTurnId;
			const anchor = latestTurnId ? document.getElementById(turnAnchorId(latestTurnId)) : null;
			if (!anchor || !containerRef.contains(anchor)) {
				scrollToBottom();
				updateCurrentTurnIndex();
				return;
			}
			const containerBox = containerRef.getBoundingClientRect();
			const anchorTop = containerRef.scrollTop + anchor.getBoundingClientRect().top - containerBox.top;
			setProgrammaticScrollTop(resolveCurrentTurnAnchorScrollTop(anchorTop));
			updateCurrentTurnIndex();
		});
	});
</script>

<div class="transcript-shell" class:has-turn-nav={turnNavigationItems.length >= 2} data-ui-mount={UI_MOUNT.messageArea}>
<!-- Unsend verdicts are announced here, outside the busy log region below. -->
<PendingStatusAnnouncer messages={pendingMessages} sessionId={$activeSession.sessionId} stale={!!$activeSession.queueStale} />
<!-- role="log" is a polite live region. aria-busy while a reply streams keeps
     screen readers from announcing every streamed token; the settled turn is
     read once the stream ends. -->
<div
	bind:this={containerRef}
	onscroll={handleScroll}
	onwheel={handleWheel}
	ontouchstart={handleTouchStart}
	ontouchmove={handleTouchMove}
	role="log"
	aria-label="Conversation messages"
	aria-busy={$activeSession.isStreaming}
	class="msg-scroll flex-1 overflow-y-auto overscroll-contain"
>
	{#if transcriptRender === 'skeleton'}
		<!-- Hydration skeleton. NOT the empty hero: this session has a transcript,
		     it just has not arrived yet. The blocks mirror the transcript's own
		     rhythm (prompt, then a taller response), so the switch to real
		     content is a fill rather than a jump. -->
			<TranscriptLoadingSkeleton stall={$activeSession.historyStall} onRetry={() => liveActiveSession.reloadSession()} />
	{:else if transcriptRender === 'empty'}
		<!-- Empty State -->
		<div class="h-full flex flex-col">
			<!-- Error display for empty state -->
			{#if $activeSession.error}
				<div class="max-w-5xl mx-auto w-full px-4 sm:px-8 pt-4">
					{@render errorBanner($activeSession.error, $activeSession.forkOfferSessionId)}
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
		<div class="chat-column" bind:this={chatColumnRef}>
			{#if mountedStart > 0}
				<!-- Keyboard-accessible equivalent of scrolling into the older edge. -->
				<div class="earlier-row" style:margin-top={leadingGap ? `${leadingGap}px` : undefined}>
					<button type="button" class="earlier-button" onclick={loadOlderItems}>
						Show earlier messages
					</button>
				</div>
			{/if}
			{#each visibleItems as item, i (displayItemKey(item))}
				<!-- Bounded mounting, with real row geometry for scroll anchoring. -->
				<div
					class="msg-virtualized"
					data-transcript-index={mountedStart + i}
					data-transcript-key={displayItemKey(item)}
					data-ui-row={item.kind === 'message' ? item.message.id || undefined : displayItemKey(item)}
					use:observeTurnAnchor
					id={item.kind === 'message' && item.message.id && isNavigableUserTurn(item.message) ? turnAnchorId(item.message.id) : undefined}
					data-user-turn-id={item.kind === 'message' && item.message.id && isNavigableUserTurn(item.message) ? item.message.id : undefined}
				>
				{#if item.kind === 'work_log'}
					<!-- Routine steps of one turn, folded (see $lib/utils/work-log).
					     Live and settled share the item key: settling only flips `mode`. -->
					<WorkLogGroup
						mode={item.mode}
						folded={item.folded}
						tail={item.tail}
						sequence={item.sequence}
						runs={item.runs}
						foldKey={item.foldKey}
						foldAlias={item.foldAlias}
						gallery={item.gallery}
						durationLabel={item.durationLabel}
						summary={item.summary}
						compactSummary={item.compactSummary}
						row={messageBody}
						revealMessageId={revealedTurnId}
						pagingEnabled={!shouldAutoScroll}
						{observeTurnAnchor}
					/>
				{:else if item.kind === 'system_group'}
					<!-- Grouped system/hook messages (2+ consecutive low-signal events) -->
					<SystemMessageGroup messages={item.messages} pagingEnabled={!shouldAutoScroll} />
				{:else}
					{@render messageBody(item.message, item.index, undefined, item.hiddenMedia)}
				{/if}
			</div>
			{/each}

			{#if mountedEnd < displayItems.length}
				<div class="earlier-row">
					<button type="button" class="earlier-button" onclick={loadNewerItems}>Show newer messages</button>
					<button type="button" class="earlier-button" onclick={jumpToLatest}>Jump to latest</button>
				</div>
			{/if}

			<!-- Prompts already sent but not yet delivered to the model. They sit
			     below the transcript, above the activity indicator, until the
			     backend hands them over (mid-turn steer or next-turn start). -->
			{#if pendingMessages.length > 0}
				<PendingMessages
					messages={pendingMessages}
					stale={!!$activeSession.queueStale}
					ownerName={$activeSession.isRemote ? $activeSession.ownerDeviceName : null}
					onRemove={(id) => liveActiveSession.unsendQueuedMessage(id)}
					onInterrupt={() => liveActiveSession.interruptForQueuedMessages()}
				/>
			{/if}

			<!-- The gap predicate guarantees this does not duplicate a prose cursor,
			     running-tool spinner, visible thinking state, or active subagent row. -->
			{#if showGapIndicator || $activeSession.liveCompaction}
				<TurnActivity
					startedAt={liveTurnStartedAt}
					compaction={$activeSession.liveCompaction ?? null}
					working={showGapIndicator}
				/>
			{/if}

			<!-- Error -->
			{#if $activeSession.error}
				{@render errorBanner($activeSession.error, $activeSession.forkOfferSessionId)}
			{/if}

			<!-- In-flight background work pinned to the history tail: agents (swarms
			     as one expandable row) and shell tasks, grouped, each with stop and,
			     for agents, its transcript. The phone shows the same collapsible row.
			     Renders nothing when nothing runs. Chats this one started
			     show beside the composer (BackgroundWorkStrip). -->
			<RunningTasksList sessionId={$activeSession.sessionId} presentation={$activeSession} variant="inline" />

			<!-- Responsive room belongs only to the live edge, never historical turns. -->
			<div
				bind:this={tailSpacerRef}
				class="transcript-tail-spacer"
				style:height={`calc(${Math.max(tailReservePx, latestTurnTailPx)}px + env(safe-area-inset-bottom, 0px) + var(--call-dock-reserve, 0px))`}
				aria-hidden="true"
			></div>
		</div>
	{/if}
</div>

	{#if selectionBlocksNavigation}
		<div class="selection-limit-layer">
			<div class="selection-limit-note" role="status">
				<span>Text selection is keeping these messages on screen.</span>
				<!-- Preserve selection until click; native focus can otherwise remove this control on pointerdown. -->
				<button type="button" class="earlier-button" onpointerdown={(event) => event.preventDefault()} onclick={clearTranscriptSelection} aria-label="Clear selection in transcript">Clear selection</button>
			</div>
		</div>
	{:else if showJumpToLatest}
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
			needsYouIds={needsYouTurns}
		/>
	{/if}
</div>

<!-- The one dispatch table: message → component. Shared with WorkLogGroup so a
     folded row renders exactly as it would inline. -->
<!-- The chat's error (a failed turn, a refused send). Announced as an alert;
     the dismiss control is named and has a full-size hit area, so a phone can
     clear it as easily as a mouse. -->
{#snippet errorBanner(message: string, forkOfferSessionId: string | null | undefined)}
	<div role="alert" class="chat-error-banner bg-destructive/10 border border-destructive/30 text-destructive pl-4 pr-1.5 py-1.5 rounded-lg flex items-center justify-between gap-2 shadow-s">
		<span class="text-sm py-1.5">{message}</span>
		<span class="flex items-center gap-1 shrink-0">
			{#if forkOfferSessionId}
				<button type="button" onclick={() => forkOfferedSessionHere(forkOfferSessionId)} class="text-sm font-medium underline underline-offset-2 hover:opacity-80 px-1.5">Fork here</button>
			{/if}
			<button type="button" onclick={() => liveActiveSession.clearError()} class="chat-error-dismiss" aria-label="Dismiss error" title="Dismiss">
				<X size={16} strokeWidth={2} aria-hidden="true" />
			</button>
		</span>
	</div>
{/snippet}

{#snippet messageBody(message: ChatMessage, messageIndex: number, gallerySources?: ReadonlySet<string>, hiddenMedia?: ReadonlySet<string>)}
	{@const delivery = agentDelivery(message)}
	{@const handoff = handoffAnchors.get(message)}
	{#if handoff}
		<!-- One hand-off, one row: starting → sending → settled (the later records update it). -->
		<HandoffCard episode={handoff} progress={progressForEpisode(handoff, handoffPendingView)} />
	{:else if delivery}
		{@const sender = agentSender(delivery, agentNames)}
		<AgentMessage
			sender={sender.name}
			senderId={sender.id}
			content={delivery.content}
			queued={message.systemData?.delivery === 'queued'}
		/>
	{:else if message.type === 'command'}
		<!-- Slash command invocation (e.g., /commit, /ralph-loop) -->
		<CommandMessage
			commandName={message.commandName || message.content}
			commandArgs={message.commandArgs}
		/>
	{:else if message.role === 'user'}
		<UserMessage
			content={message.content}
			timestamp={message.timestamp}
			sessionId={activeSessionId}
			messageUuid={message.jsonlUuid}
			userPromptOccurrence={userPromptOccurrence(messageIndex)}
			rewindEligible={rewindEligible[messageIndex]}
			notSent={!!message.metadata?.not_sent}
			notSentMessageId={typeof message.metadata?.not_sent_id === 'string' ? message.metadata.not_sent_id : null}
			midTurn={!!message.id && midTurnUserIds.has(message.id)}
			onRewindComplete={presentedRewindComplete}
		/>
	{:else if message.type === 'thinking' && chatPrefs?.showThinkingBlocks !== false}
		<!-- Thinking block - shown based on user preference -->
		<ThinkingBlock
			content={message.content}
			streaming={message.streaming}
			profileName={getProfileName(message)}
			subtype={message.thinkingSubtype}
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
				{gallerySources}
				timestamp={message.timestamp}
				messageUuid={message.jsonlUuid}
				sessionId={activeSessionId}
				streaming={isStreamAnchor}
				{hasPartialMessages}
				metadata={message.metadata}
				onRewindComplete={presentedRewindComplete}
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
		<!-- Fluid Note tool — scratchpad of current thoughts, not a todo bar. -->
		{@const notes = notesFromToolMessage(message)}
		{#if notes && notes.length > 0}
			<div class="w-full">
				<div class="min-w-0">
					<NotesList {notes} />
				</div>
			</div>
		{/if}
	{:else if message.type === 'tool_use' && (message.toolName === 'Task' || message.toolName === 'Agent')}
		<!-- Subagent tool_use (SDK "Task" or "Agent") - Skip if a linked SubagentMessage exists -->
		<!-- The SubagentMessage component handles displaying the agent's work -->
		<!-- The linked-subagent set is precomputed once per transcript build
		     (work-log.ts); this used to be a full `messages.some(...)` scan per
		     Task row per streaming fold. -->
		{#if !hasLinkedSubagent(message.toolId)}
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
		<!-- A generation tool that asks which source to use renders as its choice card (#379). -->
		{@const choose = chooseResult(message)}
		{#if choose}
			<GenerationChoiceCard
				kind={choose.kind}
				options={choose.options}
				tool={choose.tool}
				prompt={choose.prompt}
				toolId={message.toolId}
				rememberedUnavailable={choose.rememberedUnavailable}
				rememberedUnpriced={choose.rememberedUnpriced}
			/>
		{:else}
			{@const saved = rememberedResult(message)}
			<ToolUseMessage
				name={message.toolName || 'Tool'}
				input={message.toolInput}
				partialInput={message.partialToolInput}
				result={message.toolResult}
				status={message.toolStatus || 'running'}
				streaming={message.streaming}
				sessionId={activeSessionId}
				serverSide={message.serverSide ?? message.server_side ?? false}
				screenshot={message.toolScreenshot}
				{hiddenMedia}
			/>
			{#if saved}<GenerationSavedChoice {saved} />{/if}
		{/if}
	{:else if message.type === 'tool_result'}
		<!-- Skip standalone tool_result messages - they should be merged into tool_use -->
		<!-- This prevents flickering where result briefly appears separately -->
	{:else if isApprovalCard(message)}
		<!-- An approval asked in words: one card per request id, where it was
		     raised, settled in place (#483). Answered by typing yes or no. -->
		{@const approval = approvalCardData(message)}
		{#if approval}
			<ApprovalRequestCard data={approval} active={approval.request_id === activeApprovalId} />
		{/if}
	{:else if isApprovalOutcome(message)}
		<!-- What the owner's answer did, right after that answer (#484). -->
		<ApprovalOutcome data={message.systemData} content={message.content} />
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
				<TeamMemberMessage {message} />
			</div>
		</div>
	{/if}
{/snippet}

<!-- Fullscreen media preview overlay (driven by the lightbox store) -->
<MediaLightbox />

<style>
	/* The error banner's dismiss: a 32px square (44px on touch) around a 16px X. */
	.chat-error-dismiss {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 2rem;
		height: 2rem;
		border-radius: 0.375rem;
		color: var(--destructive);
		transition: background-color 150ms ease-out;
	}
	.chat-error-dismiss:hover { background: color-mix(in oklch, var(--destructive) 12%, transparent); }
	.chat-error-dismiss:active { background: color-mix(in oklch, var(--destructive) 18%, transparent); }
	.chat-error-dismiss:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	@media (pointer: coarse) {
		.chat-error-dismiss { width: 2.75rem; height: 2.75rem; }
	}
	@media (prefers-reduced-motion: reduce) {
		.chat-error-dismiss { transition: none; }
	}

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

	/* Match TurnNavigator's visibility. Reserve its hit area even when a wide
	   window contains a narrow chat pane; keep the usual centering once it fits. */
	@media (min-width: 640px) and (pointer: fine) {
		.has-turn-nav .chat-column {
			width: calc(100% - 40px);
			margin-left: max(40px, calc((100% - 768px) / 2));
			padding-left: 24px;
		}
	}

	/* Compact and touch layouts use TurnNavigator's narrower rail; keep the
	   text clear of it. */
	@media (max-width: 639px), (pointer: coarse) {
		.has-turn-nav .chat-column { padding-left: 28px; }
	}



	/* Explicit, keyboard-accessible paging mirrors the bounded scroll edges. */
	.earlier-row {
		display: flex;
		justify-content: center;
	}

	.earlier-button {
		min-height: 44px;
		padding: 5px 12px;
		border: 1px solid color-mix(in oklch, var(--border) 84%, transparent);
		border-radius: 999px;
		background: color-mix(in oklch, var(--background) 88%, transparent);
		color: var(--muted-foreground);
		font-size: 12px;
		cursor: pointer;
		transition: color 150ms ease, border-color 150ms ease;
	}

	.earlier-button:hover {
		color: var(--foreground);
		border-color: var(--border-strong, var(--border));
	}

	.earlier-button:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* The mounted slice is already bounded. Its rows need real geometry before
	   anchor compensation: content-visibility's 80px estimates otherwise expand
	   after the tick and move the preserved reading row a second time. */
	.msg-virtualized {
		content-visibility: visible;
	}

	.transcript-tail-spacer {
		flex: 0 0 auto;
		min-height: calc(24px + env(safe-area-inset-bottom, 0px));
		pointer-events: none;
	}

	.jump-latest-layer, .selection-limit-layer {
		position: absolute;
		z-index: 20;
		left: 50%;
		/* A phone call dock floats over the transcript's foot (ChatView, #816). */
		bottom: calc(12px + var(--call-dock-lift, 0px));
		transform: translateX(-50%);
		pointer-events: none;
	}

	.selection-limit-layer { width: max-content; max-width: calc(100% - 32px); }
	.selection-limit-note {
		display: flex; align-items: center; gap: 12px;
		padding: 6px 6px 6px 12px;
		border: 1px solid var(--border); border-radius: 12px;
		background: color-mix(in oklch, var(--background) 94%, transparent);
		color: var(--muted-foreground); font-size: 12px; line-height: 1.4;
		box-shadow: var(--shadow-m); pointer-events: auto;
	}
	.selection-limit-note .earlier-button { flex-shrink: 0; }
	.earlier-button:active { background: var(--muted); }
	.earlier-button:disabled { opacity: 0.45; pointer-events: none; }

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
		backdrop-filter: var(--fx-rail-backdrop);
		-webkit-backdrop-filter: var(--fx-rail-backdrop);
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

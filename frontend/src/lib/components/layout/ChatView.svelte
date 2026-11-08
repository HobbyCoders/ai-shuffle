<script lang="ts">
	import type { TerminalToggle } from '$lib/components/chat/composer-shell';
	/** Chat surface for every session. A new session opens on the centred
	 * agent and the "Runs on …" sentence (NewSessionHero) above the composer;
	 * heroes share one persistent editor. A live voice call adds compact
	 * controls alongside it. */
	import { onDestroy, tick, untrack } from 'svelte';
	import { MediaQuery } from 'svelte/reactivity';
	import { NO_CALL_ROOM, nextCallRoom, type CallRoom } from './callDockRoom';
	import { finishJourneyAfterPaint } from '$lib/services/journey-timing';
	import { activeSession, activeWsConnected, streamingSessions } from '$lib/stores/active-session';
	import { sessions } from '$lib/stores/app-data.svelte';
	import { openSession } from '$lib/services/session-navigation';
	import { interruptSession, stopSessionTask } from '$lib/api/sessions';
	import { buildBackgroundJobs, type BackgroundJob } from '$lib/utils/background-work';
	import BackgroundWorkStrip from '$lib/components/chat/BackgroundWorkStrip.svelte';
	import { tabRestoreTarget } from '$lib/stores/session/session-init';
	import { rememberEmptyChat, wasLastSeenEmpty } from '$lib/stores/session/empty-chat-memory';
	import { isAssistantConfirm } from '$lib/utils/assistant-confirm';
	import { sessionOwnerBoundary } from '$lib/stores/session-owner';
	import { notify } from '$lib/stores/notifications';
	import { platform } from '$lib/stores/platform.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { createConnectionBannerGrace } from '$lib/stores/session/connection-banner-grace';
	import { connectionNoticeTier, RECONNECT_BANNER_ESCALATE_MS } from './connection-notice';
	import Banner from '$lib/components/Banner.svelte';
	import ConnectionNotice from './ConnectionNotice.svelte';
	import { workerInstalls } from '$lib/stores/worker-presence';
	import { workerDisplayName } from '$lib/components/ui/worker-name';
	import { forkOfferedSessionHere } from '$lib/components/chat/fork-here';
	import MessageArea from '$lib/components/chat/MessageArea.svelte';
	import TranscriptHydrationBoundary from '$lib/components/chat/TranscriptHydrationBoundary.svelte';
	import MirrorSyncGate from '$lib/components/chat/MirrorSyncGate.svelte';
	import { createMirrorSync, mirrorSyncHeadline } from '$lib/components/chat/mirror-sync.svelte';
	import ChatInput from '$lib/components/chat/ChatInput.svelte';
	import PermissionQueue from '$lib/components/PermissionQueue.svelte';
	import QuestionTray from '$lib/components/chat/QuestionTray.svelte';
	import PrivateAccessPrompt from '$lib/components/chat/PrivateAccessPrompt.svelte';
	import NewSessionHero from './NewSessionHero.svelte';
	import ChatUnavailableNotice from './ChatUnavailableNotice.svelte';
	import { unavailableChat, clearUnavailableChat } from '$lib/stores/unavailable-chat';
	import VoiceCallPanel from '$lib/components/voice/VoiceCallPanel.svelte';
	import { voiceCallState, isCallSurfaceActive } from '$lib/services/voiceCall';
	import { watchCallOutcomes } from '$lib/services/voiceCallOutcome';
	import { RotateCcw, LoaderCircle, CloudOff } from 'lucide-svelte';
	import { chatHeaderTitle, chatHeroKind, chatLayoutSurface, classifyChatSurface, createChatLayoutTracker, HISTORY_STALL_MESSAGE, initialChatLayout, settledEmptyChat } from './chat-empty-state';
	import { pendingDeepLink, deepLinkLoading } from '$lib/stores/pending-deep-link';
	// The phone shell docks prompts above its composer; the web build has none.
	import { composerShell } from '$lib/components/chat/composer-shell';
	import { sentQuestions, markQuestionSent } from '$lib/stores/session/sent-questions';
	// Flight-recorder signature (attribute-only; see ui-signature.ts).
	import { UI_MOUNT, UI_STATE_CHAT, uiComposer, uiFlag, uiSessionTag } from '$lib/utils/ui-signature';

	interface Props {
		onBrowserToggle?: () => void;
		onBrowserPreload?: () => void;
		browserOpen?: boolean;
		terminal?: TerminalToggle;
		/** Out: the open mirrored chat still waits for its first sync. */
		mirrorSyncPending?: boolean;
		navigationOverlayOpen?: boolean;
	}

	let { onBrowserToggle, onBrowserPreload, browserOpen = false, terminal, mirrorSyncPending = $bindable(false), navigationOverlayOpen }: Props = $props();

	// Classify the empty-message surface. While a session's history is being
	// fetched (boot restore OR an openChat navigation) messages is still [] but
	// sessionId already points at the target, so the surface is 'loading': never
	// the new-chat hero (it would invite sends into an invisible session) and
	// never the cleared hint (the chat may turn out to have messages). A load
	// keeps the layout the view already had (hero or message column) until its
	// outcome is known; see createChatLayoutTracker below, chat-empty-state.ts
	// and #558. A load the feed released without any history (historyStall,
	// #913) is still 'loading': only a completed response or a clear proves a
	// chat empty, so it never turns into the cleared hero or moves the composer.
	const historyUnconfirmed = $derived(!!$activeSession.historyStall
		&& $activeSession.messages.length + ($activeSession.pendingPeerMessages?.length ?? 0) === 0);
	const openChatQueued = $derived(!!$sessions.find((s) => s.id === $activeSession.sessionId)?.start_queued);
	// The row stops saying queued on one channel and the first message arrives
	// on another, so hold the layout across that gap instead of flashing "cleared".
	const QUEUED_HANDOVER_MS = 1500;
	let queuedChatId = $state<string | null>(null);
	$effect(() => {
		const id = $activeSession.sessionId ?? null;
		if (openChatQueued) { queuedChatId = id; return; }
		const timer = setTimeout(() => { queuedChatId = null; }, QUEUED_HANDOVER_MS);
		return () => clearTimeout(timer);
	});
	const holdQueuedLayout = $derived(openChatQueued || (!!queuedChatId && queuedChatId === $activeSession.sessionId));
	const chatSurface = $derived(
		classifyChatSurface({
			sessionId: $activeSession.sessionId,
			startQueued: holdQueuedLayout,
			// Pending peers belong in the transcript, even before its first message.
			messageCount: $activeSession.messages.length + ($activeSession.pendingPeerMessages?.length ?? 0),
			isRestoringSession: $activeSession.isRestoringSession,
			// A deep link boot has read but not opened yet is a load in progress,
			// not a New Chat: no hero flash before the linked chat appears.
			isLoadingSession: $activeSession.isLoadingSession || deepLinkLoading($pendingDeepLink, $activeSession.sessionId),
			historyStalled: historyUnconfirmed
		})
	);
	// The chat.open sample ends after the DOM update once history has settled.
	// A primitive $derived keeps the effect from re-running (and restarting its
	// frames) on every activeSession tick, such as streaming text; a teardown
	// cancels the frames, so a view that moved on never completes the old sample.
	const settledChatId = $derived.by(() => {
		const id = $activeSession.sessionId;
		const loading = $activeSession.isLoadingSession || $activeSession.isRestoringSession || historyUnconfirmed;
		return id && !loading && !deepLinkLoading($pendingDeepLink, id) ? id : null;
	});
	$effect(() => {
		if (settledChatId) return finishJourneyAfterPaint('chat.open', settledChatId);
	});
	// A mirrored chat (owned by another worker) is checked against its owner
	// before it is shown, whatever the layout: a chat opened from the hero keeps
	// the hero while it loads, so the check must not wait for the message
	// column to mount MirrorSyncGate. Its first catch-up reveals the existing
	// live transcript, not a new replay. A conversation composed here has no old
	// transcript to reveal: keep its local echo and live feed through that checkpoint.
	// An unknown owner reads as mirrored, as the gate always did.
	const activeRow = $derived($sessions.find(s => s.id === $activeSession.sessionId));
	const mirrorRemote = $derived(activeRow?.is_remote ?? $activeSession.isRemote ?? true);
	const mirrorDevice = $derived(activeRow?.device_name ?? $activeSession.ownerDeviceName);
	const mirrorSync = createMirrorSync(() => ({
		sessionId: $activeSession.sessionId,
		remote: mirrorRemote,
		createdFromComposer: $activeSession.createdFromComposer,
		onCaughtUp: () => activeSession.reloadSession(false, 'mirror-caught-up')
	}));
	// MainPanel's header title follows the hero through the first sync (#812).
	$effect(() => { mirrorSyncPending = mirrorSync.waiting; });
	// The centred hero layout (new or cleared chat) versus the message column.
	// A load keeps the layout the view already had until its outcome is known,
	// so opening a chat moves the composer at most once (never centre → bottom
	// → centre for a chat that turns out empty); see createChatLayoutTracker.
	// A mirrored chat's first sync check is part of its load (chatLayoutSurface).
	// A reload knows the chat before it is adopted, and whether this tab last saw
	// it empty, so an empty chat starts centred instead of moving there (#1049).
	const restoreTarget = tabRestoreTarget();
	const chatLayout = createChatLayoutTracker(initialChatLayout(wasLastSeenEmpty($activeSession.sessionId ?? restoreTarget?.sessionId)));
	// What the view presents: a mirrored chat still waiting for its first sync
	// shows no transcript yet (the hero or the gate's pane), so it reads as
	// loading, like the history load it is part of.
	const presentedSurface = $derived(chatLayoutSurface(chatSurface, { mirrorSyncPending: mirrorSync.waiting }));
	const emptyChatToRemember = $derived(settledEmptyChat(presentedSurface, $activeSession.sessionId));
	$effect(() => {
		if (emptyChatToRemember !== undefined) rememberEmptyChat(emptyChatToRemember);
	});
	// Diagnostics follow the painted transcript, not the incoming empty store.
	// Layout/composer binding still follow the live target and existing tracker.
	let transcriptSurface = $state<typeof presentedSurface | null>(null);
	let transcriptMessageCount = $state<number | null>(null);
	let transcriptSessionId = $state<string | null | undefined>(undefined);
	// A shown notice also survives an empty outcome until its minimum ends.
	// The mirror gate still owns its own layout and visibility barrier.
	const isEmptyState = $derived(chatLayout(presentedSurface) === 'hero'
		&& (mirrorSync.waiting || transcriptSurface !== 'loading'));
	const paintedSurface = $derived(isEmptyState || mirrorSync.waiting
		? presentedSurface : transcriptSurface ?? presentedSurface);
	// Which hero the hero layout shows: the new-session hero, a cleared chat,
	// a chat opened from the hero still loading, or a mirrored chat waiting
	// for its first sync.
	const heroKind = $derived(chatHeroKind({ surface: chatSurface, mirrorSyncPending: mirrorSync.waiting }));
	// (A 'cleared' hero is a BOUND, settled, empty session — a just-cleared
	// conversation: the session's own title + a "cleared" hint instead of the
	// brand-new "Let's build your project" workspace picker.)
	// The bound chat's hero is still waiting (history load or first sync).
	const isLoadingHero = $derived(heroKind === 'loading' || heroKind === 'syncing');
	// The history load gave up waiting without any (#913): the same hero says
	// so in its one line and offers Retry.
	const heroStall = $derived(heroKind === 'loading' && historyUnconfirmed ? $activeSession.historyStall ?? null : null);
	// A chat that could not be opened, while this surface is still the unbound
	// New Chat it fell back to. Binding any chat hides it.
	const unavailable = $derived($activeSession.sessionId ? null : $unavailableChat);

	// Chats this one started (StartSession) that are still working, as plain
	// lines beside the composer, in every chat. Its own background tasks stay
	// in the transcript's running-tasks list, so they are not repeated here.
	const backgroundJobs = $derived(
		buildBackgroundJobs({
			tasks: [],
			messages: $activeSession.messages,
			sessions: $sessions,
			streaming: $streamingSessions
		})
	);
	function openJob(job: BackgroundJob) {
		if (job.sessionId) openSession(job.sessionId);
	}
	async function stopJob(job: BackgroundJob): Promise<boolean> {
		try {
			if (job.stop.kind === 'task') {
				const owner = $activeSession.sessionId;
				if (!owner) return false;
				const result = await stopSessionTask(owner, job.stop.taskId);
				if (!result.ok) notify.error('Could not stop that job. It may have finished.');
				return result.ok;
			}
			await interruptSession(job.stop.sessionId);
			return true;
		} catch {
			notify.error('Could not stop that job. It may have finished.');
			return false;
		}
	}
	// The bound hero's heading: the header's rule (chatHeaderTitle), so a chat
	// whose name is not known yet reads "Loading…", never "New Chat" (the
	// switcher's placeholder), while it opens.
	const heroTitle = $derived(
		chatHeaderTitle({
			// Before boot adopts the chat it is reopening, the hero already names it.
			sessionId: $activeSession.sessionId ?? (isLoadingHero ? restoreTarget?.sessionId : null),
			title: $activeSession.sessionId ? $activeSession.title : restoreTarget?.title,
			messageCount: 0,
			loading: isLoadingHero,
			listTitle: activeRow?.title
		})
	);
	const heroHint = $derived(
		heroKind === 'syncing'
			? mirrorSyncHeadline(mirrorSync.syncState, mirrorDevice)
			: heroStall
				? HISTORY_STALL_MESSAGE[heroStall]
				: isLoadingHero
				? 'Opening the conversation…'
				: 'Conversation cleared — send a message to start fresh.'
	);

	// Voice (ruling VM18). The gate is the CALL, not the session's mode: a voice
	// session with no call running is an ordinary chat, and only an in-flight call
	// on the session currently being viewed takes over the composer slot. The
	// singleton controller means the sessionId comparison is what stops a call on
	// one session from swallowing another session's composer.
	const voiceCallActive = $derived(
		isCallSurfaceActive($voiceCallState, $activeSession.sessionId)
	);

	// A live call also suppresses the new-chat hero: a freshly created voice
	// session has no messages yet, and the workspace picker has nothing to offer
	// mid-call. The transcript column takes over and fills in as the exchange
	// lands.
	const showEmptyHero = $derived(isEmptyState && !voiceCallActive);
	// The injected flight recorder can read the live derived values *inside*
	// a DOM removal, before Svelte updates the signature attributes. No effect
	// tracks these reads. Vite eliminates this entire block in release builds.
	if (import.meta.env.DEV) {
		$effect(() => {
			const view = document.querySelector<HTMLElement & { __flightRecorderState?: () => unknown }>(
				'[data-ui-mount="ChatView"]'
			);
			if (!view) return;
			const read = () => untrack(() => ({
				showEmptyHero, isEmptyState, chatSurface, presentedSurface, transcriptSurface,
				mirrorWaiting: mirrorSync.waiting, voiceCallActive,
				session: uiSessionTag($activeSession.sessionId),
				transcriptSession: uiSessionTag(transcriptSessionId),
				messages: $activeSession.messages.length,
				transcriptMessages: transcriptMessageCount
			}));
			view.__flightRecorderState = read;
			return () => { if (view.__flightRecorderState === read) delete view.__flightRecorderState; };
		});
	}
	// A brand-new session (not a cleared one or the "could not open" notice)
	// leads with the agent and the "Runs on …" sentence, which stands in for
	// the composer's chip row until the first message.
	const showNewSessionHero = $derived(showEmptyHero && heroKind === 'new' && !unavailable);

	// Keep the editor mounted through call transitions so drafts and file chips survive.
	const showComposer = true;
	// What the flight recorder reads off this view each frame (ui-signature.ts).
	// Derived from the same inputs as chatSurface; nothing reads them back.
	// The surface and message count are what is on screen: a mirrored chat's
	// transcript is not shown until its first sync, so both read as loading
	// and 0 until then.
	const uiSignature = $derived({
		session: uiSessionTag($activeSession.sessionId),
		composer: uiComposer({ archived: false, emptyHero: showEmptyHero, mobile: platform.isMobile }),
		messages: mirrorSync.waiting ? '0' : String($activeSession.messages.length + ($activeSession.pendingPeerMessages?.length ?? 0)),
		loading: uiFlag(
			$activeSession.isLoadingSession || $activeSession.isRestoringSession || historyUnconfirmed
				|| deepLinkLoading($pendingDeepLink, $activeSession.sessionId)
		),
		streaming: uiFlag($activeSession.isStreaming)
	});
	let surfaceHeight = $state(0);
	let composerHeight = $state(0);

	// #816: on a phone the call dock floats over the transcript's foot instead of
	// stacking on the composer, so neither the composer nor the transcript moves
	// when a call starts or ends. The transcript's tail spacer gets the dock's
	// height (--call-dock-reserve) so its last lines can still scroll clear of
	// the dock; when that room is held and released is callDockRoom.ts.
	const phoneWidth = new MediaQuery('max-width: 639px');
	let callDockHeight = $state(0);
	let callRoom = $state<CallRoom>(NO_CALL_ROOM);
	$effect(() => {
		const input = {
			callActive: voiceCallActive,
			dockHeight: callDockHeight,
			session: $activeSession.sessionId ?? null,
			streaming: $activeSession.isStreaming,
			phone: phoneWidth.current
		};
		const next = nextCallRoom(untrack(() => callRoom), input);
		if (next !== untrack(() => callRoom)) callRoom = next;
	});
	$effect(() => {
		if (showEmptyHero && surfaceHeight > 0 && composerHeight > surfaceHeight && platform.isMobile) {
			void tick().then(() => {
				const focused = document.activeElement;
				if (focused instanceof HTMLElement && focused.closest('.empty-input-layout')) {
					focused.scrollIntoView({ block: 'nearest' });
				}
			});
		}
	});

	// VM23 (user directive, 2026-08-15): while a call is live, worker permission
	// requests are the VOICE agent's to relay — it reads them out and answers via
	// respond_approval on the user's spoken yes/no. Showing the GUI modal at the
	// same time creates a race: the user clicks Allow, then says "go ahead", and
	// the agent's respond_approval finds nothing to answer. So the modal's
	// permission leg is suppressed for exactly the sessions/timespan the spoken
	// leg covers; it returns the moment the call ends. Questions are unaffected.
	// A request the agent asks in words (`assistant_confirm`, raised on any
	// owner chat) never opens the dialog: it reads as the agent's question in
	// the transcript and is answered by typing or saying yes or no
	// (MessageArea renders it). Every other request keeps the dialog.
	const dialogPermissions = $derived(($activeSession.pendingPermissions ?? []).filter((request) => !isAssistantConfirm(request)));
	const showPermissionModal = $derived(!voiceCallActive && dialogPermissions.length > 0);
	// A shell prompt dock answers permissions, private access and questions
	// above the composer, so it replaces the modal and the question tray.
	const PromptDock = composerShell().PromptDock;

	// A call that ends by itself (failed start, transport error, the VW5 length
	// cap) unmounts the controls that were showing the reason: say why on the
	// page. One watcher for the whole page, so remounting this view never
	// re-reports the last ending (see watchCallOutcomes).
	watchCallOutcomes(voiceCallState);

	// Animation: Track transition from empty → active state
	let transitioningFromEmpty = $state(false);
	// Null until the first look: a view that opens on the message column (every
	// reload of a chat with history) has not moved from anywhere, so it must not
	// slide the composer down from the hero (#1049).
	let previousIsEmpty = $state<boolean | null>(null);

	// The composer's move from the hero to the dock (#1085, #1073). Its box is
	// read just before the layout flips and again after, and the slide starts
	// from where it really was: a fixed distance started it above the centred
	// composer and an overshooting curve carried it past the dock. A composer
	// that was already docked (phone widths, the phone shell) does not slide.
	let inputSlot = $state<HTMLDivElement | null>(null);
	let composerBeforeDock: DOMRect | null = null;
	$effect.pre(() => {
		const isEmpty = isEmptyState;
		untrack(() => {
			composerBeforeDock = previousIsEmpty === true && !isEmpty && inputSlot ? inputSlot.getBoundingClientRect() : null;
		});
	});
	function slideComposerToDock(from: DOMRect | null): Animation | null {
		const slot = inputSlot;
		if (!slot || !from || typeof slot.animate !== 'function') return null;
		if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return null;
		const to = slot.getBoundingClientRect();
		const travel = from.top - to.top;
		if (Math.abs(to.bottom - from.bottom) <= 2 || Math.abs(travel) <= 2) return null;
		return slot.animate(
			[{ transform: `translateY(${travel}px)` }, { transform: 'translateY(0)' }],
			{ id: 'composer-dock', duration: 380, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' }
		);
	}

	// Depends only on `isEmptyState`; reading any other store here would re-run
	// the effect (clearing the timer via teardown) without resetting the flag.
	$effect(() => {
		const isEmpty = isEmptyState;
		const wasEmpty = untrack(() => previousIsEmpty);
		if (wasEmpty === isEmpty) return;
		previousIsEmpty = isEmpty;

		if (wasEmpty === true && !isEmpty) {
			// Empty → active transition: animate the reveal
			transitioningFromEmpty = true;
			const slide = untrack(() => slideComposerToDock(composerBeforeDock));
			composerBeforeDock = null;
			const timer = setTimeout(() => {
				transitioningFromEmpty = false;
			}, 450);
			return () => {
				clearTimeout(timer);
				slide?.cancel();
				transitioningFromEmpty = false;
			};
		} else {
			// Active → empty transition: clear the animation class immediately.
			// Without this, the class gets stuck if the effect cleanup (clearTimeout)
			// runs before the timeout fires, leaving transitioningFromEmpty=true.
			transitioningFromEmpty = false;
		}
	});

	// Handle permission response — approve/deny only (defer was dropped from the UI).
	function handlePermissionResponse(detail: {
		request_id: string;
		decision: 'allow' | 'deny';
		remember?: 'none' | 'session' | 'profile';
		pattern?: string;
	}) {
		const { request_id, decision, remember, pattern } = detail;
		const sessionId =
			$activeSession.pendingPermissions.find(req => req.request_id === request_id)?.session_id ??
			$activeSession.sessionId ??
			undefined;
		activeSession.sendPermissionResponse(request_id, decision, remember, pattern, sessionId);
	}

	// Handle question response
	function handleQuestionResponse(detail: {
		request_id: string;
		tool_use_id: string;
		answers: Record<string, string | string[]>;
		dismissed?: boolean;
		skipped?: string[];
	}) {
		const { request_id, tool_use_id, answers, dismissed, skipped } = detail;
		const sessionId =
			$activeSession.pendingQuestions.find(q => q.request_id === request_id)?.session_id ??
			$activeSession.sessionId ??
			undefined;
		const sent = activeSession.sendUserQuestionResponse(request_id, tool_use_id, answers, sessionId, {
			dismissed,
			skipped
		});
		if (!sent) notify.error('Your answer was not sent. Reconnect and try again.');
		// Close it on the tap, not on the server's ack (#891); a failed delivery reopens it.
		else markQuestionSent(request_id);
	}

	// Questions still waiting for an answer from this user: not the ones just sent.
	const openQuestions = $derived(
		($activeSession.pendingQuestions ?? []).filter((q) => !$sentQuestions.has(q.request_id))
	);

	// Manually re-establish the WebSocket (the Reconnect button on the
	// disconnection banner). Replace a suspect socket and reset retry backoff.
	// The store ignores a tap while its own handshake is still running.
	function handleReconnect() {
		activeSession.reconnect();
	}

	// Connection-lost notice, tiered so a phone resume stays quiet:
	//  - under 3s of VISIBLE disconnect: nothing (the resume reconnect is
	//    usually still handshaking);
	//  - after that: a small floating "Reconnecting…" pill over the transcript;
	//  - escalated to the warning banner with a Reconnect button only when the
	//    client has given up (wsReconnectRequired) or the disconnect has lasted
	//    RECONNECT_BANNER_ESCALATE_MS of visible time.
	// Both gates count visible time only (connection-banner-grace.ts), so time
	// in the background never counts toward either, and everything hides the
	// moment the socket is back. The gates live for the component's lifetime
	// (not per-effect-run, or their timer/shown state would be lost on every
	// dependency change); destroy() runs once on unmount.
	let reconnectNoticeDue = $state(false);
	let reconnectBannerDue = $state(false);
	const reconnectNoticeGrace = createConnectionBannerGrace({
		onShow: () => { reconnectNoticeDue = true; },
		onHide: () => { reconnectNoticeDue = false; }
	});
	const reconnectBannerGrace = createConnectionBannerGrace({
		onShow: () => { reconnectBannerDue = true; },
		onHide: () => { reconnectBannerDue = false; },
		graceMs: RECONNECT_BANNER_ESCALATE_MS
	});
	onDestroy(() => {
		reconnectNoticeGrace.destroy();
		reconnectBannerGrace.destroy();
	});

	$effect(() => {
		// Only an active session can show the notice; otherwise report
		// "connected" so no timer arms for a notice that would never render.
		const connected = !$activeSession.sessionId || $activeWsConnected;
		reconnectNoticeGrace.setConnected(connected);
		reconnectBannerGrace.setConnected(connected);
	});

	const connectionNotice = $derived(connectionNoticeTier({
		noticeDue: reconnectNoticeDue,
		bannerDue: reconnectBannerDue,
		reconnectRequired: $activeSession.wsReconnectRequired,
		manual: $activeSession.wsManualReconnect
	}));
	// The worker this page's socket talks to, for "Connecting to Unraid…". Only
	// the last known list: nothing is fetched while the worker is unreachable.
	const selfWorker = $derived(
		workerDisplayName($workerInstalls?.find((i) => i.is_self)?.device_name, '') || null
	);
	// The mirror sync pill floats in the same spot; it moves below this one.
	let reconnectPillHeight = $state(0);
	const statusOffset = $derived(
		connectionNotice === 'pill' && reconnectPillHeight ? `${reconnectPillHeight + 6}px` : undefined
	);
</script>

<div class="chat-view" bind:clientHeight={surfaceHeight} style:--chat-surface-height={surfaceHeight ? `${surfaceHeight}px` : undefined}
	style:--call-dock-room={callRoom.room ? `${callRoom.room}px` : undefined} style:--call-dock-height={voiceCallActive && callDockHeight ? `${callDockHeight}px` : undefined} class:transitioning-from-empty={transitioningFromEmpty} class:is-empty={showEmptyHero}
	data-ui-state={UI_STATE_CHAT}
	data-ui-mount={UI_MOUNT.chatView}
	data-ui-surface={paintedSurface}
	data-ui-session={!isEmptyState && !mirrorSync.waiting && transcriptSurface !== null ? uiSessionTag(transcriptSessionId) : uiSignature.session}
	data-ui-target-session={uiSignature.session}
	data-ui-composer={uiSignature.composer}
	data-ui-messages={!isEmptyState && !mirrorSync.waiting && transcriptMessageCount !== null ? String(transcriptMessageCount) : uiSignature.messages}
	data-ui-loading={uiSignature.loading}
	data-ui-streaming={uiSignature.streaming}>
	{#if showEmptyHero}
		<!-- Empty state hero: workspace selector / cleared hint. The ChatInput is
		     NOT rendered here — it lives in the always-mounted .input-slot below so
		     its subtree (Tiptap draft, open context-usage popover, VoiceRecorder,
		     dropdowns) survives the empty → active flip instead of being destroyed
		     and remounted (#665). -->
		<div class="empty-hero" class:hero-only={!showComposer}>
			{#if $activeSession.error}
				<div class="empty-error-overlay">
					<Banner severity="error" message={$activeSession.error} dismissible onDismiss={() => activeSession.clearError()}
						action={$activeSession.forkOfferSessionId ? { label: 'Fork here', onClick: () => forkOfferedSessionHere($activeSession.forkOfferSessionId!) } : undefined} />
				</div>
			{/if}

			<div class="empty-hero-content">
				{#if unavailable}
					<ChatUnavailableNotice chat={unavailable} onDismiss={clearUnavailableChat} />
				{:else if heroKind !== 'new'}
					<!-- Bound existing session with an empty transcript (cleared via the
					     sessions-board broom, #558). Show its OWN title + a cleared
					     hint so it never reads as a brand-new chat. The project is
					     fixed for an existing session, so no workspace picker here.
					     A chat opened from this layout shows the same heading while
					     its history loads (or, mirrored, while its first sync runs),
					     so the composer stays put until the outcome is known
					     (createChatLayoutTracker). -->
					<div class="cleared-hero" class:is-loading={isLoadingHero && heroStall !== 'error'} aria-busy={isLoadingHero && heroStall !== 'error'}>
						<div class="cleared-badge" class:is-error={heroStall === 'error'} aria-hidden="true">
							{#if heroStall === 'error'}
								<CloudOff size={22} strokeWidth={1.5} />
							{:else if isLoadingHero}
								<LoaderCircle size={22} strokeWidth={1.5} class="hero-spin" />
							{:else}
								<RotateCcw size={22} strokeWidth={1.5} />
							{/if}
						</div>
						<h1 class="cleared-title" title={heroTitle}>{heroTitle}</h1>
						<p class="cleared-hint" class:is-stalled={!!heroStall} role="status" data-history-stall={heroStall ?? undefined}>
							<!-- Every state holds the room of the cleared hint's two lines (no
							     update count), so swapping the text never moves the centred
							     composer. -->
							{#if heroStall}
								<span class="hint-text">{heroHint}</span>
								<button type="button" class="hero-retry" aria-label="Retry loading the conversation" onclick={() => activeSession.reloadSession()}>Retry</button>
							{:else}
								{heroHint}
							{/if}
						</p>
					</div>
				{:else}
					<!-- New session: one question, directly above the composer. -->
					<NewSessionHero />
				{/if}
			</div>
		</div>
	{:else}
		<!-- Active chat: message column. The input lives in the shared .input-slot
		     below, not here — see the #665 note above. -->
		<div class="chat-column" style:--chat-status-offset={statusOffset}>
			<div class="message-area-wrapper">
				<ConnectionNotice
					tier={connectionNotice}
					reconnectRequired={$activeSession.wsReconnectRequired}
					manual={$activeSession.wsManualReconnect}
					workerName={selfWorker}
					onReconnect={handleReconnect}
					bind:pillHeight={reconnectPillHeight}
				/>
				<!-- No call surface here any more (VM18): the call controls live in the
				     composer slot below, so the transcript column looks the same during
				     a call as it does outside one. -->
				<!-- MessageArea handles session-switch scroll save/restore internally
				     via $effect watching sessionId. No {#key} needed — avoiding
				     component destruction prevents a Svelte 5 reactivity edge case
				     where the recreated component loses reactive updates during
				     new session creation (sessionId: undefined → new ID).
				     onCaughtUp fires once per opened mirrored chat (its first catch-up);
				     later backlog drains arrive through the live feed (#2227). -->
				<!-- The sync check itself runs at the top of this view (mirrorSync),
				     so it started even if the chat opened in the hero layout. -->
				<MirrorSyncGate sync={mirrorSync} deviceName={mirrorDevice}>
					<TranscriptHydrationBoundary mirrorPending={mirrorSync.waiting} startQueued={holdQueuedLayout}
						navigationOverlayOpen={navigationOverlayOpen ?? (layout.current.sidebarOverlay && !layout.current.sidebarCollapsed)}
						onSurface={(surface, count, id) => { transcriptSurface = surface; transcriptMessageCount = count; transcriptSessionId = id; }}>
						<MessageArea />
					</TranscriptHydrationBoundary>
				</MirrorSyncGate>
			</div>
		</div>
	{/if}

	<!-- Persistent input slot. Rendered ONCE across empty/active states so the
	     ChatInput subtree (draft, context-usage popover, VoiceRecorder, dropdowns)
	     is never torn down at the isEmptyState boundary (#665). Only the `compact`
	     prop and the surrounding layout classes change between states; the
	     conditional strips below are siblings of ChatInput, not ancestors, so they
	     never remount it. All three lanes keep this same editor. -->
	{#if showComposer}
		<div class="input-slot" bind:this={inputSlot} bind:clientHeight={composerHeight} class:composer-overflow={surfaceHeight > 0 && composerHeight > surfaceHeight} class:input-wrapper={!showEmptyHero} class:empty-input-layout={showEmptyHero}>
			{#if voiceCallActive && $activeSession.sessionId}
				<div class="call-dock-slot" bind:clientHeight={callDockHeight}><VoiceCallPanel sessionId={$activeSession.sessionId} compact /></div>
			{/if}
			<BackgroundWorkStrip jobs={backgroundJobs} onOpen={openJob} onStop={stopJob} />
				{#key $sessionOwnerBoundary}
					{#if PromptDock}
						<PromptDock
							sessionId={$activeSession.sessionId}
							permissions={showPermissionModal ? dialogPermissions : []}
							questions={openQuestions}
							working={$activeSession.isStreaming}
							connected={$activeWsConnected}
							onPermission={handlePermissionResponse}
							onQuestion={handleQuestionResponse}
						/>
					{:else}
						<PrivateAccessPrompt sessionId={$activeSession.sessionId} />
						<QuestionTray requests={$activeSession.pendingQuestions ?? []} sent={$sentQuestions} working={$activeSession.isStreaming} connected={$activeWsConnected} onRespond={handleQuestionResponse} />
					{/if}
					<ChatInput compact={!showEmptyHero} newSession={showNewSessionHero} {onBrowserToggle} {onBrowserPreload} {browserOpen} {terminal} />
				{/key}
		</div>
	{/if}

	{#if showEmptyHero && showComposer}
		<!-- Desktop only: balances the flex:1 hero so the hero + persistent input pair sits
		     vertically centered — reproduces the old .empty-state
		     justify-content:center grouping without wrapping the input in a
		     state-specific ancestor (#665). -->
		<div class="empty-bottom-spacer" aria-hidden="true"></div>
	{/if}

	<!-- Permission gates still block their tool. Questions live above the composer.
	     A shell prompt dock answers them above the composer instead. -->
	{#if !PromptDock}
	{#if !showEmptyHero && showPermissionModal}
		<div class="modal-overlay">
			<div class="modal-backdrop"></div>
			<div class="modal-content">
				<div class="modal-card-wrapper">
					<PermissionQueue requests={dialogPermissions} onRespond={handlePermissionResponse} />
				</div>
			</div>
		</div>
	{/if}
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

	.chat-column {
		position: relative;
		display: flex;
		flex-direction: column;
		flex: 1;
		min-width: 0;
		min-height: 0;
		overflow: hidden;
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

	.message-area-wrapper {
		/* Keep the mirror/reconnect overlays anchored here before, during and
		   after messageAreaReveal. Its temporary transform must not create then
		   remove their containing block while off-catalog identity is pending. */
		position: relative;
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}

	@keyframes reconnect-spin { to { transform: rotate(360deg); } }

	/* The input slot is always rendered (see #665). It never shrinks in either
	   state; the empty-state layout (centering / width cap) is applied via
	   .empty-input-layout below, the active-state via .input-wrapper. */
	.input-slot {
		flex-shrink: 0;
	}

	.input-wrapper {
		flex-shrink: 0;
	}

	/* #816: a phone has no height to give the call dock, and stacking it on the
	   composer moved the composer and the transcript by its full height whenever
	   a call began or ended (the provider ending it included). There the dock
	   floats just above the composer, over the transcript's foot, on a fade of
	   the chat background; the transcript reserves its height (see
	   callDockRoom.ts) and the jump-to-latest pill rises above it. Wider screens
	   keep it in the composer's flow. */
	@media (max-width: 639px) {
		.chat-view {
			--call-dock-reserve: var(--call-dock-room, 0px);
			--call-dock-lift: var(--call-dock-height, 0px);
		}
		.input-wrapper:has(> .call-dock-slot) {
			position: relative;
		}
		.call-dock-slot {
			position: absolute;
			inset-inline: 0;
			bottom: 100%;
			z-index: 2;
			padding-top: 1.5rem;
			background: linear-gradient(to bottom, transparent, var(--background) 1.5rem);
			/* The see-through fade lets touches reach the transcript; the
			   opaque dock takes them, so nothing hidden under it is tappable. */
			pointer-events: none;
		}
		.call-dock-slot > :global(*) {
			pointer-events: auto;
		}
	}

	.transitioning-from-empty .message-area-wrapper {
		animation: messageAreaReveal 350ms cubic-bezier(0.16, 1, 0.3, 1) forwards;
	}

	/* The composer's own move from the hero to the dock is measured and played
	   from script (slideComposerToDock): a fixed keyframe cannot know where the
	   hero put it, or that a phone-width hero already docks it. */

	@media (prefers-reduced-motion: reduce) {
		.transitioning-from-empty .message-area-wrapper {
			animation: none;
		}
	}

	/* ========================================
	   EMPTY STATE
	   ======================================== */

	/* Hero region (workspace picker / cleared hint). Takes the top flex space with
	   its content bottom-anchored; the .empty-bottom-spacer below the input takes
	   the matching bottom flex space, so the hero + persistent input pair sits
	   vertically centered — reproducing the old .empty-state justify-content:center
	   grouping without wrapping the input in a state-specific ancestor (#665). */
	/* The hero and the spacer under the composer split the free height
	   equally (basis auto, not 0), and the hero's content sits at its
	   bottom: the hero and the composer are centred together. */
	.empty-hero {
		flex: 1 1 auto;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: flex-end;
		width: 100%;
		padding: 1rem 1rem 0;
		position: relative;
	}

	/* The transcript and composer's column, so hero and box share one edge. */
	.empty-hero-content {
		display: flex;
		flex-direction: column;
		align-items: center;
		width: 100%;
		max-width: var(--column-width);
	}

	/* A composer-free surface can center its hero on desktop. */
	.empty-hero.hero-only {
		justify-content: center;
		padding-bottom: 1rem;
	}

	/* A little more room below than above: the stack sits just over true centre. */
	.empty-bottom-spacer {
		flex: 1.15 1 auto;
	}
	/* A phone-width window keeps the composer under the thumb, with the greeting
	   sitting on it rather than floating mid-screen. */
	@media (max-width: 480px) {
		.empty-bottom-spacer { display: none; }
		.empty-hero:not(.hero-only) { justify-content: flex-end; padding-bottom: 8px; }
		.is-empty .input-slot.empty-input-layout { margin-top: 0; }
	}

	/* Overlay the error so it doesn't shift the vertically-centered content. */
	.empty-error-overlay {
		position: absolute;
		top: 1rem;
		left: 1rem;
		right: 1rem;
		z-index: 5;
	}

	/* Empty-state layout for the persistent input slot: centered and capped at the
	   same content width as the hero above it. Tailwind preflight makes
	   box-sizing:border-box global, so the cap includes the gutters — ChatInput
	   supplies its own dock padding inside, so none is added here. */
	.input-slot.empty-input-layout {
		align-self: center;
		width: 100%;
		max-width: calc(var(--column-width) + 3rem);
		margin-top: 0.75rem;
	}

	/* Cleared/empty existing-session hero. Reuses WorkspaceSelector's tokens
	   (font-display heading, muted-foreground secondary text) so it reads as the
	   same surface — just communicating "cleared" instead of "new". */
	.cleared-hero {
		display: flex;
		flex-direction: column;
		align-items: center;
		/* Full width, not its content's: the title's web font and the hint both
		   change while it opens, and that must not resize the hero (#1049). */
		width: 100%;
		gap: 8px;
		padding: 2rem 2rem 1.25rem;
		text-align: center;
		user-select: none;
	}

	.cleared-badge {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		margin-bottom: 10px;
		border-radius: 50%;
		color: var(--subtle-foreground);
		background: var(--scrim);
		border: 1px solid var(--border);
	}

	.cleared-title {
		font-family: var(--font-display, Georgia, serif);
		font-size: clamp(1.375rem, 4vw, 2rem);
		font-weight: 700;
		color: var(--foreground);
		letter-spacing: -0.02em;
		line-height: 1.15;
		max-width: 32ch;
		/* Long prompts double as titles — clamp to two lines with an ellipsis. */
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.cleared-hint {
		font-size: 0.875rem;
		color: var(--muted-foreground);
		line-height: 1.5;
		max-width: 40ch;
		/* Two lines at this line-height: the settled hint wraps to two, the
		   loading and stalled hints take one (#1049). */
		min-height: 3em;
	}

	.cleared-badge.is-error { color: var(--warning, var(--subtle-foreground)); }

	/* Stalled load: still exactly one line (the text ellipsises before the hint
	   could wrap), so swapping it in never moves the centred composer. */
	.cleared-hint.is-stalled {
		display: flex;
		align-items: baseline;
		justify-content: center;
		gap: 0.5rem;
		max-width: min(40ch, 100%);
		white-space: nowrap;
	}
	.cleared-hint .hint-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.hero-retry {
		position: relative;
		flex-shrink: 0;
		padding: 0;
		border: 0;
		background: none;
		color: var(--foreground);
		font: inherit;
		font-weight: 600;
		text-decoration: underline;
		text-decoration-color: color-mix(in oklch, var(--foreground) 35%, transparent);
		text-underline-offset: 3px;
		cursor: pointer;
		transition: text-decoration-color 150ms ease-out;
	}
	/* 44px touch target without growing the line. */
	.hero-retry::before { content: ''; position: absolute; inset: -12px -10px; }
	.hero-retry:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; border-radius: 4px; }
	@media (hover: hover) { .hero-retry:hover { text-decoration-color: var(--foreground); } }

	.cleared-hero.is-loading :global(.hero-spin) {
		animation: reconnect-spin 1s linear infinite;
	}
	@media (prefers-reduced-motion: reduce) {
		.cleared-hero.is-loading :global(.hero-spin) { animation: none; }
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
		background: var(--modal-overlay);
		backdrop-filter: blur(var(--modal-blur, 2px));
		-webkit-backdrop-filter: blur(var(--modal-blur, 2px));
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
		box-shadow: var(--shadow-dialog);
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

	.chat-view :global(.prose) {
		color: var(--foreground);
		line-height: 1.6;
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
		border-radius: var(--radius-card);
		margin: 12px 0;
		padding: 12px 16px;
	}
</style>

<script lang="ts">
	/**
	 * SessionCard — one ledger row in the sidebar.
	 *
	 * Every row leads with ONE glyph in a fixed left column: where the branch
	 * sits between edit and merge (see `deriveGitStage`). The session's own
	 * runtime state rides on the title — a pulsing dot while it works, an amber
	 * warning when it is parked on a prompt.
	 *
	 * Active card (three lines):
	 *   L1  glyph · title · total working time
	 *   L2  project · status word · PR state · diffstat
	 *   L3  device icon · branch
	 *
	 * Settled row (compact, two lines):
	 *   L1  glyph · title · total working time (age in the hover title)
	 *   L2  device icon · git-state word · branch
	 *
	 * Inbox gestures. A touch or pen swipe RIGHT settles an active row (or
	 * moves a settled one back to Active); a swipe LEFT archives. Past 40% of
	 * the row the lane goes solid and letting go commits; a fast flick commits
	 * too. The row then slides out and folds away. A press-and-hold starts
	 * selection, where a tap toggles the row's checkmark. A mouse never
	 * swipes: on a pointer that hovers, the same actions sit at the row's right
	 * edge on hover. Settle and Archive offer Undo. Rename, Fork, and Delete
	 * live on the header session-title menu.
	 */
	import { untrack } from 'svelte';
	import {
		Archive,
		ArchiveRestore,
		ArrowUpFromLine,
		Check,
		Cloud,
		Diff,
		GitCommitHorizontal,
		GitMerge,
		GitPullRequest,
		GitPullRequestClosed,
		MessageSquare,
		Monitor,
		TriangleAlert,
		Undo2
	} from 'lucide-svelte';
	import type { Session } from '$lib/api/client';
	import { appData } from '$lib/stores/app-data.svelte';
	import { assistantState } from '$lib/stores/assistant';
	import { git } from '$lib/stores/git.svelte';
	import { sessionGitState } from '$lib/stores/session-git-state.svelte';
	import { notify } from '$lib/stores/notifications';
	import { platform } from '$lib/stores/platform.svelte';
	import {
		GIT_STAGE_LABELS,
		GIT_STAGE_TONES,
		GIT_STAGE_TIPS,
		RUNTIME_STATUS_TONES,
		RUNTIME_STATUS_WORDS,
		deriveGitStage,
		diffCounts,
		isAttentionStatus,
		isLiveStatus,
		isTurnLiveStatus,
		sessionWorkMs,
		formatElapsed,
		formatAge,
		deviceLabel,
		branchLabel,
		sessionModeOf,
		sessionPullRequests,
		summarizePullRequests,
		sessionTimestamp,
		cardActions,
		withinTapTarget,
		type CardAction,
		type SessionCardStatus,
		type SessionGitFacts
	} from './session-card-data';
	import SessionPrList from './SessionPrList.svelte';

	interface Props {
		session: Session;
		status: SessionCardStatus;
		/** Branch PR lookup, independent of the runtime status. */
		openPr?: boolean;
		/** Display name of the session's project. */
		projectLabel: string;
		/** True when this session is the one open in the main panel. */
		active?: boolean;
		/** Current clock (ms) supplied by the sidebar's shared ticker. */
		now: number;
		/** Last-activity time (ms) precomputed by the sidebar's row builder, so a
		 *  ticking card never re-parses its date (R15). Falls back to parsing the
		 *  row when the card is used standalone. */
		timestamp?: number;
		/** Two-line dense row — used for the settled/archived groups. */
		compact?: boolean;
		/** The list is in selection mode: the lead glyph becomes a checkmark and
		 *  a tap toggles it instead of opening the session. */
		selecting?: boolean;
		selected?: boolean;
		onSelectToggle?: (sessionId: string) => void;
		/** Press-and-hold without moving. The sidebar starts selection with it. */
		onLongPress?: (sessionId: string) => void;
		onOpen: (sessionId: string) => void;
		/** Desktop only: after settling the open session, let the shell move to a fresh chat. */
		onSettled?: () => void;
	}

	let {
		session,
		status,
		openPr = false,
		projectLabel,
		active = false,
		now,
		timestamp,
		compact = false,
		selecting = false,
		selected = false,
		onSelectToggle,
		onLongPress,
		onOpen,
		onSettled
	}: Props = $props();

	const persistentAssistant = $derived($assistantState?.home_session_id === session.id);
	const live = $derived(isLiveStatus(status));
	// Only a turn holding the live slot ticks; a parked turn's interval has
	// ended, so its timer takes the plain style, not the live accent.
	const turnLive = $derived(isTurnLiveStatus(status));
	const lastActivity = $derived(timestamp ?? sessionTimestamp(session));
	// Every row shows the session's total working time, which survives
	// Done/Parked and settling. A settled row that is idle keeps its age in
	// the hover title, where it used to be the visible value.
	const timer = $derived(formatElapsed(sessionWorkMs(session, now, turnLive, lastActivity)));
	const timerTitle = $derived.by(() => {
		if (!compact || live) return 'Total working time';
		const age = formatAge(Math.max(0, now - lastActivity));
		return `Total working time · last activity ${age === 'now' ? 'just now' : `${age} ago`}`;
	});
	const title = $derived(session.title?.trim() || 'Untitled');
	const device = $derived(deviceLabel(session));
	const branch = $derived(branchLabel(session));
	const statusWord = $derived(RUNTIME_STATUS_WORDS[status]);
	const statusTone = $derived(RUNTIME_STATUS_TONES[status]);
	const attention = $derived(isAttentionStatus(status));
	// In-flight (working) rows that aren't the open session recede so the list
	// triages at a glance; prompt-parked / PR / done rows stay prominent.
	const receded = $derived(status === 'working' && !active);

	// ── Git state ───────────────────────────────────────────────────────
	// The session open in the main panel already has its checkout loaded by the
	// real git store; reading that costs nothing and stays in step with the
	// header. Every OTHER active card leases a slow poll from the sidebar's own
	// store. Settled rows lease nothing at all — they read `pr_state`.
	const openGit = $derived.by(() => {
		if (compact) return null;
		const current = git.current;
		// Unhydrated values belong to the previous checkout, not this session.
		if (!current?.hydrated) return null;
		if (current.sessionId === session.id &&
			current.projectId === session.project_id &&
			(current.worktreeId ?? null) === (session.worktree_id ?? null)) return current;
		if (
			active &&
			!current.sessionId &&
			current.projectId &&
			current.projectId === session.project_id &&
			(current.worktreeId ?? null) === (session.worktree_id ?? null)
		) {
			return current;
		}
		return null;
	});

	// A boolean, not `openGit` itself: the git store hands back a fresh object on
	// every one of its own updates, and depending on that object would tear the
	// lease down and re-fetch each time it blinked.
	const usesOpenGit = $derived(openGit !== null);

	$effect(() => {
		if (!openGit?.status || !session.project_id) return;
		sessionGitState.remember(session.id, session.project_id, session.worktree_id ?? null, {
			status: openGit.status, diffStats: openGit.diffStats, loaded: true
		});
	});

	// Lease on the checkout's identity, not on the row object: every session-list
	// reload (visibility return, reconnect, `resources_changed`) hands the card a
	// fresh `session`, and re-running the lease on that would release it — aborting
	// the in-flight poll — and immediately re-request the same checkout (#1282).
	// Primitive deriveds only notify when the value actually changes.
	const leaseSessionId = $derived(session.id);
	const leaseProjectId = $derived(session.project_id ?? null);
	const leaseWorktreeId = $derived(session.worktree_id ?? null);
	const leasing = $derived(!compact && !usesOpenGit);

	$effect(() => {
		if (!leasing) return;
		const id = leaseSessionId;
		const projectId = leaseProjectId;
		const worktreeId = leaseWorktreeId;
		return untrack(() => sessionGitState.watch(id, projectId, worktreeId));
	});

	const gitStatus = $derived.by(() => {
		if (compact) return null;
		if (openGit) return openGit.status;
		return sessionGitState.get(session.id, session.project_id ?? null, session.worktree_id ?? null)?.status ?? null;
	});
	const gitDiffStats = $derived.by(() => {
		if (compact) return null;
		if (openGit) return openGit.diffStats;
		return sessionGitState.get(session.id, session.project_id ?? null, session.worktree_id ?? null)?.diffStats ?? null;
	});
	const gitFacts = $derived.by<SessionGitFacts | null>(() => {
		const s = gitStatus;
		if (!s) return null;
		return {
			isRepo: s.is_git_repo,
			hasChanges: s.has_changes,
			ahead: s.ahead,
			unpushed: s.unpushed,
			matchesBaseTree: s.matches_base_tree,
			defaultBranch: s.default_branch
		};
	});

	// The sidebar can call a row `pr` from an open pull request it matched by
	// head branch, before the lifecycle is persisted on the row — trust that
	// rather than drawing a session with a live PR as a bare dot.
	const prState = $derived(
		session.pr_state ?? (openPr || status === 'pr' ? 'open' : status === 'pr_merged' ? 'merged' : null)
	);
	const stage = $derived(
		deriveGitStage({
			mode: sessionModeOf(session),
			hasProject: Boolean(session.project_id),
			prState,
			branch,
			git: gitFacts
		})
	);
	const stageTone = $derived(GIT_STAGE_TONES[stage]);
	const stageTip = $derived(GIT_STAGE_TIPS[stage]);
	// Every PR the chat (and its sub-agents) opened. One reads exactly like
	// the old single-PR chip; several collapse into counts by state.
	const prs = $derived(sessionPullRequests(session));
	const prSummary = $derived(
		summarizePullRequests(prs) ??
			(prState ? summarizePullRequests([{ number: 0, url: null, state: prState, title: null, branch: null, source: 'session' }]) : null)
	);
	const prText = $derived(prSummary?.text ?? null);
	const prTone = $derived(prSummary?.state ?? prState);
	const multiPr = $derived(prs.length > 1);
	// Follow-up edits do not erase the PR history. Show both facts.
	const showPr = $derived(Boolean(prText));
	let prListOpen = $state(false);
	// The chip is the one control for the list: a button, so the keyboard
	// and screen readers reach it too, and focus returns to it on close.
	let prChipRef = $state<HTMLButtonElement | null>(null);
	const prListLabel = $derived(
		prSummary ? `Pull requests: ${prSummary.text.replace(/ · /g, ', ')}` : 'Pull requests'
	);

	function togglePrList(event: MouseEvent) {
		event.stopPropagation();
		event.preventDefault();
		prListOpen = !prListOpen;
	}
	$effect(() => {
		if (!multiPr) prListOpen = false;
	});
	/** Compact rows carry one git word; a PR names its number, the rest use the
	 *  stage's own label ("uncommitted", "pushed", …). */
	const stageWord = $derived(
		(stage === 'pr' || stage === 'merged' || stage === 'closed') && prText
			? prText
			: GIT_STAGE_LABELS[stage]
	);
	// Diff counts belong to uncommitted work only — a merged branch's totals
	// would read as "still to review".
	const diff = $derived(gitStatus?.has_changes ? diffCounts(gitDiffStats) : null);


	const actions = $derived(cardActions(session));
	/** Hover order: Archive first, then the state toggle (or Restore). */
	const hoverActions = $derived([...actions].sort((a, b) => (a === 'archive' ? -1 : b === 'archive' ? 1 : 0)));
	/** What a right swipe does; a left swipe always archives. */
	const rightAction = $derived<CardAction>(
		actions.includes('restore') ? 'restore' : session.is_active ? 'settle' : 'activate'
	);
	const canArchive = $derived(actions.includes('archive'));

	const ACTION_META: Record<CardAction, { title: string; aria: string; label: string }> = {
		settle: { title: 'Settle session', aria: 'Settle session', label: 'Settle' },
		activate: { title: 'Move to Active', aria: 'Move session to Active', label: 'Move to Active' },
		archive: { title: 'Archive session', aria: 'Archive session', label: 'Archive' },
		restore: { title: 'Restore archived session', aria: 'Restore archived session', label: 'Restore' }
	};

	/** A short banner whose Undo reverts the change it reports. */
	function offerUndo(message: string, undo: () => Promise<unknown>) {
		const id = notify.banner(message, {
			severity: 'info',
			persist: false,
			duration_ms: 6000,
			action: {
				label: 'Undo',
				onClick: () => {
					notify.dismissLive(id);
					undo().catch((err) => reportFailure(err));
				}
			}
		});
	}

	function reportFailure(err: unknown) {
		// The server says why (owner offline, owner still syncing); pass that
		// on so the toast tells the user what to do instead of a bare failure.
		const detail = (err as { detail?: unknown } | null)?.detail;
		const reason = typeof detail === 'string' ? detail.trim() : '';
		notify(reason ? `Failed to update session: ${reason}` : 'Failed to update session', {
			severity: 'error'
		});
	}

	async function commitAction(action: CardAction) {
		if (persistentAssistant) return;
		const id = session.id;
		const wasActive = !!session.is_active;
		try {
			switch (action) {
				case 'settle':
					await appData.setSessionActive(id, false);
					// Desktop moves to a fresh composer. On mobile, keep the current
					// session and drawer open so settling does not navigate the user away.
					if (active && !platform.isMobile) onSettled?.();
					offerUndo(`Settled “${title}”`, () => appData.setSessionActive(id, true));
					break;
				case 'activate':
					await appData.setSessionActive(id, true);
					break;
				case 'archive':
					await appData.setSessionArchived(id, true);
					offerUndo(`Archived “${title}”`, () => appData.setSessionArchived(id, false, wasActive));
					break;
				case 'restore':
					await appData.setSessionArchived(id, false);
					break;
			}
		} catch (err) {
			reportFailure(err);
		}
	}

	// ── Gestures ────────────────────────────────────────────────────────
	/** Movement before a press picks an axis; less than this is still a tap. */
	const AXIS_LOCK = 8;
	/** Past this share of the row the lane arms and release commits. */
	const ARM_RATIO = 0.4;
	/** The row follows the finger 1:1 up to here, then with drag. */
	const EASE_RATIO = 0.62;
	/** A flick this fast (px/ms) over FLICK_MIN_DX commits short of the arm point. */
	const FLICK_VELOCITY = 0.9;
	const FLICK_MIN_DX = 60;
	const LONG_PRESS_MS = 480;
	/** Slide-out, then fold: the row height collapses at FOLD_AT and the
	 *  action runs at COMMIT_AT, once the row is gone. */
	const FOLD_AT = 170;
	const COMMIT_AT = 420;
	/** How long the lanes stay painted while a cancelled row springs back. */
	const LANE_LINGER = 280;
	/** jsdom has no layout; a 320px row stands in for the measured one. */
	const FALLBACK_WIDTH = 320;

	let frameRef = $state<HTMLElement | null>(null);
	let dragX = $state<number | null>(null);
	let lane = $state<'l' | 'r' | null>(null);
	let armed = $state(false);
	let pressing = $state(false);
	/** Set while the row slides out and folds: the inner offset, then the fold. */
	let slideX = $state<number | null>(null);
	let foldHeight = $state<number | null>(null);
	let folded = $state(false);

	let pointerId: number | null = null;
	let pointerType = '';
	let startX = 0;
	let startY = 0;
	let axis: 'x' | null = null;
	let rowWidth = FALLBACK_WIDTH;
	let lastX = 0;
	let lastT = 0;
	let velocity = 0;
	let longTimer: ReturnType<typeof setTimeout> | null = null;
	let laneTimer: ReturnType<typeof setTimeout> | null = null;
	let suppressNextClick = false;

	const dragging = $derived(dragX !== null);

	function clearLongPress() {
		if (longTimer) clearTimeout(longTimer);
		longTimer = null;
	}

	function isControlTarget(target: EventTarget | null): boolean {
		return target instanceof Element && !!target.closest('.hover-acts, .pr-pop, .pr-chip.multi, button, input, a');
	}

	function handlePointerDown(event: PointerEvent) {
		// Any fresh press clears the click guard the last gesture left behind.
		suppressNextClick = false;
		if (event.isPrimary === false || event.button !== 0 || isControlTarget(event.target)) return;
		if (slideX !== null || persistentAssistant) return;
		pointerId = event.pointerId;
		pointerType = event.pointerType;
		startX = lastX = event.clientX;
		startY = event.clientY;
		lastT = performance.now();
		velocity = 0;
		axis = null;
		rowWidth = frameRef?.offsetWidth || FALLBACK_WIDTH;
		if (pointerType !== 'mouse') pressing = true;
		clearLongPress();
		if (onLongPress) {
			longTimer = setTimeout(() => {
				longTimer = null;
				if (pointerId === null || axis) return;
				pressing = false;
				suppressNextClick = true;
				pointerId = null;
				onLongPress?.(session.id);
			}, LONG_PRESS_MS);
		}
	}

	function handlePointerMove(event: PointerEvent) {
		if (pointerId !== event.pointerId) return;
		let dx = event.clientX - startX;
		const dy = event.clientY - startY;
		if (!axis) {
			if (Math.max(Math.abs(dx), Math.abs(dy)) < AXIS_LOCK) return;
			clearLongPress();
			pressing = false;
			const horizontal = Math.abs(dx) > Math.abs(dy);
			const allowed = dx > 0 || canArchive;
			if (!horizontal || pointerType === 'mouse' || selecting || !allowed) {
				pointerId = null;
				return;
			}
			axis = 'x';
			if (laneTimer) clearTimeout(laneTimer);
			frameRef?.setPointerCapture?.(event.pointerId);
		}
		event.preventDefault();
		const t = performance.now();
		const elapsed = t - lastT;
		if (elapsed > 0) velocity = (event.clientX - lastX) / elapsed;
		lastX = event.clientX;
		lastT = t;
		// A row that cannot archive only travels right.
		if (!canArchive) dx = Math.max(0, dx);
		const lim = rowWidth * EASE_RATIO;
		dragX = Math.abs(dx) <= lim ? dx : Math.sign(dx) * (lim + (Math.abs(dx) - lim) * 0.35);
		lane = dx > 0 ? 'l' : dx < 0 ? 'r' : null;
		armed = Math.abs(dx) > rowWidth * ARM_RATIO;
	}

	function springBack() {
		dragX = null;
		if (laneTimer) clearTimeout(laneTimer);
		laneTimer = setTimeout(() => {
			laneTimer = null;
			if (dragX === null) {
				lane = null;
				armed = false;
			}
		}, LANE_LINGER);
	}

	function finishPointer(event: PointerEvent) {
		if (pointerId !== event.pointerId) return;
		clearLongPress();
		pressing = false;
		pointerId = null;
		if (axis !== 'x') return;
		axis = null;
		// The click this gesture leaves behind must not open the session.
		suppressNextClick = true;
		const dx = dragX ?? 0;
		const flick = Math.abs(velocity) > FLICK_VELOCITY && Math.abs(dx) > FLICK_MIN_DX;
		if (dx !== 0 && (armed || flick)) {
			const dir = dx < 0 ? -1 : 1;
			slideOut(dir, dir < 0 ? 'archive' : rightAction);
		} else {
			springBack();
		}
	}

	function cancelPointer(event: PointerEvent) {
		if (pointerId !== event.pointerId) return;
		clearLongPress();
		pressing = false;
		pointerId = null;
		if (axis === 'x') springBack();
		axis = null;
	}

	/** Slide the row off in the swipe direction, fold it shut, then act. The
	 *  timers are NOT cleared on destroy: the action must still run when the
	 *  list re-renders mid-fold. */
	function slideOut(dir: number, action: CardAction) {
		armed = true;
		lane = dir < 0 ? 'r' : 'l';
		dragX = null;
		slideX = dir * rowWidth;
		foldHeight = frameRef?.offsetHeight ?? null;
		setTimeout(() => (folded = true), FOLD_AT);
		setTimeout(async () => {
			await commitAction(action);
			// Still mounted (a failed update rolled back, or this row stays in its
			// group): put it back the way it was.
			slideX = null;
			foldHeight = null;
			folded = false;
			lane = null;
			armed = false;
		}, COMMIT_AT);
	}

	function openCard(event?: MouseEvent) {
		if (suppressNextClick) {
			suppressNextClick = false;
			return;
		}
		// A finger that lands just off the 16 px PR chip still means the chip:
		// touch taps within 44×44 px of it open the list, not the chat.
		if (
			event && !selecting && multiPr && prChipRef &&
			pointerType !== '' && pointerType !== 'mouse' &&
			withinTapTarget(prChipRef.getBoundingClientRect(), event.clientX, event.clientY)
		) {
			togglePrList(event);
			return;
		}
		if (selecting) {
			onSelectToggle?.(session.id);
			return;
		}
		onOpen(session.id);
	}

	function handleCardKeydown(event: KeyboardEvent) {
		if (event.target !== event.currentTarget) return;
		if (event.key === 'Enter' || event.key === ' ') {
			event.preventDefault();
			openCard();
		}
	}

	function handleActionClick(event: MouseEvent, action: CardAction) {
		event.stopPropagation();
		void commitAction(action);
	}

	const innerTransform = $derived(
		slideX !== null ? `translateX(${slideX}px)` : dragX !== null ? `translateX(${dragX}px)` : undefined
	);
</script>

<!-- No <svelte:window> here on purpose: with 60+ cards rendered, per-card
     global listeners ran 60+ times per document event (R15). -->
<div
	bind:this={frameRef}
	data-session-card={session.id}
	class="scard-frame"
	class:phone={platform.isMobile}
	class:compact
	class:active
	class:selected={selecting && selected}
	class:dragging
	class:armed
	class:pressing
	class:sliding={slideX !== null}
	class:folded
	style:height={foldHeight === null ? undefined : folded ? '0px' : `${foldHeight}px`}
	role="group"
	aria-label={`${title} session`}
	onpointerdown={handlePointerDown}
	onpointermove={handlePointerMove}
	onpointerup={finishPointer}
	onpointercancel={cancelPointer}
>
	{#if lane}
		<div class="lane l" aria-hidden="true" class:on={lane === 'l'}>
			{#if rightAction === 'settle'}
				<Check size={20} strokeWidth={2} /><span>Settle</span>
			{:else if rightAction === 'activate'}
				<Undo2 size={20} strokeWidth={2} /><span>Move to Active</span>
			{:else}
				<ArchiveRestore size={20} strokeWidth={2} /><span>Restore</span>
			{/if}
		</div>
		<div class="lane r" aria-hidden="true" class:on={lane === 'r'}>
			<span>Archive</span><Archive size={20} strokeWidth={2} />
		</div>
	{/if}
	<div
		class="scard"
		class:active
		class:receded
		class:compact
		role="button"
		tabindex="0"
		aria-current={active ? 'true' : undefined}
		aria-pressed={selecting ? selected : undefined}
		onclick={openCard}
		onkeydown={handleCardKeydown}
		title={title}
		style:transform={innerTransform}
	>
		{#if selecting}
			<span class="check" aria-hidden="true">
				{#if selected}<Check size={12} strokeWidth={3} />{/if}
			</span>
		{:else}
		<span class="lead {stageTone}" role="img" title={stageTip} aria-label={stageTip}>
			{#if stage === 'pr'}
				<GitPullRequest size={14} strokeWidth={2} />
			{:else if stage === 'merged'}
				<GitMerge size={14} strokeWidth={2} />
			{:else if stage === 'closed'}
				<GitPullRequestClosed size={14} strokeWidth={2} />
			{:else if stage === 'dirty'}
				<Diff size={14} strokeWidth={2} />
			{:else if stage === 'committed'}
				<GitCommitHorizontal size={14} strokeWidth={2} />
			{:else if stage === 'pushed'}
				<ArrowUpFromLine size={14} strokeWidth={2} />
			{:else if stage === 'none'}
				<MessageSquare size={14} strokeWidth={2} />
			{:else}
				<span class="stage-dot"></span>
			{/if}
		</span>
		{/if}

		<span class="body">
			<span class="l1">
				<span class="title">
					{#if status === 'working'}
						<span class="pip" aria-hidden="true"></span>
					{:else if attention}
						<TriangleAlert size={12} strokeWidth={2.2} class="warn" aria-hidden="true" />
					{/if}{#if compact && (status === 'working' || attention)}<span class="sr-only">{`${statusWord}: `}</span>{/if}{title}
				</span>
			</span>

			<!-- The working time sits at the end of the row that carries the
			     session's state: the status line on active rows, the meta line
			     on settled ones. The title keeps the full width. -->
			{#snippet multiPrChip()}
				<!-- The one control for the PR list: pointer, keyboard and
				     screen readers all use the chip itself. -->
				<button
					bind:this={prChipRef}
					type="button"
					class="st pr pr-chip multi"
					class:open={prListOpen}
					title={`${prText} — show the list`}
					aria-label={prListLabel}
					aria-haspopup="dialog"
					aria-expanded={prListOpen}
					onclick={togglePrList}
				>{#each prSummary?.parts ?? [] as part, i (part.state)}{#if i > 0}<span class="sep">{' · '}</span>{/if}<span class:green={part.state === 'open'} class:purple={part.state === 'merged'} class:muted={part.state === 'closed'}>{part.text}</span>{/each}</button>
			{/snippet}
			{#snippet timerChip()}
				<span class="timer" class:live={turnLive} title={timerTitle}>{timer}</span>
			{/snippet}

			{#if !compact}
				<span class="l2">
					<span class="st {statusTone}">{statusWord}{#if status === 'working'}<span class="working-dots" aria-hidden="true">
							<span></span><span></span><span></span>
						</span>{/if}</span>
					{#if showPr}
						<span class="dot" aria-hidden="true"></span>
						{#if multiPr}
							{@render multiPrChip()}
						{:else}
							<span class="st pr" title={prText} class:green={prTone === 'open'} class:purple={prTone === 'merged'}>{prText}</span>
						{/if}
					{/if}
					{#if diff}
						<span class="dot" aria-hidden="true"></span>
						<span class="diff"><span class="p">{diff.plus}</span> <span class="m">{diff.minus}</span></span>
					{/if}
					{@render timerChip()}
				</span>
			{/if}

			<span class="l3">
				<span class="seg device-seg" title={device} aria-label={device}>
					{#if session.is_remote}
						<Cloud size={12} strokeWidth={1.9} />
					{:else}
						<Monitor size={12} strokeWidth={1.9} />
					{/if}
				</span>
				{#if !compact}
					<span class="dot" aria-hidden="true"></span>
					<span class="seg proj-seg"><span class="proj">{projectLabel}</span></span>
				{/if}
				{#if compact && stageWord}
					<span class="dot" aria-hidden="true"></span>
					{#if multiPr && (stage === 'pr' || stage === 'merged' || stage === 'closed')}
						{@render multiPrChip()}
					{:else}
						<span class="st {stageTone}">{stageWord}</span>
					{/if}
				{/if}
				{#if branch}
					<span class="dot" aria-hidden="true"></span>
					<span class="seg branch-seg"><span class="branch">{branch}</span></span>
				{/if}
				{#if compact}{@render timerChip()}{/if}
			</span>
		</span>
	</div>

	{#if persistentAssistant}
		<span class="hover-acts persistent" title="Persistent assistant conversation">Persistent</span>
	{:else if !selecting}
		<!-- Hover actions on a pointer that hovers. On touch they stay in the
		     DOM for assistive tech, visually hidden: the swipe is the gesture. -->
		<span class="hover-acts" aria-label="Session actions">
			{#each hoverActions as action (action)}
				<button
					type="button"
					class="row-action"
					title={ACTION_META[action].title}
					aria-label={ACTION_META[action].aria}
					onclick={(e) => handleActionClick(e, action)}
				>
					{#if action === 'settle'}
						<Check size={15} strokeWidth={1.9} />
					{:else if action === 'activate'}
						<Undo2 size={15} strokeWidth={1.9} />
					{:else if action === 'archive'}
						<Archive size={15} strokeWidth={1.9} />
					{:else}
						<ArchiveRestore size={15} strokeWidth={1.9} />
					{/if}
				</button>
			{/each}
		</span>
	{/if}
	{#if prListOpen && prSummary && multiPr}
		<SessionPrList
			{prs}
			summary={prSummary}
			anchor={prChipRef}
			returnFocus={prChipRef}
			onClose={() => (prListOpen = false)}
		/>
	{/if}
</div>

<style>
	.scard-frame {
		position: relative;
		width: 100%;
		margin-bottom: 2px;
		border-radius: 12px;
		overflow: hidden;
		/* Vertical scroll stays native; a horizontal drag is the row's own. */
		touch-action: pan-y;
		transition:
			height 260ms cubic-bezier(0.16, 1, 0.3, 1),
			opacity 200ms ease,
			margin 260ms cubic-bezier(0.16, 1, 0.3, 1);
	}
	.scard-frame.folded {
		margin-bottom: 0;
		opacity: 0;
	}

	/* ── Swipe lanes, painted behind the row while it is dragged ───────── */
	.lane {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		gap: 9px;
		padding: 0 18px;
		font-size: 13px;
		font-weight: 600;
		opacity: 0;
		transition: background-color 150ms ease, color 150ms ease;
	}
	.lane.on { opacity: 1; }
	.lane.l {
		justify-content: flex-start;
		background: color-mix(in oklch, var(--primary) 28%, var(--sidebar));
		color: var(--primary);
	}
	.lane.r {
		justify-content: flex-end;
		background: color-mix(in oklch, var(--info) 22%, var(--sidebar));
		color: var(--info);
	}
	.lane span { opacity: 0.55; transition: opacity 150ms ease; }
	.lane :global(svg) { flex: none; transition: transform 180ms cubic-bezier(0.16, 1, 0.3, 1); }
	.scard-frame.armed .lane.l { background: var(--primary); color: var(--primary-foreground); }
	.scard-frame.armed .lane.r { background: var(--info); color: oklch(0.18 0.04 250); }
	.scard-frame.armed .lane span { opacity: 1; }
	.scard-frame.armed .lane :global(svg) { transform: scale(1.18); }

	/* ── The row itself ─────────────────────────────────────────────────
	   Opaque, so the lanes never bleed through. A fixed 18px lead column keeps
	   every glyph (and the selection checkmark) lined up down the list. */
	.scard {
		position: relative;
		z-index: 1;
		display: grid;
		grid-template-columns: 18px minmax(0, 1fr);
		column-gap: 8px;
		align-items: start;
		width: 100%;
		padding: 8px 10px 9px 9px;
		border: none;
		border-radius: 12px;
		background: var(--sidebar);
		color: inherit;
		text-align: left;
		cursor: pointer;
		transition: transform 300ms cubic-bezier(0.16, 1, 0.3, 1), background-color 150ms ease;
	}
	.scard-frame.phone .scard { column-gap: 10px; padding: 10px 10px 11px 9px; }
	.scard-frame.dragging .scard { transition: background-color 150ms ease; }
	.scard-frame:hover .scard { background: color-mix(in oklch, var(--sidebar-foreground) 5%, var(--sidebar)); }
	.scard.active,
	.scard-frame:hover .scard.active { background: color-mix(in oklch, var(--sidebar-foreground) 10%, var(--sidebar)); }
	.scard.active::before {
		content: '';
		position: absolute;
		left: 0;
		top: 10px;
		bottom: 10px;
		width: 2.5px;
		border-radius: 2px;
		background: var(--primary);
		box-shadow: 0 0 8px -1px var(--glow);
	}
	.scard.compact.active::before { top: 8px; bottom: 8px; }
	.scard-frame.pressing .scard {
		background: color-mix(in oklch, var(--sidebar-foreground) 8%, var(--sidebar));
		transform: scale(0.985);
	}
	.scard-frame.selected .scard,
	.scard-frame.selected:hover .scard { background: color-mix(in oklch, var(--primary) 9%, var(--sidebar)); }
	.scard:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	/* Selection checkmark, in the lead glyph's column. */
	.check {
		display: grid;
		place-items: center;
		width: 18px;
		height: 18px;
		margin-top: 1px;
		border: 1.5px solid var(--border-strong);
		border-radius: 50%;
		color: var(--primary-foreground);
	}
	.scard-frame.selected .check { background: var(--primary); border-color: var(--primary); }

	.scard.compact .l3 { margin-top: 3px; }

	/* ── Leading git glyph ───────────────────────────────────────────── */
	.lead {
		display: grid;
		place-items: center;
		height: 17px;
		color: var(--subtle-foreground);
	}
	.scard.compact .lead { height: 16px; }
	.lead :global(svg) { flex: none; }
	.lead.green { color: var(--success); }
	.lead.purple { color: var(--status-merged); }
	.lead.amber { color: var(--warning); }
	.lead.muted { color: var(--subtle-foreground); }
	/* "Repo, nothing to report" is a dot rather than an icon — it should read
	   as an absence, not as one more thing to decode. */
	.stage-dot {
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: currentColor;
		opacity: 0.75;
	}

	.body { min-width: 0; }

	.l1 {
		display: flex;
		align-items: baseline;
		gap: 8px;
		min-width: 0;
	}
	.title {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 13px;
		font-weight: 500;
		letter-spacing: -0.008em;
		color: var(--sidebar-foreground);
	}
	.scard:not(.compact) .title { font-size: 13.5px; font-weight: 600; }
	/* Runtime state rides IN the title, so it truncates with it and never
	   steals a column from the branch line. */
	.pip {
		display: inline-block;
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--primary);
		margin-right: 6px;
		vertical-align: 1px;
		box-shadow: 0 0 6px -1px var(--glow);
		animation: card-ping 1.8s ease-in-out infinite;
	}
	.title :global(.warn) {
		display: inline-block;
		color: var(--warning);
		margin-right: 5px;
		vertical-align: -2px;
		animation: card-pulse 2.4s ease-in-out infinite;
	}

	.dot {
		width: 3px;
		height: 3px;
		border-radius: 50%;
		background: var(--subtle-foreground);
		opacity: 0.6;
		flex: none;
	}

	@keyframes card-ping {
		0%, 100% { opacity: 1; transform: scale(1); }
		50% { opacity: 0.55; transform: scale(0.82); }
	}
	@keyframes card-pulse {
		0%, 100% { opacity: 0.75; }
		50% { opacity: 1; }
	}

	/* Pinned to the right end of its line; the segments before it give way. */
	.timer {
		flex: none;
		margin-left: auto;
		padding-left: 6px;
		font-family: var(--font-mono);
		font-variant-numeric: tabular-nums;
		font-size: 11px;
		color: var(--sidebar-muted);
		white-space: nowrap;
		transition: opacity 120ms ease;
	}
	.timer.live { color: var(--primary); }
	/* A long PR label ellipsizes before it can push the timer off the line. */
	.l2 .st.pr,
	.l3 .st.pr { flex: 0 1 auto; min-width: 0; white-space: nowrap; }
	/* Several PRs: each count keeps its state's tone; the chip opens the list. */
	/* A button that reads as inline text: no box, no font or line-height of
	   its own, so the row keeps its height in the normal and compact lines. */
	.pr-chip.multi {
		cursor: pointer;
		position: relative;
		margin: 0;
		padding: 0;
		border: 0;
		border-radius: 3px;
		background: none;
		font: inherit;
		font-weight: 500;
		line-height: inherit;
		letter-spacing: inherit;
		color: inherit;
		text-align: inherit;
		text-transform: none;
		appearance: none;
	}
	.l3 .pr-chip.multi { font-weight: 400; }
	.pr-chip.multi:focus-visible { outline: 2px solid var(--ring); outline-offset: -1px; }
	.pr-chip.multi .sep { color: var(--subtle-foreground); }
	.pr-chip.multi:hover > span:not(.sep),
	.pr-chip.multi.open > span:not(.sep) { text-decoration: underline dotted; text-underline-offset: 2px; }
	/* On the meta line the project and the branch share the squeeze, so
	   neither collapses to a stub. */
	.seg.proj-seg { flex: 0 1 auto; min-width: 4.5em; }
	.l3 .timer { font-size: 10.5px; }

	/* Receded rows dim the title + meta line but keep the status line, glyph
	   and timer lit; hover (desktop) restores the whole row. */
	.scard.receded .title,
	.scard.receded .l3 { opacity: 0.62; transition: opacity 0.13s ease; }
	.scard-frame:hover .scard.receded .title,
	.scard-frame:hover .scard.receded .l3,
	.scard-frame:has(:focus-visible) .scard.receded .title,
	.scard-frame:has(:focus-visible) .scard.receded .l3 { opacity: 1; }

	/* Line 2 (active cards): what the agent is doing · what the PR is doing ·
	   how much has changed. */
	.l2 {
		display: flex;
		align-items: center;
		gap: 6px;
		margin-top: 4px;
		min-width: 0;
		font-size: 11.5px;
		white-space: nowrap;
		overflow: hidden;
	}
	.st {
		flex: none;
		font-weight: 500;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.st.accent { color: var(--primary); }
	.st.amber { color: var(--warning); }
	.st.green { color: var(--success); }
	.st.purple { color: var(--status-merged); }
	.st.muted { color: var(--muted-foreground); }
	.working-dots {
		display: inline-flex;
		align-items: center;
		gap: 2px;
		margin-left: 4px;
		vertical-align: middle;
	}
	.working-dots > span {
		width: 3px;
		height: 3px;
		border-radius: 50%;
		background: currentColor;
		opacity: 0.65;
		animation: working-dot-pulse 1.2s ease-in-out infinite;
	}
	.working-dots > span:nth-child(2) { animation-delay: 0.16s; }
	.working-dots > span:nth-child(3) { animation-delay: 0.32s; }
	@keyframes working-dot-pulse {
		0%, 60%, 100% { opacity: 0.35; }
		30% { opacity: 1; }
	}
	.diff {
		flex: none;
		font-family: var(--font-mono);
		font-size: 10.5px;
		font-variant-numeric: tabular-nums;
	}
	.diff .p { color: var(--success); }
	.diff .m { color: var(--destructive); }

	.l3 {
		margin-top: 4px;
		display: flex;
		align-items: center;
		gap: 5px;
		font-size: 11px;
		color: var(--sidebar-muted);
		min-width: 0;
		flex-wrap: nowrap;
		overflow: hidden;
	}
	.l3 :global(svg) { color: var(--subtle-foreground); flex: none; }
	.l3 .st { font-size: 10.5px; font-weight: 400; }
	.seg {
		display: inline-flex;
		align-items: center;
		gap: 3.5px;
		min-width: 0;
		flex: none;
		white-space: nowrap;
	}
	/* Branch gives back space first: it grows to fit long names when line 3
	   has room and only ellipsizes once the row actually fills up. */
	.seg.branch-seg {
		flex: 0 1 auto;
	}
	.proj {
		display: block;
		min-width: 0;
		max-width: 96px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--sidebar-muted);
		font-weight: 400;
	}
	.device-seg {
		flex: none;
	}
	.branch {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--font-mono);
		font-size: 10px;
	}

	/* Phone type is a step larger: thumbs, arm's length. */
	.scard-frame.phone .scard:not(.compact) .title { font-size: 14.5px; }
	.scard-frame.phone .scard.compact .title { font-size: 14px; }
	.scard-frame.phone .l2 { font-size: 12px; }
	.scard-frame.phone .l3 { font-size: 11.5px; }
	.scard-frame.phone .branch { font-size: 11px; }

	/* ── Hover actions (pointer that hovers) ───────────────────────────── */
	.hover-acts {
		position: absolute;
		top: 50%;
		right: 6px;
		z-index: 2;
		display: flex;
		gap: 2px;
		padding-left: 18px;
		transform: translateY(-50%);
		background: linear-gradient(
			90deg,
			transparent,
			color-mix(in oklch, var(--sidebar-foreground) 5%, var(--sidebar)) 18px
		);
		opacity: 0;
		transition: opacity 120ms ease;
	}
	.scard-frame:hover .hover-acts,
	.hover-acts:focus-within { opacity: 1; }
	.scard-frame.active .hover-acts {
		background: linear-gradient(
			90deg,
			transparent,
			color-mix(in oklch, var(--sidebar-foreground) 10%, var(--sidebar)) 18px
		);
	}
	.hover-acts.persistent {
		padding: 0 6px 0 18px;
		font-size: 11px;
		color: var(--sidebar-muted);
	}
	.row-action {
		display: grid;
		place-items: center;
		width: 28px;
		height: 28px;
		padding: 0;
		border: 0;
		border-radius: 7px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.row-action:hover { background: var(--sidebar-accent-active); color: var(--foreground); }
	.row-action:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	/* Touch: the swipe is the gesture, so the buttons leave the screen but
	   stay for screen readers. */
	.scard-frame.phone .hover-acts,
	.scard-frame.dragging .hover-acts,
	.scard-frame.sliding .hover-acts {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
		border: 0;
	}
	@media (hover: none) {
		.hover-acts {
			position: absolute;
			width: 1px;
			height: 1px;
			padding: 0;
			margin: -1px;
			overflow: hidden;
			clip: rect(0 0 0 0);
			white-space: nowrap;
			border: 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.pip,
		.working-dots > span,
		.title :global(.warn) { animation: none; }
		.scard-frame,
		.scard,
		.lane,
		.lane :global(svg),
		.hover-acts { transition: none; }
	}
</style>

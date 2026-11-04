<script lang="ts">
	/**
	 * SessionCard — one ledger row in the sidebar.
	 *
	 * Every row is one line: the title flush left, then a mark and the total
	 * working time at the far right. The mark is the session's runtime
	 * state when it has one (a pulsing dot while it works, a warning when it is
	 * parked on a prompt or failed), and otherwise where the branch sits between
	 * edit and merge (see `deriveGitStage`).
	 *
	 * An uncommitted diffstat or a several-PR summary sits before the time. A
	 * second line appears only on an active row that is running or blocked: the
	 * status word, PR state and diffstat.
	 *
	 * Worker, project, branch and PR state live in the row's hover title and in a
	 * visually hidden line for screen readers. The sidebar groups rows by project.
	 *
	 * Inbox gestures. A touch or pen swipe LEFT settles an active row (moves a
	 * settled one back to Active, restores an archived one); a swipe RIGHT
	 * archives. Past 40% of
	 * the row the lane goes solid and letting go commits; a fast flick commits
	 * too. The row then slides out and folds away. A press-and-hold starts
	 * selection, where a tap toggles the row's checkmark. A mouse never
	 * swipes: on a pointer that hovers, the same actions sit at the row's right
	 * edge on hover. Archive offers Undo; Settle does not, since a settled
	 * session is one tap from Active. Rename, Fork, and Delete
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
		Monitor,
		Undo2
	} from 'lucide-svelte';
	import type { Session } from '$lib/api/client';

	/** Every status mark on a row is drawn at this one weight. */
	const MARK_STROKE = 1.5;

	import { appData } from '$lib/stores/app-data.svelte';
	import { git } from '$lib/stores/git.svelte';
	import { sessionGitState } from '$lib/stores/session-git-state.svelte';
	import { notify } from '$lib/stores/notifications';
	import { platform } from '$lib/stores/platform.svelte';
	import {
		GIT_STAGE_LABELS,
		GIT_STAGE_TONES,
		GIT_STAGE_TIPS,
		RUNTIME_STATUS_TONES,
		runtimeStatusWord,
		deriveGitStage,
		diffCounts,
		isAttentionStatus,
		isLiveStatus,
		isTurnLiveStatus,
		isWorkCountingStatus,
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
		/** Its conversation is being compacted right now (known for the open
		 *  session only): the status word reads "Compacting". */
		compacting?: boolean;
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
		compacting = false,
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

	const live = $derived(isLiveStatus(status));
	// Only a turn holding the live slot ticks; a parked turn's interval has
	// ended, so its timer takes the plain style, not the live accent.
	const turnLive = $derived(isTurnLiveStatus(status));
	// A parked session is still working on its background work, so its total
	// keeps counting even though its timer keeps the plain style.
	const counting = $derived(isWorkCountingStatus(status));
	const lastActivity = $derived(timestamp ?? sessionTimestamp(session));
	// Every row shows the session's total working time, which survives Done
	// and settling. A settled row that is idle keeps its age in the hover
	// title, where it used to be the visible value.
	const timer = $derived(formatElapsed(sessionWorkMs(session, now, counting, lastActivity)));
	const timerTitle = $derived.by(() => {
		if (!compact || live) return 'Total working time';
		const age = formatAge(Math.max(0, now - lastActivity));
		return `Total working time · last activity ${age === 'now' ? 'just now' : `${age} ago`}`;
	});
	const title = $derived(session.title?.trim() || 'Untitled');
	const device = $derived(deviceLabel(session));
	const branch = $derived(branchLabel(session));
	const statusWord = $derived(runtimeStatusWord(status, compacting));
	const statusTone = $derived(RUNTIME_STATUS_TONES[status]);
	const attention = $derived(isAttentionStatus(status));
	const failed = $derived(status === 'failed');
	// Only these rows speak up: the open one, and any that is running or waiting
	// on someone. A finished row stays in secondary text so the eye goes to them.
	const running = $derived(status === 'working' || status === 'parked');
	const emphasized = $derived(active || running || attention || failed);
	const showStatusLine = $derived(!compact && (running || attention || failed));

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


	// Everything a row no longer spells out stays one hover away.
	const rowDetails = $derived(
		[statusWord, prText || (compact ? stageWord : null), device, projectLabel, branch].filter(Boolean).join(' · ')
	);
	const rowTip = $derived(`${title}\n${rowDetails}`);
	// A row with no recorded work has no time worth a column.
	const showTimer = $derived(turnLive || counting || timer !== formatElapsed(0));

	const actions = $derived(cardActions(session));
	/** Hover order: Archive first, then the state toggle (or Restore). */
	const hoverActions = $derived([...actions].sort((a, b) => (a === 'archive' ? -1 : b === 'archive' ? 1 : 0)));
	/** What a left swipe does (the state toggle); a right swipe always archives. */
	const leftAction = $derived<CardAction>(
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
		const id = session.id;
		const wasActive = !!session.is_active;
		try {
			switch (action) {
				case 'settle':
					await appData.setSessionActive(id, false);
					// Desktop moves to a fresh composer. On mobile, keep the current
					// session and drawer open so settling does not navigate the user away.
					if (active && !platform.isMobile) onSettled?.();
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
		if (slideX !== null) return;
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
			const allowed = dx < 0 || canArchive;
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
		// A row that cannot archive only travels left.
		if (!canArchive) dx = Math.min(0, dx);
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
			slideOut(dir, dir < 0 ? leftAction : 'archive');
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
		<!-- Lane `l` is uncovered by a right swipe, lane `r` by a left one. -->
		<div class="lane l archive" aria-hidden="true" class:on={lane === 'l'}>
			<Archive size={20} strokeWidth={2} /><span>Archive</span>
		</div>
		<div class="lane r toggle" aria-hidden="true" class:on={lane === 'r'}>
			{#if leftAction === 'settle'}
				<span>Settle</span><Check size={20} strokeWidth={2} />
			{:else if leftAction === 'activate'}
				<span>Move to Active</span><Undo2 size={20} strokeWidth={2} />
			{:else}
				<span>Restore</span><ArchiveRestore size={20} strokeWidth={2} />
			{/if}
		</div>
	{/if}
	<div
		class="scard"
		class:active
		class:emphasized
		class:compact
		role="button"
		tabindex="0"
		aria-current={active ? 'true' : undefined}
		aria-pressed={selecting ? selected : undefined}
		onclick={openCard}
		onkeydown={handleCardKeydown}
		title={rowTip}
		style:transform={innerTransform}
	>
		{#if selecting}
			<span class="check" aria-hidden="true">
				{#if selected}<Check size={12} strokeWidth={3} />{/if}
			</span>
		{/if}

		<span class="body">
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
			{#snippet mark()}
				<span class="lead {stageTone}" role="img" title={stageTip} aria-label={stageTip}>
					{#if status === 'working'}
						<span class="pip" aria-hidden="true"></span>
					{:else if failed}
						<span class="pip warn failed" aria-hidden="true"></span>
					{:else if attention}
						<span class="pip warn" aria-hidden="true"></span>
					{:else if stage === 'pr'}
						<GitPullRequest size={12} strokeWidth={MARK_STROKE} />
					{:else if stage === 'merged'}
						<GitMerge size={12} strokeWidth={MARK_STROKE} />
					{:else if stage === 'closed'}
						<GitPullRequestClosed size={12} strokeWidth={MARK_STROKE} />
					{:else if stage === 'dirty'}
						<Diff size={12} strokeWidth={MARK_STROKE} />
					{:else if stage === 'committed'}
						<GitCommitHorizontal size={12} strokeWidth={MARK_STROKE} />
					{:else if stage === 'pushed'}
						<ArrowUpFromLine size={12} strokeWidth={MARK_STROKE} />
					{/if}
					<!-- An idle row draws nothing, and takes no room (see .lead). -->
				</span>
			{/snippet}
			{#snippet timerChip()}
				{#if showTimer}<span class="timer" class:live={turnLive} title={timerTitle}>{timer}</span>{/if}
			{/snippet}

			<span class="l1">
				<span class="title">{#if compact && (status === 'working' || failed || attention)}<span class="sr-only">{`${statusWord}: `}</span>{/if}{title}</span>
				{#if !showStatusLine && multiPr}{@render multiPrChip()}{/if}
				{#if !showStatusLine && diff}
					<span class="diff"><span class="p">{diff.plus}</span> <span class="m">{diff.minus}</span></span>
				{/if}
				{#if !selecting}{@render mark()}{/if}
				{@render timerChip()}
			</span>

			{#if showStatusLine}
				<span class="l2">
					<span class="st {statusTone}">{statusWord}</span>
					{#if showPr}
						{#if multiPr}
							{@render multiPrChip()}
						{:else}
							<span class="st pr" title={prText} class:green={prTone === 'open'} class:purple={prTone === 'merged'}>{prText}</span>
						{/if}
					{/if}
					{#if diff}
						<span class="diff"><span class="p">{diff.plus}</span> <span class="m">{diff.minus}</span></span>
					{/if}
				</span>
			{/if}

			<span class="details sr-only">{rowDetails}</span>
		</span>
	</div>

	{#if !selecting}
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
						<Check size={14} strokeWidth={1.75} />
					{:else if action === 'activate'}
						<Undo2 size={14} strokeWidth={1.75} />
					{:else if action === 'archive'}
						<Archive size={14} strokeWidth={1.75} />
					{:else}
						<ArchiveRestore size={14} strokeWidth={1.75} />
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
		border-radius: var(--radius-control);
		overflow: hidden;
		/* Vertical scroll stays native; a horizontal drag is the row's own. */
		touch-action: pan-y;
		transition:
			height 260ms cubic-bezier(0.16, 1, 0.3, 1),
			opacity 200ms ease;
	}
	.scard-frame.folded {
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
	/* Position follows the swipe; colour follows the action it commits. */
	.lane.l { justify-content: flex-start; }
	.lane.r { justify-content: flex-end; }
	.lane.toggle {
		background: color-mix(in oklab, var(--primary) 28%, var(--sidebar));
		color: var(--primary);
	}
	.lane.archive {
		background: color-mix(in oklab, var(--info) 22%, var(--sidebar));
		color: var(--info);
	}
	.lane span { opacity: 0.55; transition: opacity 150ms ease; }
	.lane :global(svg) { flex: none; transition: transform 180ms cubic-bezier(0.16, 1, 0.3, 1); }
	.scard-frame.armed .lane.toggle { background: var(--primary); color: var(--primary-foreground); }
	.scard-frame.armed .lane.archive { background: var(--info); color: oklch(0.18 0.04 250); }
	.scard-frame.armed .lane span { opacity: 1; }
	.scard-frame.armed .lane :global(svg) { transform: scale(1.18); }

	/* ── The row itself ─────────────────────────────────────────────────
	   Opaque, so the lanes never bleed through. A fixed lead column keeps every
	   mark (and the selection checkmark) lined up down the list. 32px plus the
	   list's 2px gap is the 34px pitch. */
	.scard {
		position: relative;
		z-index: 1;
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		column-gap: 8px;
		align-items: start;
		width: 100%;
		padding: 6px 8px 6px 12px;
		border: none;
		border-radius: var(--radius-control);
		background: var(--sidebar);
		color: inherit;
		text-align: left;
		cursor: pointer;
		transition: transform 300ms cubic-bezier(0.16, 1, 0.3, 1), background-color 120ms ease;
	}
	/* Selecting lends the row a checkbox column; titles move over only then. */
	.scard:has(> .check) { grid-template-columns: 16px minmax(0, 1fr); }
	.scard-frame.dragging .scard { transition: background-color 120ms ease; }
	.scard-frame:hover .scard { background: var(--row-hover); }
	.scard.active,
	.scard-frame:hover .scard.active,
	.scard-frame.pressing .scard { background: var(--row-selected); }
	.scard-frame.pressing .scard { transform: scale(0.985); }
	.scard-frame.selected .scard,
	.scard-frame.selected:hover .scard { background: color-mix(in oklab, var(--primary) 9%, var(--sidebar)); }
	.scard:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	/* Selection checkmark, in the lead mark's column. */
	.check {
		display: grid;
		place-items: center;
		width: 16px;
		height: 16px;
		margin-top: 2px;
		border: 1.5px solid var(--border-strong);
		border-radius: 50%;
		color: var(--primary-foreground);
	}
	.scard-frame.selected .check { background: var(--primary); border-color: var(--primary); }

	/* ── Leading mark ─────────────────────────────────────────────────── */
	.lead {
		display: grid;
		flex: none;
		place-items: center;
		width: 16px;
		height: 20px;
		color: var(--subtle-foreground);
	}
	.lead:not(:has(> *)) { display: none; }
	.lead :global(svg) { flex: none; }
	.lead.green { color: var(--success); }
	.lead.purple { color: var(--status-merged); }
	.lead.amber { color: var(--warning); }
	.lead.muted { color: var(--subtle-foreground); }
	/* One 6px dot for all three live states. Screen readers get the word from the
	   lead's label, and the title reads it out in compact rows. */
	.pip {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--primary);
		animation: card-ping 1.8s ease-in-out infinite;
	}
	.pip.warn {
		background: var(--warning);
		animation: card-pulse 2.4s ease-in-out infinite;
	}
	/* What happened, not something still calling: the danger colour, held still. */
	.pip.warn.failed {
		background: var(--destructive);
		animation: none;
	}

	@keyframes card-ping {
		0%, 100% { opacity: 1; transform: scale(1); }
		50% { opacity: 0.55; transform: scale(0.82); }
	}
	@keyframes card-pulse {
		0%, 100% { opacity: 0.75; }
		50% { opacity: 1; }
	}

	.body { min-width: 0; }

	.l1 {
		display: flex;
		align-items: baseline;
		gap: 8px;
		min-width: 0;
		line-height: 20px;
	}
	.title {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 13.5px;
		font-weight: 450;
		color: var(--sidebar-muted);
		transition: color 120ms ease;
	}
	.scard-frame:hover .title { color: var(--sidebar-foreground); }
	.scard.emphasized .title {
		font-weight: 500;
		color: var(--sidebar-foreground);
	}

	/* Pinned to the right end of the line; the title gives way first. */
	.timer {
		flex: none;
		margin-left: auto;
		font-variant-numeric: tabular-nums;
		font-size: var(--fs-label);
		color: var(--subtle-foreground);
		white-space: nowrap;
	}
	.timer.live { color: var(--primary); }

	/* Several PRs: each count keeps its state's tone; the chip opens the list.
	   A button that reads as inline text: no box, no font or line-height of its
	   own, so the row keeps its height. */
	.pr-chip.multi {
		cursor: pointer;
		position: relative;
		flex: 0 1 auto;
		min-width: 0;
		margin: 0;
		padding: 0;
		border: 0;
		border-radius: 3px;
		background: none;
		font: inherit;
		font-size: var(--fs-label);
		line-height: inherit;
		color: var(--sidebar-muted);
		text-align: inherit;
		text-transform: none;
		white-space: nowrap;
		appearance: none;
	}
	.pr-chip.multi:focus-visible { outline: 2px solid var(--ring); outline-offset: -1px; }
	.pr-chip.multi .sep { color: var(--subtle-foreground); }
	.pr-chip.multi:hover > span:not(.sep),
	.pr-chip.multi.open > span:not(.sep) { text-decoration: underline dotted; text-underline-offset: 2px; }

	/* Second line, only on a running or blocked row: what it is doing or waiting
	   on, then what the PR and the diff are doing. */
	.l2 {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		padding-bottom: 1px;
		font-size: var(--fs-label);
		line-height: 17px;
		white-space: nowrap;
		overflow: hidden;
	}
	.st {
		flex: none;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.l2 .st.pr { flex: 0 1 auto; min-width: 0; }
	.st.accent { color: var(--primary); }
	.st.amber { color: var(--warning); }
	.st.danger { color: var(--destructive); }
	.st.green { color: var(--success); }
	.st.purple { color: var(--status-merged); }
	.st.muted { color: var(--muted-foreground); }
	.diff {
		flex: none;
		font-size: var(--fs-label);
		font-variant-numeric: tabular-nums;
	}
	.diff .p { color: var(--success); }
	.diff .m { color: var(--destructive); }

	/* Phone: a 44px row and a larger title for thumbs at arm's length. */
	.scard-frame.phone .scard { column-gap: 10px; padding: 12px; }
	.scard-frame.phone .title { font-size: 14.5px; }
	.scard-frame.phone .l2 { font-size: 13px; line-height: 18px; }

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
		background: linear-gradient(90deg, transparent, var(--row-hover) 18px);
		opacity: 0;
		transition: opacity 120ms ease;
	}
	.scard-frame:hover .hover-acts,
	.hover-acts:focus-within { opacity: 1; }
	.scard-frame.active .hover-acts {
		background: linear-gradient(90deg, transparent, var(--row-selected) 18px);
	}
	.row-action {
		display: grid;
		place-items: center;
		width: 24px;
		height: 24px;
		padding: 0;
		border: 0;
		border-radius: 6px;
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
		.pip.warn { animation: none; }
		.scard-frame,
		.scard,
		.lane,
		.lane :global(svg),
		.hover-acts { transition: none; }
	}
</style>

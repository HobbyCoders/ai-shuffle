<script lang="ts">
	/**
	 * SessionCard — one ledger row in the sidebar.
	 *
	 * Every row reads at a glance, without hovering, and every row is two lines,
	 * so a row never changes height when it starts or stops working.
	 *
	 * A fixed state column runs down the left of every row in every lane. Runtime
	 * state wins it: a pulsing dot while working, a ring while parked on background
	 * work, amber when it waits on someone, red when it failed. Otherwise it shows
	 * where the branch sits between edit and merge (see `deriveGitStage`), and an
	 * idle row leaves it empty but keeps its width.
	 *
	 * Line one is the title and the total working time. Line two is one calm line
	 * of facts: status word, PR state, diffstat, then branch, the only segment that
	 * gives way. PR state stays there while the row works, which is the whole point
	 * of keeping it out of the state column. A row with no facts shows its age, so
	 * the line is never blank. The Settled lane is the same row, quieter: no status
	 * word, and colour only on the PR.
	 *
	 * Line two is hidden from screen readers segment by segment; `.details` says
	 * the same facts once, in order, with the several-PR chip speaking for itself.
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
		Diff,
		GitCommitHorizontal,
		GitMerge,
		GitPullRequest,
		GitPullRequestClosed,
		Monitor,
		Undo2
	} from 'lucide-svelte';
	import type { Session } from '$lib/api/client';

	/** Every status mark on a row is drawn at this one size and weight. */
	const MARK_SIZE = 14;
	const MARK_STROKE = 1.5;

	import { appData } from '$lib/stores/app-data.svelte';
	import { git } from '$lib/stores/git.svelte';
	import { sessionGitState } from '$lib/stores/session-git-state.svelte';
	import { notify } from '$lib/stores/notifications';
	import { platform } from '$lib/stores/platform.svelte';
	import {
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
		gitProgressWord,
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
		/** The quieter, tighter row of the settled/archived groups. */
		compact?: boolean;
		/** The row is not under a project header in a list that spans several
		 *  projects, so it has to name its own. */
		showProject?: boolean;
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
		showProject = false,
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
	const parked = $derived(status === 'parked');
	// Only these rows speak up: the open one, and any that is running or waiting
	// on someone.
	const running = $derived(status === 'working' || parked);
	const emphasized = $derived(active || running || attention || failed);
	// Only a word worth reading. "Done" and "Idle" would only fill space, and a
	// settled row answers how it ended, not what it is doing.
	const showStatusWord = $derived(!compact && (running || attention || failed || status === 'offline'));

	// ── Git state ───────────────────────────────────────────────────────
	// The session open in the main panel already has its checkout loaded by the
	// real git store; reading that costs nothing and stays in step with the
	// header. Every OTHER active card leases a slow poll from the sidebar's own
	// store. Settled rows lease nothing at all: they read `pr_state`, plus the
	// last answer this tab already holds for the checkout, if any.
	const openGit = $derived.by(() => {
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
		if (openGit) return openGit.status;
		return sessionGitState.get(session.id, session.project_id ?? null, session.worktree_id ?? null)?.status ?? null;
	});
	const gitDiffStats = $derived.by(() => {
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
	// Diff counts belong to uncommitted work only — a merged branch's totals
	// would read as "still to review". A settled row says the word instead.
	const diff = $derived(!compact && gitStatus?.has_changes ? diffCounts(gitDiffStats) : null);
	const diffText = $derived(diff ? `${diff.plus} ${diff.minus}` : null);
	// Numbers already say "uncommitted", so the word only stands in without them.
	const progressWord = $derived(diff ? null : gitProgressWord(stage, !!gitStatus?.has_changes));

	const remote = $derived(!!session.is_remote);
	const whereText = $derived([showProject ? projectLabel : null, branch].filter(Boolean).join(' · ') || null);
	// Nothing to report: the line holds the row's age (and its worker's name, with
	// room to spell it) so its rhythm stays and it never goes blank.
	const factless = $derived(!showStatusWord && !showPr && !diff && !progressWord && !whereText);
	const ageText = $derived.by(() => {
		if (!factless) return null;
		const age = formatAge(Math.max(0, now - lastActivity));
		return age === 'now' ? 'Just now' : `${age} ago`;
	});
	const fallbackText = $derived(factless ? [remote ? device : null, ageText].filter(Boolean).join(' · ') : null);

	// The row's whole story, for the hover tip.
	const rowFacts = $derived([statusWord, prText, progressWord, diffText, device, projectLabel, branch, ageText]);
	const rowDetails = $derived(rowFacts.filter(Boolean).join(' · '));
	const rowTip = $derived(`${title}\n${rowDetails}`);
	// The same for a screen reader, which already hears the several-PR chip by name.
	const rowSpeech = $derived(
		rowFacts.filter((fact, i) => fact && !(multiPr && i === 1)).join(' · ')
	);
	// What the state column is showing, for whoever hovers it.
	const leadTip = $derived(running || attention || failed ? statusWord : stageTip);
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

		<!-- The state column. Every row has one, the same width in every lane, so
		     the eye runs straight down it. `.details` says all of this in words. -->
		{#if selecting}
			<span class="check" aria-hidden="true">
				{#if selected}<Check size={12} strokeWidth={3} />{/if}
			</span>
		{:else}
			<span class="lead {stageTone}" aria-hidden="true" title={leadTip}>
				{#if status === 'working'}
					<span class="pip"></span>
				{:else if parked}
					<span class="pip held"></span>
				{:else if failed}
					<span class="pip warn failed"></span>
				{:else if attention}
					<span class="pip warn"></span>
				{:else if stage === 'pr'}
					<GitPullRequest size={MARK_SIZE} strokeWidth={MARK_STROKE} />
				{:else if stage === 'merged'}
					<GitMerge size={MARK_SIZE} strokeWidth={MARK_STROKE} />
				{:else if stage === 'closed'}
					<GitPullRequestClosed size={MARK_SIZE} strokeWidth={MARK_STROKE} />
				{:else if stage === 'dirty'}
					<Diff size={MARK_SIZE} strokeWidth={MARK_STROKE} />
				{:else if stage === 'committed'}
					<GitCommitHorizontal size={MARK_SIZE} strokeWidth={MARK_STROKE} />
				{:else if stage === 'pushed'}
					<ArrowUpFromLine size={MARK_SIZE} strokeWidth={MARK_STROKE} />
				{/if}
				<!-- An idle row draws nothing but keeps the column. -->
			</span>
		{/if}

		<span class="body">
			<span class="l1">
				<span class="title">{title}</span>
				{#if showTimer}<span class="timer" class:live={turnLive} title={timerTitle}>{timer}</span>{/if}
			</span>

			<!-- Always here, so a row keeps its height through every state. -->
			<span class="l2">
				{#if showStatusWord}<span class="st {statusTone}" aria-hidden="true">{statusWord}</span>{/if}
				{#if showPr}
					{#if multiPr}
						{@render multiPrChip()}
					{:else}
						<span class="st pr" aria-hidden="true" class:green={prTone === 'open'} class:purple={prTone === 'merged'} class:muted={prTone === 'closed'}>{prText}</span>
					{/if}
				{/if}
				<!-- Whatever no longer fits drops whole, last first (see .rest). -->
				<span class="rest">
					{#if diff}
						<span class="diff" aria-hidden="true"><span class="p">{diff.plus}</span> <span class="m">{diff.minus}</span></span>
					{:else if progressWord}
						<span class="st progress" class:uncommitted={!compact && stage === 'dirty'} aria-hidden="true">{progressWord}</span>
					{/if}
					{#if whereText}
						<span class="where" aria-hidden="true">{whereText}</span>
					{:else if fallbackText}
						<span class="where" aria-hidden="true">{fallbackText}</span>
					{/if}
				</span>
				{#if remote && !factless}
					<span class="worker" aria-hidden="true" title={`On ${device}`}><Monitor size={12} strokeWidth={MARK_STROKE} /></span>
				{/if}
			</span>

			<span class="details sr-only">{rowSpeech}</span>
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
	   Opaque, so the lanes never bleed through. Both lines have a fixed height and
	   line two is always rendered, so no status change can move the rows below.
	   Active: 6 + 20 + 17 + 6 = 49, plus the list's 2px gap, a 51px pitch.
	   Settled: 4 + 19 + 16 + 3 = 42, a 44px pitch. */
	.scard {
		position: relative;
		z-index: 1;
		display: grid;
		grid-template-columns: var(--state-col) minmax(0, 1fr);
		column-gap: 6px;
		align-items: start;
		width: 100%;
		padding: 6px 8px;
		border: none;
		border-radius: var(--radius-control);
		background: var(--sidebar);
		color: inherit;
		text-align: left;
		cursor: pointer;
		transition: transform 300ms cubic-bezier(0.16, 1, 0.3, 1), background-color 120ms ease;
		--state-col: 16px;
		--line-1: 20px;
		--line-2: 17px;
	}
	.scard.compact {
		padding: 4px 8px 3px;
		--line-1: 19px;
		--line-2: 16px;
	}
	.scard-frame.dragging .scard { transition: background-color 120ms ease; }
	.scard-frame:hover .scard { background: var(--row-hover); }
	.scard.active,
	.scard-frame:hover .scard.active,
	.scard-frame.pressing .scard { background: var(--row-selected); }
	.scard-frame.pressing .scard { transform: scale(0.985); }
	.scard-frame.selected .scard,
	.scard-frame.selected:hover .scard { background: color-mix(in oklab, var(--primary) 9%, var(--sidebar)); }
	.scard:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	/* Selection checkmark, in the state column, so titles never move. */
	.check {
		display: grid;
		place-items: center;
		width: 16px;
		height: 16px;
		margin-top: calc((var(--line-1) - 16px) / 2);
		border: 1.5px solid var(--border-strong);
		border-radius: 50%;
		color: var(--primary-foreground);
	}
	.scard-frame.selected .check { background: var(--primary); border-color: var(--primary); }

	/* ── State column ─────────────────────────────────────────────────── */
	.lead {
		display: grid;
		place-items: center;
		width: var(--state-col);
		height: var(--line-1);
		color: var(--subtle-foreground);
	}
	.lead :global(svg) { flex: none; }
	.lead.green { color: var(--success); }
	.lead.purple { color: var(--status-merged); }
	.lead.amber { color: var(--warning); }
	.lead.muted { color: var(--subtle-foreground); }
	/* A settled row's glyph steps back, leaving the colour to the PR word. Runtime
	   dots do not: a settled row that is somehow running still has to be seen. */
	.scard.compact .lead :global(svg) { opacity: 0.7; }
	/* One 8px dot for all the runtime states, so they read as one family and
	   outweigh the thinner git glyphs. */
	.pip {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--primary);
		animation: card-ping 1.8s ease-in-out infinite;
	}
	/* Parked: its background work still runs, but no turn is live. Same colour,
	   hollow and still. */
	.pip.held {
		background: transparent;
		box-shadow: inset 0 0 0 1.5px var(--primary);
		animation: none;
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

	.body { display: block; min-width: 0; }

	.l1 {
		display: flex;
		align-items: baseline;
		gap: 8px;
		min-width: 0;
		height: var(--line-1);
		line-height: var(--line-1);
	}
	.title {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 13.5px;
		font-weight: 450;
		color: var(--sidebar-foreground);
	}
	.scard.emphasized .title { font-weight: 500; }
	/* Settled answers "how did this end", in secondary text. */
	.scard.compact .title {
		font-size: 13px;
		color: var(--sidebar-muted);
		transition: color 120ms ease;
	}
	.scard-frame:hover .scard.compact .title,
	.scard.compact.emphasized .title { color: var(--sidebar-foreground); }

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

	/* Line two: one calm line of facts, in the order status, PR, diff, branch. It
	   never wraps visibly and never shows half a number: the status and PR always
	   stay, and `.rest` wraps onto a second line its fixed height hides, so a fact
	   that no longer fits disappears whole. */
	.l2 {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		min-width: 0;
		height: var(--line-2);
		font-size: var(--fs-label);
		line-height: var(--line-2);
		white-space: nowrap;
		color: var(--subtle-foreground);
	}
	.rest {
		display: flex;
		flex: 1 1 0;
		flex-wrap: wrap;
		align-content: flex-start;
		align-items: flex-start;
		column-gap: 8px;
		min-width: 0;
		height: var(--line-2);
		overflow: hidden;
	}
	/* Holds the first line, so even the first fact wraps out of sight whole when
	   it does not fit, instead of being cut mid-number. The margin cancels its gap. */
	.rest::before {
		content: '';
		flex: none;
		width: 0;
		height: var(--line-2);
		margin-right: -8px;
	}
	.st {
		flex: none;
		white-space: nowrap;
	}
	/* The first thing to read on the line, and apart from a green PR beside it. */
	.st.accent,
	.st.amber,
	.st.danger { font-weight: 500; }
	/* Always stays, but on a very narrow panel it ends in an ellipsis, not past
	   the row. */
	.st.pr {
		flex: 0 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.st.accent { color: var(--primary); }
	.st.amber { color: var(--warning); }
	.st.danger { color: var(--destructive); }
	.st.muted { color: var(--muted-foreground); }
	.st.pr.green { color: var(--success); }
	.st.pr.purple { color: var(--status-merged); }
	.st.pr.muted { color: var(--subtle-foreground); }
	.st.progress.uncommitted { color: var(--warning); }
	.diff {
		flex: none;
		font-variant-numeric: tabular-nums;
	}
	.diff .p { color: var(--success); }
	.diff .m { color: var(--destructive); }
	/* The only fact that truncates, and not into a stump. */
	.where {
		flex: 1 1 0;
		min-width: 56px;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	/* Sits under the time; only there when the session is on another worker. */
	.worker {
		display: inline-flex;
		flex: none;
		align-self: center;
	}

	/* Several PRs: each count keeps its state's tone; the chip opens the list.
	   A button that reads as inline text: no box, no font or line-height of its
	   own, so the row keeps its height. */
	.pr-chip.multi {
		cursor: pointer;
		position: relative;
		flex: 0 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		margin: 0;
		padding: 0;
		border: 0;
		border-radius: 3px;
		background: none;
		font: inherit;
		line-height: inherit;
		color: inherit;
		text-align: inherit;
		text-transform: none;
		white-space: nowrap;
		appearance: none;
	}
	.pr-chip.multi .green { color: var(--success); }
	.pr-chip.multi .purple { color: var(--status-merged); }
	.pr-chip.multi .muted { color: var(--subtle-foreground); }
	.pr-chip.multi:focus-visible { outline: 2px solid var(--ring); outline-offset: -1px; }
	.pr-chip.multi .sep { color: var(--subtle-foreground); }
	.pr-chip.multi:hover > span:not(.sep),
	.pr-chip.multi.open > span:not(.sep) { text-decoration: underline dotted; text-underline-offset: 2px; }

	/* Phone: larger type and row for thumbs at arm's length, same anatomy. */
	.scard-frame.phone .scard {
		column-gap: 8px;
		padding: 8px 12px 8px 10px;
		--line-1: 22px;
		--line-2: 19px;
	}
	.scard-frame.phone .scard.compact {
		padding: 6px 12px 6px 10px;
		--line-1: 21px;
		--line-2: 18px;
	}
	.scard-frame.phone .title { font-size: 14.5px; }
	.scard-frame.phone .scard.compact .title { font-size: 14px; }
	.scard-frame.phone .l2 { font-size: 13px; }

	/* ── Hover actions (pointer that hovers) ───────────────────────────── */
	.hover-acts {
		position: absolute;
		top: 0;
		right: 0;
		bottom: 0;
		z-index: 2;
		display: flex;
		align-items: center;
		gap: 2px;
		padding: 0 6px 0 18px;
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

<script lang="ts">
	/**
	 * SessionCard — the ledger row for the Studio Noir sidebar.
	 *
	 * Every row leads with ONE glyph in a fixed left column: where the branch
	 * sits between edit and merge (see `deriveGitStage`). The session's own
	 * runtime state moved off that slot and onto the title — a pulsing dot while
	 * it works, an amber warning when it is parked on a prompt — so the two
	 * questions ("what is the agent doing" / "what happened to the code") stop
	 * competing for the same pixel.
	 *
	 * Active card (three lines):
	 *   L1  glyph · title · elapsed
	 *   L2  project · status word · PR state · diffstat
	 *   L3  device icon · branch
	 *
	 * Settled row (compact, two lines):
	 *   L1  glyph · title · age
	 *   L2  device icon · git-state word · branch
	 *
	 * Hover/swipe exposes a single action: Settle (active) or Move to Active
	 * (settled). On the swipe layout a long swipe across the row commits that
	 * action on release. Rename, Fork, and Delete live on the header
	 * session-title menu.
	 */
	import { onMount, untrack } from 'svelte';
	import {
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
		formatElapsed,
		formatAge,
		deviceLabel,
		branchLabel,
		prStateText,
		sessionModeOf,
		sessionTimestamp,
		type SessionCardStatus,
		type SessionGitFacts
	} from './session-card-data';

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
		/** Single-line dense row — used for the settled/archived groups. */
		compact?: boolean;
		/** Id of the card whose swipe actions are currently revealed, owned by the
		 *  parent list so ONE delegated listener can close them (R15). Leave it
		 *  undefined to let the card manage its own reveal state (standalone use). */
		openActionsId?: string | null;
		/** Notifies the parent that this card opened (its id) or closed (null). */
		onActionsOpenChange?: (sessionId: string | null) => void;
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
		openActionsId,
		onActionsOpenChange,
		onOpen,
		onSettled
	}: Props = $props();

	const live = $derived(isLiveStatus(status));
	const lastActivity = $derived(timestamp ?? sessionTimestamp(session));
	const elapsed = $derived(Math.max(0, now - lastActivity));
	const timer = $derived(live ? formatElapsed(elapsed) : formatAge(elapsed));
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
	const prText = $derived(prStateText(prState, session.pr_number));
	// Follow-up edits do not erase the PR history. Show both facts.
	const showPr = $derived(Boolean(prText));
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

	let cardRef = $state<HTMLElement | null>(null);
	// Reveal state is parent-owned when `openActionsId` is supplied (the sidebar
	// path); the local fallback keeps the card self-contained on its own.
	let localActionsOpen = $state(false);
	const controlledActions = $derived(openActionsId !== undefined);
	const actionsOpen = $derived(controlledActions ? openActionsId === session.id : localActionsOpen);

	function setActionsOpen(open: boolean) {
		if (controlledActions) onActionsOpenChange?.(open ? session.id : null);
		else localActionsOpen = open;
	}

	let swipeLayout = $state(false);
	let dragOffset = $state<number | null>(null);
	// True once the drag has travelled far enough that letting go commits the
	// action itself: one long swipe settles a row, no second tap needed.
	let swipeCommit = $state(false);

	// Width of the swipe rail; the styles read it back through `--reveal`.
	const ACTION_REVEAL_WIDTH = 60;
	// A short swipe latches the rail open once it has come this far.
	const SWIPE_COMMIT_DISTANCE = 24;
	// Past the rail the card keeps following the finger, with a little drag.
	const OVERSCROLL_RATIO = 0.85;
	// A flick faster than this (px per ms) latches the rail open or shut
	// whatever distance it covered, so a quick swipe is never "too short".
	const FLICK_VELOCITY = 0.45;
	// Ignore a flick reading older than this at release: the finger stopped.
	const FLICK_MAX_AGE = 80;
	// The click a touch commit leaves behind is swallowed for this long.
	const COMMIT_CLICK_GUARD = 500;
	let pointerId: number | null = null;
	let pointerStartX = 0;
	let pointerStartY = 0;
	let pointerStartOffset = 0;
	let fullSwipeDistance = Infinity;
	let horizontalDrag = false;
	let lastMoveX = 0;
	let lastMoveTime = 0;
	let velocity = 0;
	let suppressNextClick = false;
	let actionJustCommitted = false;
	let lastCommitAt = -Infinity;

	function closeActions() {
		setActionsOpen(false);
		dragOffset = null;
		swipeCommit = false;
	}

	async function commitToggle() {
		setActionsOpen(false);
		const settling = session.is_active;
		try {
			await appData.setSessionActive(session.id, !settling);
			// Desktop moves to a fresh composer. On mobile, keep the current
			// session and drawer open so settling does not navigate the user away.
			if (settling && active && !platform.isMobile) onSettled?.();
		} catch {
			notify('Failed to update session', { severity: 'error' });
		}
	}

	// Touch/pen: the synthetic click after a swipe is unreliable — pointer capture on
	// the frame can reroute or drop it, which is why the first tap on a freshly
	// revealed action was being lost. Commit straight from pointerup and mark the
	// trailing click handled so it can't double-fire.
	//
	// Committing closes the rail at once, which makes the button inert before
	// the browser dispatches that trailing click. The click then hit-tests the
	// card sliding back underneath and used to OPEN the session. Arm the card's
	// click guard as well so the click dies wherever it lands.
	function handleActionPointerUp(event: PointerEvent) {
		if (event.pointerType === 'mouse') return;
		event.stopPropagation();
		actionJustCommitted = true;
		suppressNextClick = true;
		lastCommitAt = event.timeStamp;
		void commitToggle();
	}

	// Mouse and keyboard path; skips the work when pointerup already handled a tap.
	function handleActionClick(event: MouseEvent) {
		event.stopPropagation();
		if (actionJustCommitted) {
			actionJustCommitted = false;
			return;
		}
		void commitToggle();
	}

	function openCard(event?: Event) {
		if (suppressNextClick) {
			suppressNextClick = false;
			return;
		}
		// Belt and braces for the post-commit click: some engines deliver it
		// after a fresh pointerdown has already cleared the one-shot guard.
		if (event && event.timeStamp - lastCommitAt < COMMIT_CLICK_GUARD) return;
		if (swipeLayout && actionsOpen) {
			closeActions();
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

	function isActionTarget(target: EventTarget | null): boolean {
		return target instanceof Element && !!target.closest('.card-actions, button, input, a');
	}

	function handlePointerDown(event: PointerEvent) {
		// Any fresh press clears the one-shot click guards left by the last gesture.
		suppressNextClick = false;
		actionJustCommitted = false;
		if (!swipeLayout || event.isPrimary === false || event.button !== 0 || isActionTarget(event.target)) return;
		pointerId = event.pointerId;
		pointerStartX = event.clientX;
		pointerStartY = event.clientY;
		pointerStartOffset = actionsOpen ? -ACTION_REVEAL_WIDTH : 0;
		// A swipe across half the row commits outright. The floor keeps a
		// narrow (or unmeasured) row from committing on what is really a reveal.
		fullSwipeDistance = Math.max(ACTION_REVEAL_WIDTH * 2.5, (cardRef?.clientWidth ?? 0) * 0.5);
		horizontalDrag = false;
		lastMoveX = event.clientX;
		lastMoveTime = performance.now();
		velocity = 0;
		cardRef?.setPointerCapture?.(event.pointerId);
	}

	function handlePointerMove(event: PointerEvent) {
		if (pointerId !== event.pointerId) return;
		const dx = event.clientX - pointerStartX;
		const dy = event.clientY - pointerStartY;

		if (!horizontalDrag) {
			if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return;
			if (Math.abs(dy) >= Math.abs(dx)) {
				pointerId = null;
				dragOffset = null;
				return;
			}
			horizontalDrag = true;
		}

		event.preventDefault();
		// Timed off the wall clock rather than event.timeStamp: the two agree in
		// browsers and the clock is the one a test can steer.
		const moveTime = performance.now();
		const elapsed = moveTime - lastMoveTime;
		if (elapsed > 0) velocity = (event.clientX - lastMoveX) / elapsed;
		lastMoveX = event.clientX;
		lastMoveTime = moveTime;

		const raw = Math.min(0, pointerStartOffset + dx);
		// Inside the rail the card tracks the finger 1:1; past it the card
		// keeps going with a touch of drag so the long swipe reads as a
		// deliberate stretch rather than a wall.
		dragOffset =
			raw >= -ACTION_REVEAL_WIDTH
				? raw
				: -ACTION_REVEAL_WIDTH - (-raw - ACTION_REVEAL_WIDTH) * OVERSCROLL_RATIO;
		swipeCommit = -dragOffset >= fullSwipeDistance;
	}

	function finishPointer(event: PointerEvent) {
		if (pointerId !== event.pointerId) return;
		const dx = event.clientX - pointerStartX;
		if (horizontalDrag) {
			const nextOffset = dragOffset ?? pointerStartOffset;
			const flick = performance.now() - lastMoveTime <= FLICK_MAX_AGE && Math.abs(velocity) >= FLICK_VELOCITY;
			if (swipeCommit) {
				// The long swipe IS the tap on the action.
				lastCommitAt = event.timeStamp;
				void commitToggle();
			} else if (flick) {
				setActionsOpen(velocity < 0);
			} else {
				setActionsOpen(
					actionsOpen
						? nextOffset < -(ACTION_REVEAL_WIDTH - SWIPE_COMMIT_DISTANCE)
						: nextOffset <= -SWIPE_COMMIT_DISTANCE
				);
			}
			// Swallow the trailing click from this gesture; cleared on the next press.
			suppressNextClick = true;
		} else if (Math.abs(dx) > 6) {
			suppressNextClick = true;
		}
		dragOffset = null;
		swipeCommit = false;
		pointerId = null;
		horizontalDrag = false;
	}

	function cancelPointer(event: PointerEvent) {
		if (pointerId !== event.pointerId) return;
		dragOffset = null;
		swipeCommit = false;
		pointerId = null;
		horizontalDrag = false;
	}

	// Swipe-to-reveal has two independent triggers, mirrored by the styles at the
	// bottom of this file: the mobile shell (fixed for the life of the page), and
	// a touch-only pointer anywhere else — a touch laptop still swipes.
	onMount(() => {
		if (platform.isMobile) {
			swipeLayout = true;
			return;
		}
		if (typeof window.matchMedia !== 'function') {
			swipeLayout = false;
			closeActions();
			return;
		}

		const query = window.matchMedia('(hover: none)');
		const syncLayout = () => {
			swipeLayout = query.matches;
			if (!swipeLayout) closeActions();
		};
		syncLayout();
		query.addEventListener('change', syncLayout);
		return () => query.removeEventListener('change', syncLayout);
	});
</script>

<!-- No <svelte:window> here on purpose: with 60+ cards rendered, per-card
     global click/keydown handlers ran 60+ times per document click (R15). The
     sidebar closes the open card's actions from one delegated listener, keyed
     on `data-session-card`. -->
<div
	bind:this={cardRef}
	data-session-card={session.id}
	class="scard-frame"
	class:compact
	class:actions-open={actionsOpen}
	class:dragging={dragOffset !== null}
	class:swipe-commit={swipeCommit}
	style:--reveal={`${ACTION_REVEAL_WIDTH}px`}
	style:--rail-shift={dragOffset === null ? undefined : `${Math.min(0, dragOffset + ACTION_REVEAL_WIDTH)}px`}
	role="group"
	aria-label={`${title} session`}
	onpointerdown={handlePointerDown}
	onpointermove={handlePointerMove}
	onpointerup={finishPointer}
	onpointercancel={cancelPointer}
>
	<div
		class="scard"
		class:active
		class:receded
		class:compact
		role="button"
		tabindex="0"
		aria-current={active ? 'true' : undefined}
		onclick={openCard}
		onkeydown={handleCardKeydown}
		title={title}
		style:transform={dragOffset === null ? undefined : `translateX(${dragOffset}px)`}
	>
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

		<span class="body">
			<span class="l1">
				<span class="title">
					{#if status === 'working'}
						<span class="pip" aria-hidden="true"></span>
					{:else if attention}
						<TriangleAlert size={12} strokeWidth={2.2} class="warn" aria-hidden="true" />
					{/if}{#if compact && (status === 'working' || attention)}<span class="sr-only">{`${statusWord}: `}</span>{/if}{title}
				</span>
				<span class="timer" class:live>{timer}</span>
			</span>

			{#if !compact}
				<span class="l2">
					<span class="seg"><span class="proj">{projectLabel}</span></span>
					<span class="dot" aria-hidden="true"></span>
					<span class="st {statusTone}">{statusWord}{#if status === 'working'}<span class="working-dots" aria-hidden="true">
							<span></span><span></span><span></span>
						</span>{/if}</span>
					{#if showPr}
						<span class="dot" aria-hidden="true"></span>
						<span class="st" class:green={prState === 'open'} class:purple={prState === 'merged'}>{prText}</span>
					{/if}
					{#if diff}
						<span class="dot" aria-hidden="true"></span>
						<span class="diff"><span class="p">{diff.plus}</span> <span class="m">{diff.minus}</span></span>
					{/if}
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
				{#if compact && stageWord}
					<span class="dot" aria-hidden="true"></span>
					<span class="st {stageTone}">{stageWord}</span>
				{/if}
				{#if branch}
					<span class="dot" aria-hidden="true"></span>
					<span class="seg branch-seg"><span class="branch">{branch}</span></span>
				{/if}
			</span>
		</span>
	</div>

	<span
		class="card-actions"
		aria-label="Session actions"
		aria-hidden={swipeLayout && !actionsOpen ? 'true' : undefined}
		inert={swipeLayout && !actionsOpen}
	>
		{#if session.is_active}
			<button
				class="row-action state-toggle"
				title="Settle session"
				aria-label="Settle session"
				onpointerup={handleActionPointerUp}
				onclick={handleActionClick}
			>
				<Check size={16} strokeWidth={1.8} />
				{#if swipeLayout && !compact}<span class="row-action-label" aria-hidden="true">Settle</span>{/if}
			</button>
		{:else}
			<button
				class="row-action state-toggle"
				title="Move to Active"
				aria-label="Move session to Active"
				onpointerup={handleActionPointerUp}
				onclick={handleActionClick}
			>
				<Undo2 size={16} strokeWidth={1.8} />
				{#if swipeLayout && !compact}<span class="row-action-label" aria-hidden="true">Restore</span>{/if}
			</button>
		{/if}
	</span>
</div>

<style>
	.scard-frame {
		--ram-reveal: 0;
		position: relative;
		width: 100%;
		border-radius: 9px;
	}
	/* :has(:focus-visible), not :focus-within: a mouse click focuses the card
	   (tabindex=0) and that focus persists, which used to stick the highlight +
	   revealed action until the user clicked elsewhere. Keyboard focus still
	   reveals everything. */
	.scard-frame:hover,
	.scard-frame:has(:focus-visible),
	.scard-frame.actions-open {
		--ram-reveal: 1;
	}
	/* Fixed 16px lead column so every glyph lines up down the list, however
	   narrow the sidebar gets (it resizes to 220px). */
	.scard {
		display: grid;
		grid-template-columns: 16px minmax(0, 1fr);
		column-gap: 6px;
		align-items: start;
		width: 100%;
		text-align: left;
		/* When the hover action reveals, grow the right padding so lines
		   truncate short of the button instead of running underneath it. */
		padding: 7px calc(8px + var(--ram-reveal) * 30px) 8px 8px;
		border: none;
		border-radius: 9px;
		background: transparent;
		color: inherit;
		cursor: pointer;
		transition: background-color 0.13s ease, padding 0.13s ease;
		position: relative;
		z-index: 2;
	}
	.scard-frame:hover .scard,
	.scard-frame:has(:focus-visible) .scard,
	.scard-frame.actions-open .scard {
		background: var(--sidebar-accent);
	}
	.scard.active { background: var(--sidebar-accent-active); }
	.scard.active::before {
		content: '';
		position: absolute;
		left: 0;
		top: 8px;
		bottom: 8px;
		width: 2px;
		border-radius: 2px;
		background: var(--primary);
		box-shadow: 0 0 8px -1px var(--glow);
	}
	.scard:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	/* Compact two-line row for settled/archived sessions: title + age, then a
	   muted meta line (device icon · git state · branch). Still denser than the
	   three-line active cards. Hover reveals the quick action centered at the
	   row's right edge; the mobile swipe strip is untouched. */
	.scard.compact { padding: 6px calc(8px + var(--ram-reveal) * 30px) 7px 8px; }
	.scard.compact .l3 { margin-top: 3px; font-size: 10.5px; }
	.scard.compact.active::before { top: 6px; bottom: 6px; }

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

	.timer {
		flex: none;
		font-family: var(--font-mono);
		font-variant-numeric: tabular-nums;
		font-size: 11px;
		color: var(--sidebar-muted);
		white-space: nowrap;
		transition: opacity 120ms ease;
	}
	.timer.live { color: var(--primary); }

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

	/* Rail fills the card's right side — full row height and the entire
	   width the revealed padding reserves — so the button's hit area does
	   too. */
	.card-actions {
		position: absolute;
		top: 0;
		right: 0;
		bottom: 0;
		width: 38px;
		display: flex;
		align-items: stretch;
		gap: 1px;
		opacity: var(--ram-reveal);
		transition: opacity 120ms ease;
		z-index: 5;
	}
	.card-actions > * {
		transition: color 120ms ease, background-color 120ms ease;
	}
	.row-action {
		flex: 1;
		min-width: 0;
		padding: 0;
		border: none;
		border-radius: 6px;
		background: transparent;
		color: var(--sidebar-muted);
		display: grid;
		place-items: center;
		cursor: pointer;
		transition: color 0.12s ease, background-color 0.12s ease, opacity 0.12s ease;
	}
	.row-action:hover {
		color: var(--foreground);
		background: color-mix(in oklch, var(--sidebar-accent-active) 78%, transparent);
	}
	.row-action.state-toggle { color: var(--muted-foreground); }
	.row-action:disabled { opacity: 0.45; cursor: not-allowed; }
	.row-action:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	/* Swipe layout. Two independent triggers, matching `swipeLayout` in the
	   script: a touch-only pointer (still true on a touch laptop), and the
	   mobile shell regardless of pointer. */
	@media (hover: none) {
		.scard-frame {
			overflow: hidden;
			touch-action: pan-y;
			background: color-mix(in oklch, var(--sidebar-accent-active) 84%, var(--sidebar));
		}
		.scard {
			background: var(--sidebar);
			padding: 7px 8px 8px;
			transition: transform 220ms cubic-bezier(0.22, 1, 0.36, 1), background-color 130ms ease;
			will-change: transform;
		}
		/* The swipe track sits behind the card, so the card surface has to be
		   opaque or the revealed actions bleed through the accent tint. */
		.scard.active {
			background:
				linear-gradient(var(--sidebar-accent-active), var(--sidebar-accent-active)),
				var(--sidebar);
		}
		.scard-frame.actions-open .scard { transform: translateX(calc(-1 * var(--reveal))); }
		.scard-frame.dragging .scard { transition: none; }
		.scard-frame:hover .scard { background: var(--sidebar); }
		.scard-frame:hover .scard.active {
			background:
				linear-gradient(var(--sidebar-accent-active), var(--sidebar-accent-active)),
				var(--sidebar);
		}
		/* A long swipe arms the commit: the whole track goes solid so the
		   release reads as "this will settle now", not "this reveals a button". */
		.scard-frame.swipe-commit { background: var(--primary); }

		/* The rail hugs the card's trailing edge. Inside the reveal width it is
		   pinned to the frame; once the finger drags past, `--rail-shift`
		   carries it along with the card so the button never detaches. */
		.card-actions {
			top: 0;
			right: 0;
			bottom: 0;
			width: var(--reveal);
			justify-content: flex-end;
			gap: 0;
			padding-left: 0;
			background: none;
			opacity: 1;
			z-index: 1;
			pointer-events: none;
			transform: translateX(var(--rail-shift, 0px));
			transition: transform 220ms cubic-bezier(0.22, 1, 0.36, 1);
		}
		.scard-frame.dragging .card-actions { transition: none; }
		.scard-frame.actions-open .card-actions { pointer-events: auto; }
		.card-actions > * {
			width: var(--reveal);
			height: 100%;
			min-height: 44px;
			border-radius: 0;
			display: grid;
			grid-auto-flow: row;
			place-items: center;
			align-content: center;
			gap: 3px;
			color: var(--foreground);
			background: color-mix(in oklch, var(--foreground) 9%, var(--sidebar));
			transition: color 120ms ease, background-color 120ms ease, transform 120ms ease;
		}
		.card-actions > *:hover {
			background: color-mix(in oklch, var(--foreground) 14%, var(--sidebar));
		}
		.card-actions > *:active {
			background: color-mix(in oklch, var(--foreground) 18%, var(--sidebar));
		}
		.row-action-label {
			font-size: 10px;
			font-weight: 600;
			line-height: 1;
			letter-spacing: 0.02em;
		}
		.scard-frame.swipe-commit .card-actions > * {
			color: var(--primary-foreground);
			background: var(--primary);
		}
		.card-actions > * :global(svg) { transition: transform 120ms ease; }
		.scard-frame.swipe-commit .card-actions > * :global(svg) { transform: scale(1.18); }
	}

	@media (prefers-reduced-motion: reduce) {
		.pip,
		.working-dots > span,
		.title :global(.warn) { animation: none; }
		.scard,
		.card-actions,
		.card-actions > * { transition: none; }
	}
</style>

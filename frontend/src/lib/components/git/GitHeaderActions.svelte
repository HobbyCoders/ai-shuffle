<script lang="ts">
	/**
	 * GitHeaderActions - the chat header's git cluster, folded into one button.
	 *
	 * The header shows a single button whose label is whatever needs the user
	 * first (approvals waiting, merge conflicts, commits behind, uncommitted
	 * changes, commits to push, an open PR, or "up to date" — see
	 * computeHeaderSummary). It opens a panel that tells the whole story in
	 * order: the branch against its base, the Ship It pipeline with the next
	 * step lit, each git action as a row with an in-panel editor, the changed
	 * files, and the chat's private access.
	 *
	 * Each mutating action (Commit / Push / Pull / Create PR / Ship It) keeps
	 * its split shape inside the panel: the row runs the action immediately
	 * with saved defaults, and its chevron opens a compact
	 * GitActionOptionsDropdown for per-invocation overrides. Editors and operation
	 * progress share the same surface, with Back returning to the overview.
	 *
	 * On narrow screens (the mobile shell, or a narrow web window) the panel
	 * becomes a bottom sheet behind a scrim. Init Repo (non-repo projects) and
	 * the "Git unavailable" retry stay as direct header buttons: there is no
	 * panel to open for them.
	 */
	import { onDestroy, onMount, untrack } from 'svelte';
	import GitProgressPopover from './GitProgressPopover.svelte';
	import { gitOperationState } from '$lib/stores/gitOperations.svelte';
	import SwarmDropdown from '../chat/SwarmDropdown.svelte';
	import { buildShell } from '$lib/utils/platform';
	import { GitBranch, GitCommitHorizontal, GitPullRequest, ChevronDown, ChevronRight, ArrowDownToLine, ArrowUpFromLine, FolderGit2, FolderClosed, Rocket, TriangleAlert, ShieldCheck, ExternalLink, Download, X } from 'lucide-svelte';
	import { git, gitSnapshot, gitMode } from '$lib/stores/git.svelte';
	import { githubRepoInfo } from '$lib/stores/github.svelte';
	import { openHeaderDropdown, openDropdown } from '$lib/stores/header-dropdowns';
	import { chatAccessSummary } from '$lib/stores/chat-access';
	import { computeShowShipIt } from '$lib/stores/derived/ship-it-visibility';
	import { isCurrentHeadMerged } from '$lib/stores/derived/merged-pr';
	import { computePrimaryAction, mergeBlockedByMergeability, type PrimaryGitAction } from '$lib/stores/derived/git-primary-action';
	import { computeHeaderSummary, computePipeline } from '$lib/stores/derived/git-header-summary';
	import GitActionOptionsDropdown from './GitActionOptionsDropdown.svelte';
	import { computeShipItSteps } from '$lib/stores/derived/git-action-options';
	import type {
		CommitOptions,
		PushOptions,
		PullOptions,
		CreatePROptions,
		ShipItOptions
	} from '$lib/stores/derived/git-action-options';

	interface Props {
		sessionId?: string | null;
		isStreaming?: boolean;
		/** True while a git operation (commit/push/PR/ship) is in flight.
		 *  Disables the mutating action rows so the user can't fire a second
		 *  action while work is running. */
		operationInProgress?: boolean;
		isGitRepo?: boolean;
		error?: string | null;
		/** Project name shown with the branch in the panel head. */
		projectName?: string | null;
		/** When set, forces worktree read-only mode and uses this as the branch display.
		 *  This ensures the correct branch shows immediately without waiting for the store to load. */
		worktreeBranch?: string | null;
		/** Existing PR to merge through Ship It */
		openPR?: { number: number; title: string; url: string; mergeable?: boolean | null } | null;
		/** Mutating actions. A bare row click passes no options (falling back to
		 *  the gitActionPreferences defaults); the option popover passes an
		 *  explicit bag. Pass the MainPanel handlers directly so both paths flow
		 *  through one function. */
		onClickCommit: (opts?: CommitOptions) => void;
		onClickCreatePR: (opts?: CreatePROptions) => void;
		onClickPush: (opts?: PushOptions) => void;
		onClickPull: (opts?: PullOptions) => void;
		onClickInitRepo?: () => void;
		/** Are the GitHub-backed actions usable? The header itself no longer
		 *  requires GitHub auth (local git must work without it), so this gates
		 *  ONLY the gh-dependent actions — Create PR / Ship It. Pass the
		 *  git store's hysteresis-guarded `githubFeaturesAvailable`; a single
		 *  failed `gh auth status` must not flip it. Defaults to true so callers
		 *  that don't pass it keep today's behaviour. */
		githubAvailable?: boolean;
		/** Ship It: one-shot commit → push → PR → merge → cleanup pipeline */
		onClickShipIt?: (opts?: ShipItOptions) => void;
		/** Open diff review panel (side-by-side) */
		onOpenDiffReview?: () => void;
		/** Download the chat transcript. Rendered as a panel row on narrow
		 *  screens only, where the header has no room for its own icon. */
		onDownloadChat?: () => void;
		downloadingChat?: boolean;
	}

	let { sessionId = null, isStreaming = false, operationInProgress = false, error = null, isGitRepo = true, projectName = null, worktreeBranch = null, openPR = null, githubAvailable = true, onClickCommit, onClickCreatePR, onClickPush, onClickPull, onClickInitRepo, onClickShipIt, onOpenDiffReview, onDownloadChat, downloadingChat = false }: Props = $props();

	// Mutating git actions are blocked while streaming OR while another git
	// operation is in flight — the user must never fire a second action while
	// work is running.
	const actionsBusy = $derived(isStreaming || operationInProgress);

	// ── ONE coherent snapshot ────────────────────────────────────────────────
	// Every value the action decision depends on is read from a SINGLE store
	// object rather than from four independently-timed derived stores. Reading
	// them separately is what made the header walk through intermediate states
	// after each session switch and each mutation (finding G4).
	const snapshot = $derived($gitSnapshot);
	// True until a complete load has landed for the CURRENT checkout. While it
	// is false the values on screen belong to the previous context (or are a
	// cache hit mid-revalidation) and must not be used to derive a NEW action.
	const snapshotPending = $derived(!snapshot.hydrated);
	// The mutating actions are ALSO disabled while the snapshot is still
	// resolving for this context: what's rendered is the previous coherent
	// action, greyed out, so the user can't fire it against state we haven't
	// confirmed yet.
	const primaryDisabled = $derived(actionsBusy || snapshotPending);

	// Slow, visibility-gated PR/CI refresh: PR merges, reviews and CI results
	// happen on GitHub and are otherwise invisible until a session switch. It
	// runs only while this header is mounted (i.e. actually visible) and only
	// while the browser tab is visible — see git.startBackgroundRefresh.
	onMount(() => {
		git.startBackgroundRefresh();
		return () => git.stopBackgroundRefresh();
	});

	// Worktree mode: determined by prop (immediate) OR store (after load)
	const isWorktreeMode = $derived(!!worktreeBranch || $gitMode === 'worktree');

	// ── Panel + popover state ────────────────────────────────────────────────
	// Driven by the shared store so opening the panel closes the profile /
	// notification dropdowns and vice versa. A git-action popover id implies
	// the panel is open (the popover lives inside it).
	type GitActionDropdownId = 'git-commit' | 'git-push' | 'git-pull' | 'git-pr' | 'git-shipit';
	const GIT_ACTION_IDS = new Set<string>(['git-commit', 'git-push', 'git-pull', 'git-pr', 'git-shipit']);
	const isPanelId = (id: string | null) => id === 'git-panel' || GIT_ACTION_IDS.has(id ?? '');
	const panelOpen = $derived(isPanelId($openHeaderDropdown));
	const editorId = $props.id();
	const activeEditor = $derived($openHeaderDropdown === 'git-commit' ? 'commit' : $openHeaderDropdown === 'git-push' ? 'push' : $openHeaderDropdown === 'git-pull' ? 'pull' : $openHeaderDropdown === 'git-pr' ? 'pr' : $openHeaderDropdown === 'git-shipit' ? 'shipit' : null);
	const progressActive = $derived($gitOperationState.active);
	$effect(() => { if (progressActive) untrack(() => openDropdown('git-panel')); });
	const commitOptsOpen = $derived($openHeaderDropdown === 'git-commit');
	const pushOptsOpen = $derived($openHeaderDropdown === 'git-push');
	const pullOptsOpen = $derived($openHeaderDropdown === 'git-pull');
	const prOptsOpen = $derived($openHeaderDropdown === 'git-pr');
	const shipItOptsOpen = $derived($openHeaderDropdown === 'git-shipit');
	let triggerRef = $state<HTMLButtonElement | null>(null);
	let panelRef = $state<HTMLDivElement | null>(null);
	// The native mobile build stays a sheet on foldables at ANY width. Web
	// windows use a sheet on phones and coarse-pointer tablets.
	let sheetLayout = $state(buildShell() === 'mobile');
	onMount(() => {
		if (typeof window.matchMedia !== 'function') return;
		const media = window.matchMedia('(max-width: 700px), (pointer: coarse)');
		const update = () => { sheetLayout = buildShell() === 'mobile' || media.matches; };
		update();
		media.addEventListener('change', update);
		return () => media.removeEventListener('change', update);
	});
	let sheetOffset = $state(0);
	let draggingSheet = $state(false);
	let dragPointer: number | null = null;
	let dragStartY = 0;
	let suppressHandleClick = false;
	$effect(() => {
		if (!panelOpen || !sheetLayout) cancelSheetDrag();
	});

	function startSheetDrag(e: PointerEvent) {
		if (!e.isPrimary || e.button !== 0) return;
		dragPointer = e.pointerId;
		dragStartY = e.clientY;
		suppressHandleClick = false;
		draggingSheet = true;
		(e.currentTarget as HTMLButtonElement).setPointerCapture(e.pointerId);
	}

	function moveSheetDrag(e: PointerEvent) {
		if (e.pointerId !== dragPointer) return;
		const distance = e.clientY - dragStartY;
		if (Math.abs(distance) > 8) suppressHandleClick = true;
		sheetOffset = Math.max(0, distance);
	}

	function endSheetDrag(e: PointerEvent) {
		if (e.pointerId !== dragPointer) return;
		const dismiss = sheetOffset >= Math.min(120, (panelRef?.offsetHeight ?? 600) * 0.2);
		cancelSheetDrag();
		if (dismiss) {
			closePanel();
			triggerRef?.focus({ preventScroll: true });
		}
	}

	function cancelSheetDrag() {
		dragPointer = null;
		draggingSheet = false;
		sheetOffset = 0;
	}

	function activateSheetHandle(e: MouseEvent) {
		// Suppress the click synthesized after dragging, not keyboard activation.
		if (suppressHandleClick && e.detail > 0) return;
		closePanel();
		triggerRef?.focus({ preventScroll: true });
	}

	// Use worktreeBranch prop as immediate fallback while store is loading
	const currentBranch = $derived(snapshot.currentBranch ?? worktreeBranch ?? null);
	const hasChanges = $derived(
		(snapshot.diffStats?.additions ?? 0) > 0 ||
		(snapshot.diffStats?.deletions ?? 0) > 0 ||
		(snapshot.diffStats?.files_changed ?? 0) > 0
	);
	const additions = $derived(snapshot.diffStats?.additions ?? 0);
	const deletions = $derived(snapshot.diffStats?.deletions ?? 0);
	const filesChanged = $derived(snapshot.changedFiles?.length ?? snapshot.diffStats?.files_changed ?? 0);
	const changedFiles = $derived(snapshot.changedFiles ?? []);
	const commitsAhead = $derived(snapshot.status?.ahead ?? 0);
	// Whether the branch carries real content over its base branch (non-empty
	// base...HEAD diff). Distinct from commitsAhead: a branch can be ahead by
	// commit count yet have a 0/0 net diff (empty/cancelling commits, or an
	// already-merged/cleaned-up worktree) — those have nothing left to ship.
	const branchDiffersFromBase = $derived(snapshot.status?.differs_from_base ?? false);
	// `/git/status` reports the repository's REAL default branch. The old
	// fallback chain started at the github store, which nothing ever populated,
	// so every `master`-default repo silently used 'main' — and Ship It / Create
	// PR appeared while sitting ON the default branch (finding G3).
	const defaultBranch = $derived(
		snapshot.defaultBranch ?? $githubRepoInfo?.default_branch ?? 'main'
	);
	const isOnDefaultBranch = $derived(currentBranch === defaultBranch);

	// The open PR for this branch. The host passes it from the same store field;
	// the snapshot is the fallback so the value can never disagree with the rest
	// of the decision inputs.
	const openPr = $derived(openPR ?? snapshot.openPr);
	const isHeadMerged = $derived(!openPr && isCurrentHeadMerged(snapshot.status, snapshot.prState));

	// Upstream tracking branch state (for push/pull rows)
	const currentBranchInfo = $derived(snapshot.branches.find(b => b.current));
	const commitsBehindUpstream = $derived(currentBranchInfo?.behind ?? 0);
	const commitsAheadUpstream = $derived(currentBranchInfo?.ahead ?? 0);
	const hasUpstream = $derived(!!currentBranchInfo?.upstream);
	// Diverged = both ahead AND behind (e.g. after rebase/amend or normal development divergence).
	// Always offer Pull as the way forward — pull integrates remote changes safely.
	// Force push is available as a secondary option in the Push popover for rebase recovery.
	const isDiverged = $derived(commitsAheadUpstream > 0 && commitsBehindUpstream > 0 && hasUpstream);
	const needsPull = $derived(commitsBehindUpstream > 0 && hasUpstream);
	const canPush = $derived(!hasChanges && commitsAheadUpstream > 0 && hasUpstream && !needsPull);

	// Primary action decision (Commit / Create PR / clean placeholder)
	// extracted into a pure, unit-tested helper — the template renders its result.
	const computedPrimaryAction = $derived(
		computePrimaryAction({
			hasChanges,
			isHeadMerged,
			hasOpenPr: !!openPr,
			commitsAhead,
			differsFromBase: branchDiffersFromBase,
			isOnDefaultBranch,
			needsPull
		})
	);

	// Hold the last action computed from a COHERENT snapshot. While the current
	// context has not fully loaded, the header keeps showing that action (greyed
	// and disabled) instead of re-deriving from half-filled state — this is what
	// stops "Merge PR #12 → Create PR → Merge PR #12" flicker on a switch.
	type HeaderOpenPr = { number: number; title: string; url: string; mergeable?: boolean | null };
	let heldPrimaryAction = $state<PrimaryGitAction>('clean');
	let heldShowShipIt = $state(false);
	let heldOpenPr = $state<HeaderOpenPr | null>(null);

	// GitHub-only actions degrade to their local predecessor when gh is
	// unusable, rather than rendering a button that is guaranteed to fail.
	// 'create-pr' needs gh; 'commit' and 'push' do not. Never invent an action — fall back to 'clean', which is the
	// no-op placeholder.
	const GITHUB_ONLY_ACTIONS = new Set<PrimaryGitAction>(['create-pr']);
	const resolvedPrimaryAction = $derived(snapshotPending ? heldPrimaryAction : computedPrimaryAction);
	const primaryAction = $derived(
		!githubAvailable && GITHUB_ONLY_ACTIONS.has(resolvedPrimaryAction)
			? 'clean'
			: resolvedPrimaryAction
	);
	/** The PR the rendered action refers to — held alongside the action so a
	 *  greyed "Ship It" still names the PR it would merge. */
	const shownPr = $derived(snapshotPending ? heldOpenPr : openPr);
	const mergeBlocked = $derived(mergeBlockedByMergeability(shownPr?.mergeable ?? null));
	const shipItSummary = $derived(computeShipItSteps({
		hasChanges, unpushed: snapshot.status?.unpushed, hasOpenPR: !!shownPr,
		prNumber: shownPr?.number, cleanup: false
	}).map(step => step.label).join(', '));
	// The backend serves the LAST KNOWN-GOOD PR when it cannot reach GitHub
	// (`pr_lookup_failed` + `stale`), and a `gh` transport failure surfaces as a
	// 502 rather than an empty result. Either way the value shown is real but not
	// freshly confirmed — say so, and never downgrade it to "Create PR".
	const prIsLastKnown = $derived(snapshot.prStateStale && !!shownPr);
	const lastKnownSuffix = $derived(prIsLastKnown ? ' (last known state — GitHub unreachable)' : '');

	// Whether this folder is a *confirmed* git repo. Strict `=== true` (not the
	// optimistic `!== false` used for the header gate): for a reused/cleaned-up
	// worktree the folder may be empty, so Ship It must only show once status has
	// loaded and positively confirms a repo — never on null/unconfirmed or stale
	// state. (diffStats, which drives hasChanges, can outlive the repo in cache.)
	// Also requires a hydrated snapshot: a carried-over placeholder from another
	// checkout must never be read as confirmation of THIS one.
	const isRepoConfirmed = $derived(!snapshotPending && snapshot.status?.is_git_repo === true);

	// Ship It shows only when there's actually something to ship — uncommitted
	// changes, unmerged content, or an open PR — on a non-default branch
	// that isn't behind upstream, and only for a confirmed repo. See
	// computeShowShipIt for the full rationale.
	const computedShowShipIt = $derived(computeShowShipIt({
		isRepoConfirmed,
		hasChanges,
		isHeadMerged,
		hasOpenPr: !!openPr,
		branchDiffersFromBase,
		isOnDefaultBranch,
		needsPull,
		hasShipItHandler: !!onClickShipIt
	}));
	// Ship It ends in a PR + merge, so it needs gh just as much as create-pr.
	const showShipIt = $derived(
		githubAvailable && (snapshotPending ? heldShowShipIt : computedShowShipIt)
	);

	// Remember the coherent decision so the next switch has something real to
	// show while it revalidates.
	$effect(() => {
		if (snapshotPending) return;
		heldPrimaryAction = computedPrimaryAction;
		heldShowShipIt = computedShowShipIt;
		heldOpenPr = openPr;
	});

	// Local branch names offered as PR base targets in the Create PR popover.
	const baseBranchNames = $derived(snapshot.branches.filter(b => !b.remote).map(b => b.name));

	// Private access for this chat, published by the mounted ChatAccess.
	const access = $derived($chatAccessSummary);
	const accessPending = $derived(access?.pending ?? 0);

	// ── What the one header button says ─────────────────────────────────────
	const summary = $derived(computeHeaderSummary({
		accessPending,
		mergeBlocked: mergeBlocked && showShipIt,
		needsPull,
		commitsBehind: commitsBehindUpstream,
		isDiverged,
		hasChanges,
		additions,
		deletions,
		filesChanged,
		canPush,
		commitsAheadUpstream,
		prNumber: shownPr?.number ?? null,
		defaultBranch
	}));
	const pipeline = $derived(computePipeline({
		hasChanges,
		canPush,
		hasOpenPr: !!shownPr,
		showShipIt,
		mergeBlocked
	}));

	function togglePanel() {
		if (panelOpen) closePanel();
		else openDropdown('git-panel');
	}
	/** Close the panel and any popover inside it. */
	function closePanel() {
		cancelSheetDrag();
		openHeaderDropdown.update((cur) => (isPanelId(cur) ? null : cur));
	}
	/** Run an action in this workspace; progress remains inside its surface. */
	function fire<T>(fn: ((opts?: T) => void) | undefined, opts?: T) {
		if (actionsBusy) return;
		openDropdown('git-panel');
		// A bare row click passes NO argument (the handler falls back to saved
		// defaults); only the popover passes an explicit bag.
		if (opts === undefined) fn?.();
		else fn?.(opts);
	}

	function leaveForReview(fn: (() => void) | undefined) { closePanel(); fn?.(); }

	// Chevron handler for a git-action row: toggle its option popover.
	// The shared store guarantees single-open, so this also closes any sibling
	// popover. Never fires when actions are busy.
	function toggleActionDropdown(id: GitActionDropdownId) {
		if (actionsBusy) return;
		if ($openHeaderDropdown === id) closeActionDropdown(id);
		else openDropdown(id);
	}
	// Closing a popover returns to the panel — unless something else (an
	// outside click, an action firing) already closed the whole thing.
	const closeActionDropdown = (id: GitActionDropdownId) =>
		openHeaderDropdown.update((cur) => (cur === id ? 'git-panel' : cur));

	// Close the panel on outside click. This listener registers at mount, so it
	// runs BEFORE a popover's own outside-click handler: a click outside the
	// panel closes everything here, and the popover's later "return to panel"
	// update then finds nothing open and leaves it closed.
	function handleClickOutside(e: MouseEvent) {
		if (!panelOpen) return;
		const target = e.target as Node;
		if (panelRef?.contains(target) || triggerRef?.contains(target)) return;
		closePanel();
	}

	function handleKeydown(e: KeyboardEvent) {
		// A popover closes itself on Escape (back to the panel); the panel only
		// closes when it is the thing on top.
		if (e.key === 'Escape' && $openHeaderDropdown === 'git-panel') closePanel();
	}

	// Clear our shared-store flag on unmount so the panel doesn't re-open on remount.
	onDestroy(() => {
		openHeaderDropdown.update((cur) => (isPanelId(cur) ? null : cur));
	});

	const fileLabel = (n: number) => `${n} file${n === 1 ? '' : 's'}`;
	const commitLabel = (n: number) => `${n} commit${n === 1 ? '' : 's'}`;
	const MAX_FILES = 6;
</script>

<svelte:window onclick={handleClickOutside} onkeydown={handleKeydown} />

<div class="git-actions" class:sheet-layout={sheetLayout}>
	{#if error}
		<button class="git-btn" title={error} onclick={() => git.retryLoad()}>
			<TriangleAlert size={13} /> Git unavailable · Retry
		</button>
	{:else if isGitRepo}
	<button
		class="git-btn trigger tone-{summary.tone}"
		class:stale={snapshotPending}
		class:open={panelOpen}
		bind:this={triggerRef}
		onclick={togglePanel}
		aria-expanded={panelOpen}
		aria-haspopup="dialog"
		aria-label="Git actions: {summary.summary}{lastKnownSuffix}"
		title="{summary.summary}{lastKnownSuffix}"
	>
		{#if summary.icon === 'shield'}<ShieldCheck size={14} strokeWidth={1.8} />
		{:else if summary.icon === 'alert'}<TriangleAlert size={14} strokeWidth={1.6} />
		{:else if summary.icon === 'pull'}<ArrowDownToLine size={14} strokeWidth={1.6} />
		{:else if summary.icon === 'push'}<ArrowUpFromLine size={14} strokeWidth={1.6} />
		{:else if summary.icon === 'pr'}<GitPullRequest size={14} strokeWidth={1.6} />
		{:else}<GitBranch size={14} strokeWidth={1.6} />{/if}
		{#if summary.tone === 'changes' && (additions > 0 || deletions > 0)}
			<span class="stat-line">
				{#if additions > 0}<span class="stat additions">+{additions}</span>{/if}
				{#if deletions > 0}<span class="stat deletions">−{deletions}</span>{/if}
			</span>
		{/if}
		{#if summary.label}<span class="btn-label">{summary.label}</span>{/if}
		{#if summary.detail}<span class="btn-detail">{summary.detail}</span>{/if}
		{#if prIsLastKnown}
			<span class="last-known-dot" aria-label="Last known PR state — GitHub unreachable"></span>
		{/if}
		<ChevronDown size={12} strokeWidth={1.6} class="trigger-chev" />
	</button>

	{#if panelOpen}
		<!-- Scrim only takes effect in the bottom-sheet layout (see CSS). -->
		<div class="git-scrim" aria-hidden="true"></div>
		<div class="git-panel" class:dragging-sheet={draggingSheet} style:--sheet-offset={`${sheetOffset}px`} bind:this={panelRef} role="dialog" aria-label="Git actions">
			<button
				class="sheet-handle"
				aria-label="Close Git panel (drag down or tap)"
				onpointerdown={startSheetDrag}
				onpointermove={moveSheetDrag}
				onpointerup={endSheetDrag}
				onpointercancel={cancelSheetDrag}
				onlostpointercapture={cancelSheetDrag}
				onclick={activateSheetHandle}
			><span></span></button>
			<div class="gp-head">
				<button class="gp-close" onclick={closePanel} aria-label="Close Git panel"><X size={16} /></button>
				<div class="gp-branch" title={currentBranch ?? ''}>
					<GitBranch size={13} strokeWidth={1.6} />
					<span class="gp-branch-name">
						{#if projectName}<span class="gp-project">{projectName} / </span>{/if}{currentBranch ?? 'no branch'}
					</span>
					{#if currentBranch && !isOnDefaultBranch}
						<span class="gp-base">into <b>{defaultBranch}</b></span>
					{/if}
				</div>
				<div class="gp-sum">
					<span class="light tone-{summary.tone}" class:pending={snapshotPending}></span>
					<span class="gp-sum-text">{summary.summary}</span>
					{#if shownPr}
						<a class="pr-pill" class:conflict={mergeBlocked} href={shownPr.url} target="_blank" rel="noopener noreferrer" title={shownPr.title}>
							PR #{shownPr.number} {mergeBlocked ? 'conflicts' : 'open'}{#if prIsLastKnown}?{/if}
						</a>
					{/if}
				</div>
			</div>

			{#if activeEditor}
				{#key activeEditor}
					<GitActionOptionsDropdown id={editorId} action={activeEditor} {currentBranch} {defaultBranch}
						baseBranches={baseBranchNames} openPR={shownPr} {hasChanges} {isWorktreeMode}
						unpushed={snapshot.status?.unpushed} busy={actionsBusy || (activeEditor === 'shipit' && mergeBlocked)}
						onClose={() => closeActionDropdown(`git-${activeEditor}` as GitActionDropdownId)}
						onCommit={(o) => fire(onClickCommit, o)} onPush={(o) => fire(onClickPush, o)}
						onPull={(o) => fire(onClickPull, o)} onCreatePR={(o) => fire(onClickCreatePR, o)}
						onShipIt={(o) => fire(onClickShipIt, o)} />
				{/key}
			{:else}
				<GitProgressPopover />
			<!-- Ship It pipeline: one-click commit → push → PR → merge → cleanup -->
			<div class="gp-pipe" class:blocked={showShipIt && mergeBlocked}>
				{#if showShipIt && mergeBlocked}<TriangleAlert size={15} strokeWidth={1.6} />{:else}<Rocket size={15} strokeWidth={1.6} />{/if}
				<ol class="steps" aria-label="Ship It steps">
					{#each pipeline as step (step.id)}
						<li class="step {step.state}"><i></i>{step.label}</li>
					{/each}
				</ol>
				{#if showShipIt}
					<div class="split-btn-group ship-go" class:blocked={mergeBlocked}>
						<button
							class="split-main"
							class:stale={snapshotPending}
							onclick={() => fire(onClickShipIt)}
							disabled={primaryDisabled || mergeBlocked}
							aria-label="Ship It"
							title={isStreaming ? 'Cannot ship during streaming' : mergeBlocked
								? `PR #${shownPr?.number} has merge conflicts — resolve them before shipping`
								: `${shipItSummary}${lastKnownSuffix}`}
						>
							{mergeBlocked ? 'Fix conflicts first' : 'Ship It'}
						</button>
						<button
							class="split-chevron"
							class:active={shipItOptsOpen}
							onclick={() => toggleActionDropdown('git-shipit')}
							disabled={actionsBusy}
							aria-label="Ship It options"
							aria-controls={editorId}
							aria-expanded={shipItOptsOpen}
							title="Ship It options"
						>
							<ChevronDown size={11} strokeWidth={1.5} />
						</button>
					</div>
				{:else}
					<span class="ship-off">{githubAvailable ? 'Nothing to ship' : 'Needs GitHub'}</span>
				{/if}
			</div>

			<div class="gp-rows">
				<!-- Commit -->
				{#if primaryAction === 'commit'}
					<div class="row-group">
						<button class="row" aria-label="Commit" class:stale={snapshotPending} disabled={primaryDisabled}
							onclick={() => fire(onClickCommit)}
							title={isStreaming ? 'Cannot commit during streaming' : 'Quick commit with saved settings'}>
							<GitCommitHorizontal size={14} strokeWidth={1.5} />
							<span class="row-text"><span class="row-title">Commit</span><span class="row-sub"><span class="count">{fileLabel(filesChanged)}</span></span></span>
						</button>
						<button class="row-opts" class:active={commitOptsOpen} onclick={() => toggleActionDropdown('git-commit')} disabled={actionsBusy}
							aria-label="Commit options" aria-controls={editorId} aria-expanded={commitOptsOpen} title="Commit options">
							<ChevronDown size={12} strokeWidth={1.5} />
						</button>
					</div>
				{:else}
					<div class="row-group off">
						<button class="row" aria-label="Commit" disabled>
							<GitCommitHorizontal size={14} strokeWidth={1.5} />
							<span class="row-text"><span class="row-title">Commit</span><span class="row-sub">{hasChanges ? 'pull first' : 'nothing new'}</span></span>
						</button>
					</div>
				{/if}

				<!-- Push -->
				{#if canPush}
					<div class="row-group">
						<button class="row" aria-label="Push" class:stale={snapshotPending} disabled={primaryDisabled}
							onclick={() => fire(onClickPush)}
							title={isStreaming ? 'Cannot push during streaming' : `${commitLabel(commitsAheadUpstream)} to push`}>
							<ArrowUpFromLine size={14} strokeWidth={1.5} />
							<span class="row-text"><span class="row-title">Push</span><span class="row-sub"><span class="count">{commitLabel(commitsAheadUpstream)}</span></span></span>
						</button>
						<button class="row-opts" class:active={pushOptsOpen} onclick={() => toggleActionDropdown('git-push')} disabled={actionsBusy}
							aria-label="Push options" aria-controls={editorId} aria-expanded={pushOptsOpen} title="Push options">
							<ChevronDown size={12} strokeWidth={1.5} />
						</button>
					</div>
				{:else}
					<div class="row-group off">
						<button class="row" aria-label="Push" disabled>
							<ArrowUpFromLine size={14} strokeWidth={1.5} />
							<span class="row-text"><span class="row-title">Push</span><span class="row-sub">{!hasUpstream ? 'no upstream yet' : needsPull ? 'pull first' : hasChanges ? 'commit first' : 'up to date'}</span></span>
						</button>
					</div>
				{/if}

				<!-- Pull (when behind upstream, including diverged state) -->
				{#if needsPull}
					<div class="row-group">
						<button class="row" aria-label={isDiverged ? 'Sync' : 'Pull'} class:stale={snapshotPending} class:diverged={isDiverged} disabled={primaryDisabled}
							onclick={() => fire(onClickPull)}
							title={isStreaming
								? 'Cannot pull during streaming'
								: isDiverged
									? `Quick sync — ${commitsAheadUpstream} ahead, ${commitsBehindUpstream} behind upstream`
									: `Quick pull ${commitLabel(commitsBehindUpstream)} with saved settings`}>
							<ArrowDownToLine size={14} strokeWidth={1.5} />
							<span class="row-text"><span class="row-title">{isDiverged ? 'Sync' : 'Pull'}</span><span class="row-sub"><span class="count warn">{commitsBehindUpstream} behind</span></span></span>
						</button>
						<button class="row-opts" class:active={pullOptsOpen} onclick={() => toggleActionDropdown('git-pull')} disabled={actionsBusy}
							aria-label="Pull options" aria-controls={editorId} aria-expanded={pullOptsOpen} title="Pull options">
							<ChevronDown size={12} strokeWidth={1.5} />
						</button>
					</div>
				{:else}
					<div class="row-group off">
						<button class="row" aria-label="Pull" disabled>
							<ArrowDownToLine size={14} strokeWidth={1.5} />
							<span class="row-text"><span class="row-title">Pull</span><span class="row-sub">{hasUpstream ? 'nothing to pull' : 'no upstream yet'}</span></span>
						</button>
					</div>
				{/if}

				<!-- Create PR / open PR -->
				{#if primaryAction === 'create-pr'}
					<div class="row-group">
						<button class="row" aria-label="Create PR" class:stale={snapshotPending} disabled={primaryDisabled}
							onclick={() => fire(onClickCreatePR)}
							title={isStreaming ? 'Cannot create PR during streaming' : `Quick create PR (${commitLabel(commitsAhead)} ahead)`}>
							<GitPullRequest size={14} strokeWidth={1.5} />
							<span class="row-text"><span class="row-title">Create PR</span><span class="row-sub">into {defaultBranch}</span></span>
						</button>
						<button class="row-opts" class:active={prOptsOpen} onclick={() => toggleActionDropdown('git-pr')} disabled={actionsBusy}
							aria-label="Pull request options" aria-controls={editorId} aria-expanded={prOptsOpen} title="Pull request options">
							<ChevronDown size={12} strokeWidth={1.5} />
						</button>
					</div>
				{:else if shownPr}
					<div class="row-group">
						<a class="row link" href={shownPr.url} target="_blank" rel="noopener noreferrer" aria-label="Open PR #{shownPr.number} on GitHub" title={shownPr.title}>
							<GitPullRequest size={14} strokeWidth={1.5} />
							<span class="row-text"><span class="row-title">Open pull request</span><span class="row-sub">#{shownPr.number} on GitHub</span></span>
							<ExternalLink size={12} strokeWidth={1.5} class="row-trail" />
						</a>
					</div>
				{:else}
					<div class="row-group off">
						<button class="row" aria-label="Create PR" disabled>
							<GitPullRequest size={14} strokeWidth={1.5} />
							<span class="row-text"><span class="row-title">Create PR</span><span class="row-sub">{!githubAvailable ? 'needs GitHub' : hasChanges ? 'commit first' : isOnDefaultBranch ? `on ${defaultBranch}` : 'nothing to open'}</span></span>
						</button>
					</div>
				{/if}
			</div>

			<!-- Changed files (each opens the review panel) -->
			<div class="gp-files">
				<div class="gp-files-head">
					<span>{hasChanges ? `Changed files · ${filesChanged}` : 'Changed files'}</span>
					{#if hasChanges && onOpenDiffReview}
						<button class="gp-review" onclick={() => leaveForReview(onOpenDiffReview)}>Review all</button>
					{/if}
				</div>
				{#if hasChanges && changedFiles.length > 0}
					{#each changedFiles.slice(0, MAX_FILES) as f (f.file)}
						<button class="file" onclick={() => leaveForReview(onOpenDiffReview)} disabled={!onOpenDiffReview} title={f.file}>
							<span class="file-path">{f.file}</span>
							<span class="file-counts">
								{#if f.additions > 0}<span class="additions">+{f.additions}</span>{/if}
								{#if f.deletions > 0}<span class="deletions">−{f.deletions}</span>{/if}
								{#if f.is_binary}<span class="binary">binary</span>{/if}
							</span>
						</button>
					{/each}
					{#if changedFiles.length > MAX_FILES}
						<button class="file more" onclick={() => leaveForReview(onOpenDiffReview)} disabled={!onOpenDiffReview}>
							<span class="file-path">{changedFiles.length - MAX_FILES} more…</span>
						</button>
					{/if}
				{:else if hasChanges}
					<p class="gp-empty">{fileLabel(filesChanged)} changed.</p>
				{:else}
					<p class="gp-empty">Working tree is clean.</p>
				{/if}
			</div>

			<SwarmDropdown {sessionId} />

			{#if access || onDownloadChat}
				<div class="gp-foot">
					{#if access}
						{@const hasAny = access.pending > 0 || access.active > 0 || access.saved > 0 || access.error}
						<div class="row-group" class:off={!hasAny}>
							<button class="row" aria-label="Private access" disabled={!hasAny} onclick={() => { closePanel(); access.open(); }}>
								<ShieldCheck size={14} strokeWidth={1.5} />
								<span class="row-text">
									<span class="row-title">Private access</span>
									<span class="row-sub">
										{#if access.pending > 0}<span class="count attn">{access.pending} to approve</span>
										{:else if access.error}<span class="count warn">unavailable</span>
										{:else if access.active > 0}{access.active} active grant{access.active === 1 ? '' : 's'}
										{:else if access.saved > 0}{access.saved} saved connection{access.saved === 1 ? '' : 's'}
										{:else}none in this chat{/if}
									</span>
								</span>
								{#if hasAny}<ChevronRight size={12} strokeWidth={1.5} class="row-trail" />{/if}
							</button>
						</div>
					{/if}
					{#if onDownloadChat}
						<div class="row-group narrow-only">
							<button class="row" aria-label="Download chat history" disabled={downloadingChat} onclick={() => leaveForReview(onDownloadChat)}>
								<Download size={14} strokeWidth={1.5} />
								<span class="row-text"><span class="row-title">Download chat history</span><span class="row-sub">.jsonl</span></span>
							</button>
						</div>
					{/if}
				</div>
			{/if}
			{/if}
		</div>
	{/if}

	{:else if !isWorktreeMode}
		<!-- Only non-repo projects can be initialized, not removed branch checkouts. -->
		{#if projectName}
			<span class="git-btn project-only-badge" title="Project: {projectName}">
				<FolderClosed size={13} strokeWidth={1.5} />
				<span class="btn-label project-name">{projectName}</span>
			</span>
		{/if}
		<button
			class="git-btn init-btn"
			onclick={onClickInitRepo}
			title="Initialize a git repository for this project"
		>
			<FolderGit2 size={14} strokeWidth={1.5} />
			<span class="btn-label">Init Repo</span>
		</button>
	{/if}
</div>

<style>
	.git-actions {
		position: relative;
		display: flex;
		align-items: center;
		gap: 4px;
	}

	/* The action shown belongs to the previous coherent snapshot while the
	   current checkout revalidates — indicate it rather than flashing empty. */
	.stale {
		opacity: 0.55;
	}

	/* Subtle "this value is real but unconfirmed this cycle" marker. */
	.last-known-dot {
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: var(--warning, #d29922);
		opacity: 0.8;
	}

	.git-btn {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 28px;
		padding: 0 8px;
		background: transparent;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.7);
		color: var(--muted-foreground);
		font-size: 0.6875rem;
		font-weight: 500;
		cursor: pointer;
		transition: background-color 150ms ease, color 150ms ease, border-color 150ms ease;
		white-space: nowrap;
	}

	.git-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.git-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.git-btn :global(svg) {
		flex-shrink: 0;
	}

	.init-btn {
		border-color: var(--primary);
		color: var(--primary);
	}

	.init-btn:hover:not(:disabled) {
		background: color-mix(in oklch, var(--primary) 10%, transparent);
	}

	.btn-label {
		line-height: 1;
	}

	.project-name {
		max-width: 120px;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* ── The one header button ─────────────────────────────────────────────── */
	.trigger {
		gap: 6px;
		padding: 0 7px 0 9px;
		font-size: 0.71875rem;
	}

	.trigger.open {
		background: var(--accent);
		color: var(--foreground);
		border-color: var(--border-strong, var(--border));
	}

	.trigger :global(.trigger-chev) {
		color: var(--subtle-foreground, var(--muted-foreground));
		transition: transform 200ms ease;
	}

	.trigger.open :global(.trigger-chev) {
		transform: rotate(180deg);
	}

	.btn-detail {
		color: var(--subtle-foreground, var(--muted-foreground));
		line-height: 1;
	}

	.stat-line {
		display: inline-flex;
		gap: 5px;
		font-family: var(--font-mono);
	}

	.stat {
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		line-height: 1;
	}

	.additions {
		color: var(--success);
	}

	.deletions {
		color: var(--destructive);
	}

	/* Tone: the colour says what kind of attention the branch needs. */
	.trigger.tone-attn {
		color: var(--primary);
		border-color: color-mix(in oklch, var(--primary) 55%, transparent);
		background: color-mix(in oklch, var(--primary) 8%, transparent);
	}

	.trigger.tone-attn:hover:not(:disabled) {
		background: color-mix(in oklch, var(--primary) 16%, transparent);
		color: var(--primary);
	}

	.trigger.tone-blocked {
		color: var(--destructive);
		border-color: color-mix(in oklch, var(--destructive) 50%, transparent);
	}

	.trigger.tone-blocked:hover:not(:disabled) {
		color: var(--destructive);
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
	}

	.trigger.tone-behind :global(svg:first-child) {
		color: var(--warning);
	}

	/* ── Panel ─────────────────────────────────────────────────────────────── */
	.git-panel {
		position: absolute;
		top: calc(100% + 8px);
		right: 0;
		width: min(440px, calc(100vw - 24px));
		max-height: calc(100vh - 80px);
		overflow-y: auto;
		background: var(--popover, var(--card));
		border: 1px solid var(--border-strong, var(--border));
		border-radius: calc(var(--radius, 0.75rem) * 1.2);
		box-shadow: var(--shadow-l);
		z-index: 150;
		text-align: left;
		animation: panel-in 160ms cubic-bezier(0.2, 0.7, 0.2, 1);
		transform-origin: top right;
	}

	@keyframes panel-in {
		from { opacity: 0; transform: translateY(-4px) scale(0.985); }
		to { opacity: 1; transform: none; }
	}

	.git-scrim, .sheet-handle {
		display: none;
	}

	.gp-close { position: absolute; top: 8px; right: 8px; display: grid; place-items: center; width: 32px; height: 32px; border: 0; border-radius: .5rem; background: transparent; color: var(--muted-foreground); cursor: pointer; }
	.gp-close:hover { background: var(--hover-overlay); color: var(--foreground); }
	.gp-close:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.sheet-layout .gp-close { width: 44px; height: 44px; }
	.gp-head {
		position: sticky;
		top: 0;
		z-index: 2;
		background: var(--popover, var(--card));
		padding: 12px 54px 10px 14px;
		border-bottom: 1px solid var(--border);
		display: grid;
		gap: 6px;
	}

	.gp-branch {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.gp-branch :global(svg) {
		color: var(--subtle-foreground, var(--muted-foreground));
		flex-shrink: 0;
	}

	.gp-branch-name {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.gp-project {
		color: var(--muted-foreground);
		font-weight: 400;
	}

	.gp-base {
		flex: none;
		color: var(--subtle-foreground, var(--muted-foreground));
		font-weight: 400;
	}

	.gp-base b {
		font-weight: 500;
		color: var(--muted-foreground);
	}

	.gp-sum {
		display: flex;
		align-items: center;
		gap: 10px;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		min-width: 0;
	}

	.gp-sum-text {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.light {
		flex: none;
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--subtle-foreground, var(--muted-foreground));
	}

	.light.tone-attn, .light.tone-changes, .light.tone-push { background: var(--primary); }
	.light.tone-behind { background: var(--warning); }
	.light.tone-blocked { background: var(--destructive); }
	.light.tone-pr { background: var(--success); }
	.light.pending { opacity: 0.5; }

	.pr-pill {
		margin-left: auto;
		flex: none;
		font-size: 0.6875rem;
		padding: 1px 8px;
		border-radius: 999px;
		border: 1px solid color-mix(in oklch, var(--success) 40%, transparent);
		color: var(--success);
		text-decoration: none;
	}

	.pr-pill:hover {
		background: color-mix(in oklch, var(--success) 10%, transparent);
	}

	.pr-pill.conflict {
		color: var(--destructive);
		border-color: color-mix(in oklch, var(--destructive) 40%, transparent);
	}

	.pr-pill.conflict:hover {
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
	}

	/* Pipeline strip */
	.gp-pipe {
		display: grid;
		grid-template-columns: auto 1fr auto;
		align-items: center;
		gap: 10px;
		padding: 11px 14px;
		border-bottom: 1px solid var(--border);
	}

	.gp-pipe > :global(svg) {
		color: var(--primary);
	}

	.gp-pipe.blocked > :global(svg) {
		color: var(--destructive);
	}

	.steps {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		align-items: center;
		min-width: 0;
		overflow: hidden;
		font-size: 0.6875rem;
		color: var(--subtle-foreground, var(--muted-foreground));
		white-space: nowrap;
	}

	.step {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.step::after {
		content: '';
		width: 14px;
		height: 1px;
		background: var(--border-strong, var(--border));
		margin: 0 6px;
	}

	.step:last-child::after {
		display: none;
	}

	.step i {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--border-strong, var(--border));
	}

	.step.done { color: var(--muted-foreground); }
	.step.done i { background: var(--success); }
	.step.next { color: var(--foreground); font-weight: 500; }
	.step.next i { background: var(--primary); box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 25%, transparent); }
	.step.stop { color: var(--destructive); }
	.step.stop i { background: var(--destructive); }

	/* Split button: main action + chevron, sharing a seam. The group is the
	   positioning anchor for its GitActionOptionsDropdown popover. */
	.split-btn-group {
		position: relative;
		display: inline-flex;
		align-items: stretch;
	}

	.ship-go {
		height: 26px;
		border-radius: calc(var(--radius, 0.75rem) * 0.7);
		overflow: visible;
	}

	.ship-go .split-main,
	.ship-go .split-chevron {
		display: flex;
		align-items: center;
		gap: 5px;
		box-sizing: border-box;
		height: 100%;
		padding: 0 9px;
		border: 1px solid color-mix(in oklch, var(--primary) 55%, transparent);
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		color: var(--primary);
		font-size: 0.71875rem;
		font-weight: 600;
		cursor: pointer;
		white-space: nowrap;
		transition: background-color 150ms ease;
	}

	.ship-go .split-main {
		border-radius: calc(var(--radius, 0.75rem) * 0.7) 0 0 calc(var(--radius, 0.75rem) * 0.7);
	}

	.ship-go .split-chevron {
		border-radius: 0 calc(var(--radius, 0.75rem) * 0.7) calc(var(--radius, 0.75rem) * 0.7) 0;
		border-left: 0;
		padding: 0 5px;
	}

	.ship-go .split-main:hover:not(:disabled),
	.ship-go .split-chevron:hover:not(:disabled),
	.ship-go .split-chevron.active {
		background: color-mix(in oklch, var(--primary) 18%, transparent);
	}

	.ship-go .split-main:disabled,
	.ship-go .split-chevron:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.ship-go.blocked .split-main,
	.ship-go.blocked .split-chevron {
		border-color: color-mix(in oklch, var(--destructive) 55%, transparent);
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		color: var(--destructive);
	}

	.ship-off {
		font-size: 0.6875rem;
		color: var(--subtle-foreground, var(--muted-foreground));
		padding: 0 4px;
	}

	/* Action rows */
	.gp-rows {
		padding: 6px;
		display: grid;
		gap: 1px;
		border-bottom: 1px solid var(--border);
	}

	.row-group {
		position: relative;
		display: grid;
		grid-template-columns: 1fr auto;
		align-items: stretch;
	}

	.row-group.off {
		opacity: 0.45;
	}

	.row {
		display: grid;
		grid-template-columns: 22px 1fr auto;
		align-items: center;
		gap: 10px;
		height: 36px;
		padding: 0 8px;
		border: 0;
		border-radius: calc(var(--radius, 0.75rem) * 0.8);
		background: transparent;
		color: var(--foreground);
		font: inherit;
		text-align: left;
		cursor: pointer;
		text-decoration: none;
		min-width: 0;
	}

	.row:hover:not(:disabled) {
		background: var(--hover-overlay);
	}

	.row:disabled {
		cursor: default;
	}

	.row > :global(svg) {
		color: var(--muted-foreground);
		justify-self: center;
	}

	.row.diverged > :global(svg) {
		color: var(--warning);
	}

	.row :global(.row-trail) {
		color: var(--subtle-foreground, var(--muted-foreground));
	}

	.row-text {
		display: flex;
		align-items: baseline;
		gap: 8px;
		min-width: 0;
		font-size: 0.78125rem;
	}

	.row-title {
		flex: none;
	}

	.row-sub {
		font-size: 0.6875rem;
		color: var(--subtle-foreground, var(--muted-foreground));
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.count {
		font-family: var(--font-mono);
		font-size: 0.65625rem;
		font-weight: 600;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 14%, transparent);
		padding: 1px 5px;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
	}

	.count.warn {
		color: var(--warning);
		background: color-mix(in oklch, var(--warning) 14%, transparent);
	}

	.count.attn {
		color: var(--primary-foreground);
		background: var(--primary);
	}

	.row-opts {
		width: 30px;
		margin-left: 2px;
		border: 0;
		border-radius: calc(var(--radius, 0.75rem) * 0.6);
		background: transparent;
		color: var(--subtle-foreground, var(--muted-foreground));
		display: grid;
		place-items: center;
		cursor: pointer;
	}

	.row-opts:hover:not(:disabled),
	.row-opts.active {
		background: var(--active-overlay, var(--hover-overlay));
		color: var(--foreground);
	}

	.row-opts:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	/* Files */
	.gp-files {
		padding: 8px 6px 6px;
	}

	.gp-files-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 0 8px 4px;
		font-size: 0.6875rem;
		color: var(--subtle-foreground, var(--muted-foreground));
	}

	.gp-review {
		border: 0;
		background: transparent;
		color: var(--primary);
		font: inherit;
		font-size: 0.6875rem;
		font-weight: 500;
		cursor: pointer;
		padding: 2px 4px;
		border-radius: 4px;
	}

	.gp-review:hover {
		text-decoration: underline;
	}

	.file {
		display: grid;
		grid-template-columns: 1fr auto;
		gap: 10px;
		align-items: center;
		width: 100%;
		height: 26px;
		padding: 0 8px;
		border: 0;
		border-radius: calc(var(--radius, 0.75rem) * 0.6);
		background: transparent;
		color: var(--muted-foreground);
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		text-align: left;
		cursor: pointer;
	}

	.file:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.file:disabled {
		cursor: default;
	}

	.file-path {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		direction: rtl;
		text-align: left;
	}

	.file.more .file-path {
		direction: ltr;
		color: var(--subtle-foreground, var(--muted-foreground));
	}

	.file-counts {
		display: flex;
		gap: 6px;
		font-weight: 600;
		font-size: 0.65625rem;
		font-variant-numeric: tabular-nums;
	}

	.binary {
		color: var(--subtle-foreground, var(--muted-foreground));
		font-weight: 400;
	}

	.gp-empty {
		margin: 0;
		padding: 2px 8px 4px;
		font-size: 0.75rem;
		color: var(--subtle-foreground, var(--muted-foreground));
	}

	.gp-foot {
		padding: 6px;
		border-top: 1px solid var(--border);
		display: grid;
		gap: 1px;
	}

	.narrow-only {
		display: none;
	}

	:global(.spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	/* ── Narrow screens: the panel becomes a bottom sheet ──────────────────── */
	/* Native mobile builds and touch/narrow web windows use a bottom sheet. */
	.sheet-layout .git-btn.trigger {
		height: 36px;
		padding: 0 10px 0 12px;
		font-size: 0.75rem;
		border-radius: calc(var(--radius, 0.75rem) * 0.9);
	}

	.sheet-layout .git-scrim {
		display: block;
		position: fixed;
		inset: 0;
		background: rgb(0 0 0 / 0.5);
		z-index: 140;
		animation: scrim-in 180ms ease;
	}

	@keyframes scrim-in {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	.sheet-layout .git-panel {
		position: fixed;
		top: auto;
		left: 0;
		right: 0;
		bottom: 0;
		width: auto;
		max-height: 88dvh;
		border-radius: 18px 18px 0 0;
		border-bottom: 0;
		padding-bottom: env(safe-area-inset-bottom);
		transform-origin: bottom center;
		transform: translateY(var(--sheet-offset, 0px));
		transition: transform 180ms ease;
		animation: sheet-in 220ms cubic-bezier(0.2, 0.7, 0.2, 1);
		overscroll-behavior: contain;
	}

	.sheet-layout .git-panel.dragging-sheet {
		animation: none;
		transition: none;
	}

	@keyframes sheet-in {
		from { transform: translateY(24px); opacity: 0; }
		to { transform: none; opacity: 1; }
	}

	/* A real pointer target, separate from the normally scrollable content. */
	.sheet-layout .sheet-handle {
		display: grid;
		place-items: center;
		width: 100%;
		height: 32px;
		padding: 0;
		border: 0;
		background: transparent;
		cursor: grab;
		touch-action: none;
		user-select: none;
	}
	.sheet-layout .dragging-sheet .sheet-handle { cursor: grabbing; }
	.sheet-layout .sheet-handle span {
		width: 36px;
		height: 4px;
		border-radius: 2px;
		background: var(--border-strong, var(--border));
	}

	.sheet-layout .gp-head {
		padding: 10px 16px 12px;
	}

	.sheet-layout .gp-branch {
		font-size: 0.78125rem;
	}

	.sheet-layout .gp-pipe {
		grid-template-columns: auto 1fr;
		padding: 12px 16px;
	}

	.sheet-layout .ship-go, .sheet-layout .ship-off {
		grid-column: 1 / -1;
		justify-self: stretch;
	}

	.sheet-layout .ship-go { height: 44px; }

	.sheet-layout .ship-go .split-main {
		flex: 1;
		justify-content: center;
		font-size: 0.8125rem;
	}

	.sheet-layout .ship-go .split-chevron {
		width: 44px;
		justify-content: center;
		padding: 0;
	}

	.sheet-layout .gp-rows, .sheet-layout .gp-files, .sheet-layout .gp-foot {
		padding: 6px 10px;
	}

	.sheet-layout .row {
		height: 44px;
	}

	.sheet-layout .row-text {
		font-size: 0.875rem;
	}

	.sheet-layout .row-sub {
		font-size: 0.75rem;
	}

	.sheet-layout .row-opts {
		width: 44px;
	}

	.sheet-layout .gp-review { min-height: 44px; }

	.sheet-layout .file {
		height: 44px;
		font-size: 0.75rem;
	}

	.sheet-layout .narrow-only {
		display: grid;
	}
	/* Keep room for the session title and download on a phone's single bar.
	   The icon/tone and accessible name retain the full Git status. */
	@media (max-width: 500px) {
		.sheet-layout .trigger .stat-line,
		.sheet-layout .trigger .btn-label,
		.sheet-layout .trigger .btn-detail { display: none; }
	}

	@media (prefers-reduced-motion: reduce) {
		.git-btn, .trigger :global(.trigger-chev), .row-opts, .ship-go .split-main, .ship-go .split-chevron {
			transition: none;
		}
		.git-panel, .git-scrim {
			animation: none;
			transition: none;
		}
	}
</style>

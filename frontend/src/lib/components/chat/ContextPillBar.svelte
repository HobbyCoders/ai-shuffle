<script lang="ts">
	/**
	 * ContextPillBar - the loose row of context chips that floats above the
	 * chat composer box: agent · | · runner · repository · branch · worktree.
	 *
	 * New draft (no durable session ID yet): every chip is a selector, because the
	 * execution context (runner / repository / branch / worktree) has to be
	 * chosen before the first send. The runner and worktree chips come from
	 * ExecutionModeBar; the repository chip and the local-mode branch chip are
	 * rendered here and handed to it as its `between` snippet so the DOM order
	 * matches the visual order. GitHub auth gates the runner/branch/worktree
	 * controls, as before.
	 *
	 * Active chat: the same row, now describing the session's context. The
	 * agent chip stays a selector and the branch chip stays the checkout
	 * selector where it was available before; the runner chip opens the
	 * "Move this session" menu for both local and remote owners. A degraded
	 * session (queued work, no runner online) shows a read-only warning chip.
	 *
	 * `sheetMode` renders the same selectors as stacked rows inside the mobile
	 * controls sheet. The chip dropdowns open upward since this row sits just
	 * above the input.
	 */
	import { onDestroy } from 'svelte';
	import ProfileSelector from './ProfileSelector.svelte';
	import WorkspaceFolderSelector from './WorkspaceFolderSelector.svelte';
	import BranchSelector from './BranchSelector.svelte';
	import ExecutionModeBar from '$lib/components/layout/ExecutionModeBar.svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import { computeContextBadge, shouldShowBranchPill, shouldShowEmptyStateBranchPill } from './contextPills';
	import { gitStateQuery, runnerBadgeMenuItems } from './runner-badge-menu';
	import type { RunnerBadgeMenuItem } from './runner-badge-menu';
	import { pendingHandoff, handoffActivitySince, moveBlocked } from './pending-handoff';
	import { activeSession } from '$lib/stores/active-session';
	import { clusterProjects, projects, sessions, appData } from '$lib/stores/app-data.svelte';
	import { githubAuthenticated } from '$lib/stores/auth.svelte';
	import { git, gitStatus } from '$lib/stores/git.svelte';
	import { subagentChats } from '$lib/stores/subagent-chats';
	import { openHeaderDropdown, openDropdown, closeDropdown } from '$lib/stores/header-dropdowns';
	import { listInstalls, getClusterGitState, type Install } from '$lib/api/installs';
	import {
		adoptSession,
		moveSession,
		getQueuedMove,
		queueMove,
		cancelQueuedMove,
		type QueuedMoveBusy
	} from '$lib/api/sessions';
	import { notify } from '$lib/stores/notifications';
	import { GitBranch, Monitor, MonitorOff, Check, Loader2, X } from 'lucide-svelte';
	import { checkoutEligibility, type EligibilityMap } from '$lib/components/layout/execution-mode-eligibility';

	interface Props {
		/** Render the selectors as stacked, full-width controls inside the mobile sheet. */
		sheetMode?: boolean;
	}

	let { sheetMode = false }: Props = $props();

	const projectName = $derived(
		$projects.find((p) => p.id === $activeSession.project)?.name ??
			$clusterProjects.find((p) => p.project_v2_id === $activeSession.projectV2Id)?.name ??
			null
	);

	// The sessions-board summary row for the active session — source of the
	// server-derived ownership/health fields (degraded, is_remote, device name +
	// online) the read-only context chips read.
	const sessionRow = $derived(
		$sessions.find((s) => s.id === $activeSession.sessionId) ?? null
	);

	// V2 Q7: the active session's server-derived degraded flag (queued work, no
	// runner online).
	const degraded = $derived(sessionRow?.degraded ?? false);

	// A persisted session can legitimately have no messages while it is being
	// restored, or before its first message is submitted. Its durable ID is the
	// distinction from a compose-time draft; message count alone would render
	// that session as a new execution-mode selector row.
	const isEmptyState = $derived($activeSession.sessionId == null);
	// The context badge/branch helpers historically use messageCount as their
	// "active chat" gate. Keep that gate true for a durable zero-message
	// session, while retaining zero for a genuine compose-time draft.
	const contextMessageCount = $derived(
		isEmptyState ? 0 : Math.max(1, $activeSession.messages.length)
	);

	const isWorktreeSession = $derived(!!($activeSession.worktreeId || $activeSession.worktreeBranch));
	// Auto naming starts before the session has a worktree ID or branch.
	const isWorktreeSelected = $derived(isWorktreeSession || $activeSession.isCreatingWorktree);

	// A subagent's own chat (Task/Swarm child) is read and message only: its
	// branch and runner show read-only, with no checkout or move controls.
	const isSubagentChat = $derived(!!$activeSession.sessionId && $subagentChats.has($activeSession.sessionId));

	// Interactive branch chip in an active chat. Remote / degraded sessions show
	// read-only context instead.
	const showBranch = $derived(
		shouldShowBranchPill({
			messageCount: contextMessageCount,
			project: $activeSession.project || null,
			githubAuthenticated: $githubAuthenticated,
			isRemote: sessionRow?.is_remote ?? false,
			degraded,
			isSubagent: isSubagentChat
		})
	);

	// New-session screen: show the checkout-branch chip in LOCAL mode, where the
	// ExecutionModeBar has no branch picker of its own. In worktree mode that bar
	// owns branch selection (which branch to base the worktree on), so we don't
	// add a second branch control. See contextPills.shouldShowEmptyStateBranchPill.
	const showEmptyStateBranch = $derived(
		shouldShowEmptyStateBranchPill({
			messageCount: contextMessageCount,
			project: $activeSession.project || null,
			githubAuthenticated: $githubAuthenticated,
			worktreeIntent: $activeSession.worktreeIntent,
			// Reuse the git store's repo signal — a non-git project has no branches.
			isGitRepo: $gitStatus?.is_git_repo !== false
		})
	);

	// Read-only context (runner / degraded / unauthenticated branch) for an
	// active chat — see contextPills.computeContextBadge for the gating.
	const badge = $derived(
		computeContextBadge({
			messageCount: contextMessageCount,
			projectName,
			isWorktree: isWorktreeSession,
			selectedWorktreeBranch: $activeSession.selectedWorktreeBranch,
			worktreeBranch: $activeSession.worktreeBranch,
			githubAuthenticated: $githubAuthenticated,
			degraded,
			isRemote: sessionRow?.is_remote ?? false,
			deviceName: sessionRow?.device_name ?? null,
			deviceOnline: sessionRow?.device_online ?? false
		})
	);

	// Branch shown read-only in an active chat when the interactive chip is not
	// available: the worktree's branch, else the checkout's current branch.
	const readOnlyBranch = $derived.by(() => {
		if (badge?.branch) return badge.branch;
		if (isWorktreeSession) {
			return $activeSession.selectedWorktreeBranch || $activeSession.worktreeBranch || null;
		}
		if ($activeSession.project) return $gitStatus?.current_branch ?? null;
		return null;
	});

	let showCreateBranchModal = $state(false);

	const showRunnerMenu = $derived($openHeaderDropdown === 'runner');
	let runnerMenuRef = $state<HTMLDivElement | null>(null);
	let runnerInstalls = $state<Install[]>([]);
	let runnerEligibility = $state<EligibilityMap>(null);
	let runnerMenuLoading = $state(false);
	let runnerMenuError = $state('');
	/**
	 * The move/continue in flight. It commits, pushes, fetches and syncs
	 * context, which can take a minute, so the runner chip shows it as busy.
	 */
	let runnerAction = $state<{ sessionId: string; label: string } | null>(null);
	const runnerActionPending = $derived(runnerAction !== null);
	/** Brief confirmation on the chip after a move/continue succeeded. */
	let runnerActionDone = $state<{ sessionId: string; label: string } | null>(null);
	let runnerDoneTimer: ReturnType<typeof setTimeout> | null = null;
	/** Why the last move/continue was refused, shown inside the reopened menu. */
	let runnerActionError = $state('');
	let pendingResume = $state<{ item: RunnerBadgeMenuItem; sessionId: string } | null>(null);
	/**
	 * A running or parked chat cannot move now (#1819). The menu then says what
	 * is running and offers "Move when this turn ends" instead of the move.
	 */
	let busyChoice = $state<{ item: RunnerBadgeMenuItem; sessionId: string; busy: QueuedMoveBusy } | null>(null);
	let runnerChecking = $state(false);
	/**
	 * A move queued from this chip, shown until the chat's own hand-off rows
	 * (from `seen` on) take over. `cancelled` hides a queued row the owning
	 * worker has not yet answered with its `cancelled` row.
	 */
	let ownerQueued = $state<{ sessionId: string; worker: string; seen: number; afterQueued: boolean } | null>(null);
	let queueCancelled = $state<{ sessionId: string; seen: number } | null>(null);

	/**
	 * A queued hand-off: the agent's HandOffSession (#1809) or the owner's
	 * "Move when this turn ends" (#1819). It runs on the owning worker when the
	 * turn ends, so the chip learns of it from the chat, and shows the same
	 * pending state as a move the owner started here.
	 */
	const handoff = $derived.by(() => {
		const sid = $activeSession.sessionId;
		const messages = $activeSession.messages;
		if (queueCancelled?.sessionId === sid && !handoffActivitySince(messages, queueCancelled.seen)) return null;
		const fromChat = pendingHandoff(messages);
		if (fromChat) return fromChat;
		if (ownerQueued?.sessionId === sid && !handoffActivitySince(messages, ownerQueued.seen)) {
			return { worker: ownerQueued.worker, phase: 'queued' as const, afterQueued: ownerQueued.afterQueued };
		}
		return null;
	});
	const MOVE_TITLE = 'Committing, pushing and syncing this session. This can take a minute.';
	const runnerBusy = $derived.by((): { label: string; title: string } | null => {
		if (runnerAction && runnerAction.sessionId === $activeSession.sessionId) {
			return { label: runnerAction.label, title: MOVE_TITLE };
		}
		if (!handoff) return null;
		return handoff.phase === 'moving'
			? { label: `Moving to ${handoff.worker}…`, title: MOVE_TITLE }
			: {
					label: handoff.afterQueued
						? `Moving to ${handoff.worker} after the queued messages`
						: `Moving to ${handoff.worker} after this turn`,
					title: `A move to ${handoff.worker} is queued. When this turn, the queued messages and background work end, the app moves this session there. Messages you send now are sent there after the move.`
				};
	});
	/** No second move while one is running or queued. */
	const runnerLocked = $derived(runnerActionPending || !!handoff);
	const canCancelQueued = $derived(handoff?.phase === 'queued' && !runnerActionPending);
	const runnerDone = $derived(
		!runnerBusy && runnerActionDone && runnerActionDone.sessionId === $activeSession.sessionId
			? runnerActionDone
			: null
	);

	const sessionHasProject = $derived(!!(sessionRow?.project_id || sessionRow?.project_v2_id));
	const runnerMenu = $derived(
		runnerBadgeMenuItems({
			ownerId: sessionRow?.instance_id ?? null,
			isRemote: sessionRow?.is_remote ?? false,
			installs: runnerInstalls,
			eligibility: runnerEligibility,
			hasProject: sessionHasProject,
			isWorktree: isWorktreeSession
		})
	);

	async function loadRunnerMenu() {
		runnerMenuLoading = true;
		runnerMenuError = '';
		const q = gitStateQuery({
			project_id: sessionRow?.project_id,
			project_v2_id: sessionRow?.project_v2_id
		});
		const gitStateReq = q
			? ('project_id' in q
					? getClusterGitState(q.project_id)
					: getClusterGitState(null, q.project_v2_id)
				).catch((e) => {
					console.warn('[ContextPillBar] Failed to load git state:', e);
					return null;
				})
			: Promise.resolve(null);
		try {
			runnerInstalls = (await listInstalls()).installs;
		} catch (e) {
			console.warn('[ContextPillBar] Failed to list runners:', e);
			runnerInstalls = [];
			runnerMenuError = 'Could not load workers. Try again.';
		}
		const gitState = await gitStateReq;
		runnerEligibility = checkoutEligibility(gitState);
		runnerMenuLoading = false;
	}

	function toggleRunnerMenu() {
		// The chip is disabled while a move runs or is queued; never start a second one.
		if (runnerLocked) return;
		runnerActionError = '';
		busyChoice = null;
		if (showRunnerMenu) {
			closeDropdown('runner');
			return;
		}
		openDropdown('runner');
		void loadRunnerMenu();
	}

	async function runMenuAction(item: RunnerBadgeMenuItem, confirmed = false) {
		if (!item.action || item.disabled || runnerLocked || runnerChecking) return;
		const sessionId = $activeSession.sessionId;
		if (!sessionId) return;
		if (!confirmed) {
			// A running or parked chat moves when its work ends, never mid-turn.
			runnerChecking = true;
			let busy: QueuedMoveBusy | null = null;
			try {
				busy = (await getQueuedMove(sessionId)).busy;
			} catch {
				// An older owner cannot say: the move itself still refuses a busy chat.
			} finally {
				runnerChecking = false;
			}
			if (sessionId !== $activeSession.sessionId) return;
			if (busy && moveBlocked(busy)) {
				busyChoice = { item, sessionId, busy };
				return;
			}
		}
		// Only Git projects commit and push on the way; plain chats move history alone.
		if (sessionHasProject && !confirmed) {
			pendingResume = { item, sessionId };
			closeDropdown('runner');
			return;
		}
		const adopt = item.action.kind === 'adopt';
		runnerAction = { sessionId, label: adopt ? 'Continuing here…' : `Moving to ${item.label}…` };
		runnerActionDone = null;
		runnerActionError = '';
		closeDropdown('runner');
		try {
			if (item.action.kind === 'adopt') {
				await adoptSession(sessionId);
			} else {
				await moveSession(sessionId, item.action.targetInstanceId);
			}
			const done = adopt ? 'Now runs here' : `Moved to ${item.label}`;
			showRunnerDone(sessionId, done);
			notify.success(adopt ? 'This session now runs on this worker.' : `This session now runs on ${item.label}.`);
			await appData.loadSessions();
		} catch (e) {
			const detail =
				e && typeof e === 'object' && 'detail' in e && typeof (e as { detail: unknown }).detail === 'string'
					? (e as { detail: string }).detail
					: 'Could not change this session\'s runner';
			notify.error(detail);
			// The confirmation closed the menu. Reopen it with the reason next
			// to the destinations, so a refusal (busy checkout, unreachable
			// worker) shows where the move was started.
			if (sessionId === $activeSession.sessionId) {
				runnerActionError = detail;
				openDropdown('runner');
				void loadRunnerMenu();
			}
		} finally {
			runnerAction = null;
		}
	}

	async function queueChoice(stop: boolean) {
		const choice = busyChoice;
		if (!choice?.item.action || runnerLocked) return;
		busyChoice = null;
		closeDropdown('runner');
		const target = choice.item.action.kind === 'move' ? choice.item.action.targetInstanceId : undefined;
		const seen = $activeSession.messages.length;
		try {
			const result = await queueMove(choice.sessionId, target, stop);
			const afterQueued = result.after === 'queued';
			ownerQueued = { sessionId: choice.sessionId, worker: result.worker, seen, afterQueued };
			queueCancelled = null;
			notify.info(
				stop
					? `Stopping its work, then moving to ${result.worker}.`
					: afterQueued
						? `Moves to ${result.worker} after the queued messages.`
						: `Moves to ${result.worker} when this turn ends.`
			);
		} catch (e) {
			const detail =
				e && typeof e === 'object' && 'detail' in e && typeof (e as { detail: unknown }).detail === 'string'
					? (e as { detail: string }).detail
					: 'Could not queue the move';
			notify.error(detail);
			if (choice.sessionId === $activeSession.sessionId) {
				runnerActionError = detail;
				openDropdown('runner');
				void loadRunnerMenu();
			}
		}
	}

	async function cancelQueued() {
		const sessionId = $activeSession.sessionId;
		if (!sessionId || !canCancelQueued) return;
		const seen = $activeSession.messages.length;
		try {
			await cancelQueuedMove(sessionId);
			ownerQueued = null;
			queueCancelled = { sessionId, seen };
			notify.info('Move cancelled.');
		} catch (e) {
			const detail =
				e && typeof e === 'object' && 'detail' in e && typeof (e as { detail: unknown }).detail === 'string'
					? (e as { detail: string }).detail
					: 'Could not cancel the move';
			notify.error(detail);
		}
	}

	function showRunnerDone(sessionId: string, label: string) {
		if (runnerDoneTimer) clearTimeout(runnerDoneTimer);
		runnerActionDone = { sessionId, label };
		runnerDoneTimer = setTimeout(() => {
			runnerActionDone = null;
			runnerDoneTimer = null;
		}, 4000);
	}

	function confirmResume() {
		const pending = pendingResume;
		pendingResume = null;
		if (pending && !runnerLocked && pending.sessionId === $activeSession.sessionId) {
			void runMenuAction(pending.item, true);
		}
	}

	function handleRunnerClickOutside(e: MouseEvent) {
		if (showRunnerMenu && runnerMenuRef && !runnerMenuRef.contains(e.target as Node)) {
			closeDropdown('runner');
		}
	}

	function handleRunnerKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && showRunnerMenu) {
			busyChoice = null;
			closeDropdown('runner');
		}
	}

	onDestroy(() => {
		closeDropdown('runner');
		if (runnerDoneTimer) clearTimeout(runnerDoneTimer);
	});

	// The voice-call start affordance no longer lives here (ruling VM19): it is
	// an action button in the composer's controls row (VoiceCallButton).
</script>

<svelte:window onclick={handleRunnerClickOutside} onkeydown={handleRunnerKeydown} />

<ConfirmDialog
	open={!!pendingResume}
	title={pendingResume?.item.action?.kind === 'move' ? `Commit, push and move to ${pendingResume.item.label}?` : 'Commit, push and continue here?'}
	message="The owning worker will commit all non-ignored changes and push this branch to origin. The destination worker will fetch the branch and sync the conversation and context. Ignored files stay on the original worker. If a later step fails, the commit and push are not undone."
	confirmLabel={pendingResume?.item.action?.kind === 'move' ? 'Commit, push and move' : 'Commit, push and continue'}
	onConfirm={confirmResume}
	onCancel={() => (pendingResume = null)}
/>

{#snippet repoChip()}
	{#if projectName}
		<WorkspaceFolderSelector {projectName} {sheetMode} />
	{/if}
{/snippet}

<div class="context-pill-bar" class:sheet-mode={sheetMode} role="toolbar" aria-label="Session context">
	<ProfileSelector {sheetMode} />
	{#if $activeSession.mode !== 'chat'}
	<span class="ctx-sep" aria-hidden="true"></span>

	{#if isEmptyState}
		{#if $activeSession.project || $activeSession.projectV2Id}
			<ExecutionModeBar {sheetMode}>
				{#snippet between()}
					{@render repoChip()}
					{#if showEmptyStateBranch}
						<BranchSelector
							{sheetMode}
							projectName={sheetMode ? projectName : null}
							onClickNewBranch={() => (showCreateBranchModal = true)}
						/>
					{/if}
				{/snippet}
			</ExecutionModeBar>
		{:else}
			{@render repoChip()}
		{/if}
	{:else}
		<!-- Both local and remote persisted sessions expose the move menu. -->
		{#if badge?.degraded}
			<span class="ctx-chip readonly degraded" title={badge.title}>
				<MonitorOff size={12} strokeWidth={1.6} aria-hidden="true" />
				<span class="ctx-chip-label">Read-only — no runner online</span>
			</span>
		{:else if sessionRow && !isSubagentChat}
			<div class="runner-badge-wrap" bind:this={runnerMenuRef}>
				<button
					type="button"
					class="ctx-chip runner"
					class:open={showRunnerMenu}
					class:busy={!!runnerBusy}
					class:done={!!runnerDone}
					title={runnerBusy
						? runnerBusy.title
						: badge?.title || 'Runs on this device — move this session to another worker'}
					aria-label={runnerBusy
						? runnerBusy.label
						: `Runner: ${badge?.runner?.deviceName ?? 'Local'}${badge?.runner && !badge.runner.online ? ' (offline)' : ''}`}
					aria-busy={runnerBusy ? 'true' : undefined}
					disabled={runnerLocked}
					aria-haspopup="menu"
					aria-expanded={showRunnerMenu}
					onclick={toggleRunnerMenu}
				>
					{#if runnerBusy}
						<Loader2 size={12} strokeWidth={1.8} class="spin" aria-hidden="true" />
						<span class="ctx-chip-label">{runnerBusy.label}</span>
					{:else if runnerDone}
						<Check size={12} strokeWidth={2.2} aria-hidden="true" />
						<span class="ctx-chip-label">{runnerDone.label}</span>
					{:else}
						{#if !badge?.runner || badge.runner.online}
							<Monitor size={12} strokeWidth={1.6} aria-hidden="true" />
						{:else}
							<MonitorOff size={12} strokeWidth={1.6} aria-hidden="true" />
						{/if}
						<span class="ctx-chip-label">{badge?.runner?.deviceName ?? 'Local'}</span>
					{/if}
				</button>
				{#if canCancelQueued}
					<button
						type="button"
						class="queued-cancel"
						title="Cancel move"
						aria-label="Cancel queued move"
						onclick={cancelQueued}
					>
						<X size={12} strokeWidth={2} aria-hidden="true" />
					</button>
				{/if}
				<span class="sr-only" aria-live="polite">{runnerBusy?.label ?? runnerDone?.label ?? ''}</span>
				{#if showRunnerMenu}
					<div class="runner-dropdown" role="menu" aria-label="Move this session">
						{#if busyChoice}
							<div class="runner-busy">
								<strong>{busyChoice.busy.running ? 'This chat is running' : 'Background work is running'}</strong>
								{#if busyChoice.busy.tasks.length || busyChoice.busy.queued || busyChoice.busy.wake}
									<ul class="runner-busy-tasks">
										{#if busyChoice.busy.queued}
											<li>{busyChoice.busy.queued === 1 ? '1 queued message' : `${busyChoice.busy.queued} queued messages`}</li>
										{/if}
										{#each busyChoice.busy.tasks as task, i (i)}
											<li>{task}</li>
										{/each}
										{#if busyChoice.busy.wake}
											<li>A scheduled wake-up</li>
										{/if}
									</ul>
								{/if}
								<span class="runner-hint">{sessionHasProject ? 'Commits, pushes, then moves.' : 'Moves when it finishes.'}</span>
							</div>
							<button type="button" class="runner-item primary" role="menuitem" onclick={() => queueChoice(false)}>
								<span class="ctx-chip-label">{busyChoice.busy.queued ? 'Move after the queued messages' : 'Move when this turn ends'}</span>
							</button>
							{#if !busyChoice.busy.running}
								<button type="button" class="runner-item danger" role="menuitem" onclick={() => queueChoice(true)}>
									<span class="ctx-chip-label">Stop them and move now</span>
								</button>
							{/if}
							<button type="button" class="runner-item" role="menuitem" onclick={() => (busyChoice = null)}>
								<span class="ctx-chip-label">Back</span>
							</button>
						{:else}
						{#if runnerActionError}
							<div class="runner-error" role="alert">{runnerActionError}</div>
						{/if}
						{#if runnerMenuLoading}
							<div class="runner-loading">
								<Loader2 size={14} strokeWidth={1.5} class="spin" />
								<span>Loading…</span>
							</div>
						{:else if runnerMenuError}
							<div class="runner-loading" role="alert">{runnerMenuError}</div>
							<button type="button" class="runner-item" role="menuitem" onclick={loadRunnerMenu}>Retry</button>
						{:else if runnerMenu.length === 0}
							<div class="runner-loading" role="status">No other workers available.</div>
						{:else}
							{#each runnerMenu as item (item.id)}
								<button
									type="button"
									class="runner-item"
									class:disabled={item.disabled}
									role="menuitem"
									disabled={item.disabled || runnerActionPending || runnerChecking}
									title={item.title}
									onclick={() => runMenuAction(item)}
								>
									<span class="ctx-chip-label">{item.label}</span>
								</button>
							{/each}
						{/if}
						{/if}
					</div>
				{/if}
			</div>
		{:else}
			<span class="ctx-chip readonly" title="Runs on this device">
				<Monitor size={12} strokeWidth={1.6} aria-hidden="true" />
				<span class="ctx-chip-label">Local</span>
			</span>
		{/if}

		{@render repoChip()}

		<div class="branch-wt-row">
		{#if showBranch}
			<BranchSelector
				{sheetMode}
				projectName={sheetMode ? projectName : null}
				onClickNewBranch={() => (showCreateBranchModal = true)}
			/>
		{:else if readOnlyBranch}
			<span class="ctx-chip readonly" title="Branch: {readOnlyBranch}">
				<GitBranch size={12} strokeWidth={1.6} aria-hidden="true" />
				<span class="ctx-chip-label branch-name">{readOnlyBranch}</span>
			</span>
		{/if}

		{#if $activeSession.project && !badge?.degraded}
			<span
				class="ctx-chip readonly wt-indicator"
				title={isWorktreeSelected ? 'Runs in an isolated git worktree' : 'Runs in the main checkout'}
			>
				<span class="ctx-chip-cb" class:checked={isWorktreeSelected} aria-hidden="true">
					<Check size={9} strokeWidth={2.6} />
				</span>
				<span class="ctx-chip-label">worktree</span>
			</span>
		{/if}
		</div>
	{/if}
	{/if}
</div>

{#if $activeSession.mode !== 'chat' && $activeSession.project}
	<CreateBranchModal
		open={showCreateBranchModal}
		onClose={() => (showCreateBranchModal = false)}
		onCreated={() => git.refreshStatus()}
	/>
{/if}

<style>
	/* The chip row floats above the composer box with a visible gap (mockup
	   `.ctx`). Its dropdowns open upward; the stacking level keeps them below
	   the input area's @/slash autocomplete panels. */
	.context-pill-bar {
		position: relative;
		z-index: 10;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		margin: 0 0 10px 2px;
		min-width: 0;
	}

	.context-pill-bar.sheet-mode {
		z-index: auto;
		width: 100%;
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		align-items: stretch;
		gap: 6px;
		margin: 0;
	}

	.sheet-mode .ctx-sep {
		display: none;
	}

	.runner-badge-wrap {
		position: relative;
		min-width: 0;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
	}

	.sheet-mode .runner-badge-wrap > .ctx-chip.runner {
		width: auto;
		flex: 1 1 auto;
		min-width: 0;
	}

	/* Read-only chips describe the context without inviting a click. */
	.ctx-chip.readonly {
		cursor: default;
	}

	.branch-name {
		max-width: min(180px, 38vw);
	}

	.branch-wt-row {
		display: contents;
	}

	.sheet-mode .branch-wt-row {
		grid-column: 1 / -1;
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
	}

	.sheet-mode .ctx-chip,
	.sheet-mode .runner-badge-wrap {
		width: 100%;
	}

	.sheet-mode .branch-wt-row :global(.branch-selector) {
		flex: 1 1 0;
		min-width: 0;
		width: auto;
	}

	.sheet-mode .branch-wt-row .ctx-chip.readonly:not(.wt-indicator) {
		flex: 1 1 0;
		min-width: 0;
		width: auto;
	}

	.sheet-mode .wt-indicator {
		width: auto;
		flex: 0 0 auto;
	}

	.sheet-mode .ctx-chip {
		height: 44px;
		padding: 0 12px;
		gap: 8px;
		border: 1px solid var(--border-subtle);
		border-radius: var(--composer-control-radius, 12px);
		background: var(--composer-control-bg, color-mix(in oklch, var(--muted) 55%, var(--card)));
		font-size: 0.8rem;
	}

	.sheet-mode .runner-dropdown {
		position: static;
		width: 100%;
		max-width: none;
		margin-top: 6px;
		max-height: 180px;
		touch-action: pan-y;
	}

	.runner-dropdown {
		position: absolute;
		bottom: calc(100% + 6px);
		left: 0;
		min-width: 220px;
		max-width: min(320px, calc(100vw - 24px));
		max-height: 320px;
		overflow-y: auto;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
		box-shadow: var(--shadow-l);
		padding: 4px;
		z-index: 100;
	}

	.runner-loading {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 10px;
		color: var(--muted-foreground);
		font-size: 0.75rem;
	}

	.runner-error {
		padding: 8px 10px;
		color: var(--destructive);
		font-size: 0.75rem;
		line-height: 1.35;
		overflow-wrap: anywhere;
		border-bottom: 1px solid var(--border);
	}

	.runner-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 7px 10px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.58);
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		text-align: left;
	}

	.runner-item:hover:not(:disabled) {
		background: var(--hover-overlay);
	}

	.runner-item.primary {
		color: var(--primary);
		font-weight: 500;
	}

	.runner-item.danger {
		color: var(--destructive);
	}

	.runner-busy {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 8px 10px 6px;
		font-size: 0.75rem;
		line-height: 1.35;
		border-bottom: 1px solid var(--border);
		margin-bottom: 4px;
	}

	.runner-busy strong {
		color: var(--foreground);
		font-weight: 600;
	}

	.runner-busy-tasks {
		margin: 0;
		padding: 0;
		list-style: none;
		color: var(--foreground);
		overflow-wrap: anywhere;
	}

	.runner-hint {
		color: var(--muted-foreground);
	}

	/* Cancels a queued move; sits on the busy chip's trailing edge. */
	.queued-cancel {
		display: inline-grid;
		place-items: center;
		width: 22px;
		height: 22px;
		margin-left: 2px;
		vertical-align: middle;
		border: none;
		border-radius: 999px;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 70%, transparent);
		cursor: pointer;
	}

	.queued-cancel:hover {
		color: var(--foreground);
		background: var(--hover-overlay, var(--muted));
	}

	.queued-cancel:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	.runner-item.disabled,
	.runner-item:disabled {
		opacity: 0.45;
		cursor: default;
	}

	:global(.spin) {
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	/* Remote-placed session: a primary tint so the runner chip is
	   distinguishable from the local one. */
	.ctx-chip.runner {
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, var(--muted));
	}

	.ctx-chip.runner :global(svg) {
		color: var(--primary);
	}

	/* A move/continue in flight: the chip carries the progress label and
	   cannot be clicked again until the request settles. */
	.ctx-chip.runner.busy {
		cursor: progress;
		color: var(--foreground);
	}

	/* Brief confirmation once the session runs on the new worker. Theme
	   tokens only: no preset defines --success, so it stayed the app's green
	   under every theme (#1809). The tick carries the meaning. */
	.ctx-chip.runner.done {
		color: var(--foreground);
		background: color-mix(in oklch, var(--primary) 18%, var(--muted));
	}

	/* Long device names in the progress label must not push the row wider
	   than a phone screen. */
	.ctx-chip.runner .ctx-chip-label {
		max-width: min(240px, 60vw);
	}

	/* V2 Q7 degraded session: read-only, no runner online. Warning accent so it
	   reads as an advisory (informational — there is no adopt/action here). */
	.ctx-chip.degraded {
		color: var(--warning);
		background: color-mix(in oklch, var(--warning) 12%, var(--muted));
	}

	.ctx-chip.degraded :global(svg) {
		color: var(--warning);
	}

	/* The tick inside the read-only worktree indicator must beat the generic
	   muted svg colour on chips. */
	.wt-indicator :global(.ctx-chip-cb svg) {
		color: var(--primary-foreground);
	}
</style>

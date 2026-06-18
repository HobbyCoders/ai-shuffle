<script lang="ts">
	/**
	 * MainPanel - Content area that renders the active view
	 *
	 * Routes between ChatView, SettingsView, and other feature pages
	 * based on the layout store's activeView state.
	 */
	import { untrack } from 'svelte';
	import { activeView, sidebarCollapsed } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { projects } from '$lib/stores/app-data.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { git, gitStatus, branches as branchStore } from '$lib/stores/git.svelte';
	import { githubAuthenticated } from '$lib/stores/auth.svelte';
	import { githubRepoInfo } from '$lib/stores/github.svelte';
	import { gitControlsStreaming } from '$lib/stores/derived/active-streaming.svelte';
	import { getPullRequests } from '$lib/api/github';
	import { exportSession } from '$lib/api/sessions';
	import { toast } from '$lib/stores/toast';
	import { Download } from 'lucide-svelte';
	import { gitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';
	import ChatView from './ChatView.svelte';
	import PageHeader from './PageHeader.svelte';
	import GitHeaderActions from '$lib/components/git/GitHeaderActions.svelte';
	import ProfileSelector from '$lib/components/chat/ProfileSelector.svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import InitRepoModal from '$lib/components/git/InitRepoModal.svelte';
	import ChangedFilesPanel from '$lib/components/git/ChangedFilesPanel.svelte';
	import DiffViewer from '$lib/components/git/DiffViewer.svelte';

	// Feature page components
	import SettingsCard from '$lib/components/deck/cards/SettingsCard.svelte';
	import ProfileCard from '$lib/components/deck/cards/ProfileCard.svelte';
	import SubagentCard from '$lib/components/deck/cards/SubagentCard.svelte';
	import ProjectCard from '$lib/components/deck/cards/ProjectCard.svelte';
	import FileBrowserCard from '$lib/components/deck/cards/FileBrowserCard.svelte';
	import PluginManagerCard from '$lib/components/deck/cards/PluginManagerCard.svelte';
	import McpManagerCard from '$lib/components/deck/cards/McpManagerCard.svelte';
	import NotesCard from '$lib/components/deck/cards/NotesCard.svelte';
	import HooksCard from '$lib/components/deck/cards/HooksCard.svelte';
	import AutomationsCard from '$lib/components/deck/cards/AutomationsCard.svelte';
	import StatsCard from '$lib/components/deck/cards/StatsCard.svelte';
	import DocsCard from '$lib/components/docs/DocsCard.svelte';

	interface Props {
		onOpenSession?: (sessionId: string) => void;
		onOpenPlugins?: () => void;
		isMobile?: boolean;
	}

	let {
		onOpenSession,
		onOpenPlugins,
		isMobile = false
	}: Props = $props();

	// View type → page title mapping
	const viewTitles: Record<string, string> = {
		'settings': 'Settings',
		'profile': 'Profiles',
		'subagent': 'Subagents',
		'project': 'Projects',
		'file-browser': 'Files',
		'plugins': 'Plugins',
		'mcp': 'MCP Servers',
		'notes': 'Notes',
		'hooks': 'Hooks',
		'stats': 'Stats',
		'docs': 'Documentation',
	};

	// Feature pages that use FeaturePageLayout (provide their own header)
	// These don't need the generic PageHeader
	const embeddedViews = new Set([
		'profile', 'project', 'settings', 'subagent',
		'file-browser', 'plugins', 'mcp', 'notes', 'hooks', 'automations', 'stats', 'docs'
	]);

	// Get the active session state (if in chat view)
	const activeTab = $derived(
		$activeView?.type === 'chat' ? $activeSession : null
	);

	// Header title
	const headerTitle = $derived.by(() => {
		if (!$activeView) return '';
		if ($activeView.type === 'chat') {
			if (activeTab && activeTab.messages.length > 0) {
				return activeTab.title || 'Untitled';
			}
			return 'New Chat';
		}
		return viewTitles[$activeView.type] || '';
	});

	// Project name for header context
	const headerProjectName = $derived(
		activeTab ? $projects.find(p => p.id === activeTab.project)?.name : undefined
	);

	// Worktree branch for header context.
	const activeWorktreeBranch = $derived.by(() => {
		if (!activeTab) return undefined;
		if (activeTab.executionMode !== 'worktree' || activeTab.messages.length === 0) return undefined;
		return activeTab.selectedWorktreeBranch || activeTab.worktreeBranch || undefined;
	});

	// A cleaned-up worktree session keeps the badge visible (read-only) so the user
	// knows which project/branch the session belonged to even after cleanup.
	const isWorktreeBadgeDeleted = $derived(
		!!activeTab?.worktreeDeleted && !!activeWorktreeBranch
	);

	// When GitHub CLI is authenticated, GitHeaderActions normally shows project + branch,
	// so we hide the separate PageHeader badge to avoid redundancy — UNLESS the worktree
	// has been cleaned up, in which case GitHeaderActions is hidden and the read-only
	// badge becomes the only contextual indicator of what the session belonged to.
	const headerProjectNameForBadge = $derived(
		$githubAuthenticated && !isWorktreeBadgeDeleted ? undefined : headerProjectName
	);
	const headerWorktreeBranch = $derived(
		$githubAuthenticated && !isWorktreeBadgeDeleted ? undefined : activeWorktreeBranch
	);

	// =========================================================================
	// Git modal state (retained: branch creation, init repo, diff review)
	// =========================================================================

	let showCreateBranchModal = $state(false);

	// Init repo modal state
	let showInitRepoModal = $state(false);

	// Changed files panel state
	let showChangedFilesPanel = $state(false);
	let reviewMode = $state(false);
	let selectedDiffFile = $state<import('$lib/api/git').FileChangeInfo | null>(null);

	// PR detection for current branch (drives Finish button visibility — read-only)
	let branchPR = $state<{ number: number; title: string; url: string; head_branch: string } | null>(null);

	// Load git repository when active tab's project changes (only when GitHub CLI is authenticated).
	// Extract the project and worktree strings OUTSIDE the effect to avoid tracking the entire
	// activeTab object, which changes on every streaming poll and would re-trigger load needlessly.
	const activeProject = $derived(activeTab?.project ?? null);
	const activeWorktreeId = $derived(
		activeTab?.executionMode === 'worktree' && !activeTab?.worktreeDeleted
			? (activeTab.worktreeId ?? null)
			: null
	);
	const activeSessionId = $derived(activeTab?.sessionId ?? null);

	// Debounce rapid project changes (e.g., clicking through chats quickly)
	// to prevent cascading git loads that break commit/branch buttons.
	let _projectDebounceTimer: ReturnType<typeof setTimeout> | null = null;
	$effect(() => {
		const project = activeProject;
		const worktreeId = activeWorktreeId;
		const authenticated = $githubAuthenticated;
		// Track session ID so the effect re-fires on every tab switch,
		// even when two sessions share the same project/worktree.
		// This ensures git state is always fresh after a session change.
		const _sessionId = activeSessionId;

		if (_projectDebounceTimer) {
			clearTimeout(_projectDebounceTimer);
			_projectDebounceTimer = null;
		}

		if (project && authenticated) {
			// IMMEDIATE: restore cached state so UI updates within the same frame
			const mode = worktreeId ? 'worktree' : 'project';
			git.restoreCacheImmediate(project, worktreeId, mode);

			// DEBOUNCED: network fetch for fresh data
			_projectDebounceTimer = setTimeout(() => {
				_projectDebounceTimer = null;
				if (worktreeId) {
					git.loadForWorktree(project, worktreeId);
				} else {
					git.loadRepository(project);
				}
			}, 150);
		} else {
			git.clearRepository();
		}

		return () => {
			if (_projectDebounceTimer) {
				clearTimeout(_projectDebounceTimer);
				_projectDebounceTimer = null;
			}
		};
	});

	// =========================================================================
	// Adaptive git polling (replaces event-driven three-layer system)
	// =========================================================================
	// Frontend-driven polling scoped to the active tab only:
	// - 2s interval while the agent is actively streaming (or recently hinted)
	// - 5s interval after 30s of inactivity
	// - Paused entirely when the browser tab is hidden
	// - Accelerated by `git_hint` flags piggybacked on tool_result WS chunks

	import { sessionNotifier } from '$lib/stores/session/session-notifier';

	const GIT_POLL_ACTIVE_MS = 2_000;
	const GIT_POLL_IDLE_MS = 5_000;
	const GIT_IDLE_THRESHOLD_MS = 30_000;
	const GIT_HINT_DEBOUNCE_MS = 3_000; // Min time between hint-triggered polls

	let _gitPollTimer: ReturnType<typeof setTimeout> | null = null;
	let _gitPollProject: string | null = null;
	let _gitLastActivity = 0; // timestamp of last git_hint or streaming event
	let _lastHintPoll = 0; // timestamp of last hint-triggered immediate poll

	let _lastIdleDiffStatsPoll = 0;

	function gitPollNow() {
		// Skip polling while a commit/push/PR/pull operation is in progress.
		// These operations make their own API calls; polling concurrently
		// would saturate the browser's connection pool (6 per host) and
		// cause progressive slowdown / eventual UI hang.
		if (git.operationInProgress) return;
		// Stop polling entirely if the worktree was deleted (404 detected).
		// Continued polling would spam 404s and waste browser connections.
		if (git.worktreeGone) return;
		const now = Date.now();
		const isActive = (now - _gitLastActivity) < GIT_IDLE_THRESHOLD_MS;
		git.refreshStatus();
		if (isActive) {
			// Active period — poll diff stats on every cycle (5s).
			git.refreshDiffStats();
			_lastIdleDiffStatsPoll = now;
		} else if (now - _lastIdleDiffStatsPoll >= GIT_POLL_IDLE_MS) {
			// Idle period — still poll diff stats once per idle interval (30s)
			// so stale values don't persist indefinitely after streaming ends.
			git.refreshDiffStats();
			_lastIdleDiffStatsPoll = now;
		}
	}

	function gitPollInterval(): number {
		return (Date.now() - _gitLastActivity < GIT_IDLE_THRESHOLD_MS)
			? GIT_POLL_ACTIVE_MS
			: GIT_POLL_IDLE_MS;
	}

	function scheduleGitPoll() {
		if (_gitPollTimer) clearTimeout(_gitPollTimer);
		if (!_gitPollProject || document.hidden) return;
		_gitPollTimer = setTimeout(() => {
			gitPollNow();
			scheduleGitPoll();
		}, gitPollInterval());
	}

	function startGitPoll(project: string) {
		stopGitPoll();
		_gitPollProject = project;
		// Don't reset _gitLastActivity — keep idle mode unless there's actual
		// git activity (hint events, streaming). This prevents session/tab
		// switches from spuriously triggering 30s of active polling.
		scheduleGitPoll();
	}

	function stopGitPoll() {
		if (_gitPollTimer) {
			clearTimeout(_gitPollTimer);
			_gitPollTimer = null;
		}
		_gitPollProject = null;
	}

	/**
	 * Check for open PRs matching the current branch.
	 * Only called when the branch changes (event-driven), not on a timer.
	 */
	async function checkPullRequests(projectId: string, branch: string | null) {
		// Don't look for PRs to finish when on the default branch — PRs with the default
		// branch as head are fork PRs, not feature branches you'd "finish" locally.
		const defaultBranch = $githubRepoInfo?.default_branch ?? 'main';
		if (branch && branch === defaultBranch) {
			branchPR = null;
			return;
		}
		if (branch && $githubAuthenticated) {
			try {
				const prs = await getPullRequests(projectId, 'open', 10);
				const match = prs.find(pr => pr.head_branch === branch);
				branchPR = match ? { number: match.number, title: match.title, url: match.url, head_branch: match.head_branch } : null;
			} catch {
				branchPR = null;
			}
		} else {
			branchPR = null;
		}
	}

	// Pause/resume git poll when tab visibility changes.
	function handleVisibilityChange() {
		if (document.hidden) {
			stopGitPoll();
		} else if (_gitPollProject === null && activeProject && $githubAuthenticated) {
			startGitPoll(activeProject);
			gitPollNow(); // Immediate refresh on return
		}
	}

	// Main effect: start/stop adaptive poll when project/auth/worktree changes.
	// NOTE: activeSessionId deliberately excluded — session switches within the
	// same project shouldn't restart polling (worktreeId covers tab-switch context).
	$effect(() => {
		const project = activeProject;
		const worktreeId = activeWorktreeId; // Track worktree changes for tab switches
		const authenticated = $githubAuthenticated;

		// Clear stale PR state immediately when any dependency changes.
		branchPR = null;

		stopGitPoll();

		if (project && authenticated && !document.hidden) {
			startGitPoll(project);
		}

		document.addEventListener('visibilitychange', handleVisibilityChange);

		return () => {
			stopGitPoll();
			document.removeEventListener('visibilitychange', handleVisibilityChange);
		};
	});

	// Listen for git_hint on stream_chunk events to accelerate polling.
	// Debounced: at most one immediate poll per GIT_HINT_DEBOUNCE_MS window.
	$effect(() => {
		const unsub = sessionNotifier.onEvent((event) => {
			if (event.event_type === 'stream_chunk' || event.type === 'stream_chunk') {
				const data = (event as Record<string, unknown>).data as Record<string, unknown> | undefined;
				if (data?.git_hint === true) {
					const now = Date.now();
					_gitLastActivity = now;
					// Only fire an immediate poll if enough time has passed since the last one
					if (now - _lastHintPoll >= GIT_HINT_DEBOUNCE_MS) {
						_lastHintPoll = now;
						gitPollNow();
					}
					scheduleGitPoll(); // Reset timer to active interval
				}
			}
		});
		return unsub;
	});

	// PR detection: runs when the branch changes (event-driven via WebSocket state updates).
	// Tracks previous branch to avoid redundant GitHub API calls.
	let _lastCheckedPRKey: string | null = null;
	$effect(() => {
		const branch = $gitStatus?.current_branch ?? null;
		const project = activeProject;
		const authenticated = $githubAuthenticated;
		const prKey = (project && branch) ? `${project}:${branch}` : null;

		if (prKey !== _lastCheckedPRKey && project && authenticated && branch) {
			_lastCheckedPRKey = prKey;
			untrack(() => checkPullRequests(project, branch));
		} else if (!project || !authenticated) {
			_lastCheckedPRKey = null;
			branchPR = null;
		}
	});

	// When streaming ends, do an immediate refresh so buttons update promptly.
	// Uses forceRefresh() to abort any in-flight poll requests whose responses
	// may predate the session's final git operations (race with git_hint polls).
	let _wasStreaming = false;
	$effect(() => {
		const streaming = $gitControlsStreaming;
		if (streaming) {
			_wasStreaming = true;
		} else if (_wasStreaming) {
			_wasStreaming = false;
			if (activeProject) {
				untrack(() => {
					_gitLastActivity = Date.now();
					git.forceRefresh();
					scheduleGitPoll(); // Reset to active interval for post-streaming cool-down
				});
			}
		}
	});

	// =========================================================================
	// Git action prompts (send to chat instead of running git directly)
	// =========================================================================
	// The mutating git buttons (Commit / Push / Pull / Create PR / Finish /
	// Ship It) no longer call the git API. Instead, clicking the main part of a
	// button sends a pre-written prompt to the active chat so the AI agent
	// performs the operation with its own git/gh tools. The split-button chevron
	// opens an editable popover (GitPromptPopover) so the user can tweak the
	// wording before sending. Read-only git state machinery below is unchanged.

	// Upstream tracking state (drives prompt wording + quick-action guards).
	const currentBranchForPrompt = $derived($branchStore.find(b => b.current));
	const needsPull = $derived((currentBranchForPrompt?.behind ?? 0) > 0);
	const isOnDefaultBranch = $derived(
		($gitStatus?.current_branch ?? null) === ($githubRepoInfo?.default_branch ?? 'main')
	);

	/** Append "on branch `<name>`" if a branch is known. */
	function branchSuffix(): string {
		const branch = $gitStatus?.current_branch;
		return branch ? ` (current branch: \`${branch}\`)` : '';
	}

	/** Build the commit prompt, honoring saved next-step + include-unstaged prefs. */
	function buildCommitPrompt(): string {
		const prefs = gitActionPreferences.current;
		const staged = prefs.includeUnstaged
			? 'Stage and commit all current changes (including untracked files)'
			: 'Commit the currently staged changes';
		let prompt = `${staged} with a clear, conventional-commit-style message that summarizes what changed${branchSuffix()}.`;

		// Honor the saved "next step" preference, but guard against invalid states.
		let nextStep = prefs.nextStep;
		if (needsPull) nextStep = 'commit';
		else if (nextStep === 'pr' && isOnDefaultBranch) nextStep = 'push';

		if (nextStep === 'push') {
			prompt += ' Then push the branch to its remote.';
		} else if (nextStep === 'pr') {
			prompt += ' Then push the branch and open a pull request into the default branch with a descriptive title and summary.';
		}
		return prompt;
	}

	/** Build the push prompt. */
	function buildPushPrompt(): string {
		return `Push the current branch to its remote${branchSuffix()}.`;
	}

	/** Build the pull prompt, honoring saved pull strategy + auto-stash. */
	function buildPullPrompt(): string {
		const prefs = gitActionPreferences.current;
		const strategy = prefs.pullStrategy === 'rebase' ? 'rebasing onto' : 'merging';
		const stash = prefs.autoStash
			? ' If there are uncommitted local changes, stash them first and restore them afterward.'
			: '';
		return `Pull the latest changes from the remote for the current branch${branchSuffix()}, ${strategy} the upstream changes into your local work.${stash} If there are conflicts, stop and tell me which files conflict.`;
	}

	/** Build the create-PR prompt. */
	function buildCreatePRPrompt(): string {
		return `Push the current branch${branchSuffix()} and open a pull request into the default branch with a descriptive title and a summary of what changed.`;
	}

	/** Build the finish/merge prompt, honoring saved merge method + cleanup. */
	function buildFinishPrompt(): string {
		const prefs = gitActionPreferences.current;
		const pr = branchPR ? `pull request #${branchPR.number}` : 'the open pull request for this branch';
		const method = prefs.mergeMethod === 'squash'
			? 'squash-merge'
			: prefs.mergeMethod === 'rebase'
				? 'rebase-merge'
				: 'merge';
		const cleanup = prefs.cleanupAfterMerge
			? ' After it merges, delete the branch (and clean up the worktree if this is a worktree session).'
			: '';
		return `${method.charAt(0).toUpperCase() + method.slice(1)} ${pr} into the default branch.${cleanup}`;
	}

	/** Build the Ship It pipeline prompt. */
	function buildShipItPrompt(): string {
		const prefs = gitActionPreferences.current;
		const method = prefs.mergeMethod === 'squash'
			? 'squash-merge'
			: prefs.mergeMethod === 'rebase'
				? 'rebase-merge'
				: 'merge';
		const cleanup = prefs.cleanupAfterMerge
			? ', then delete the branch and clean up the worktree if applicable'
			: '';
		const prStep = branchPR
			? `then ${method} the existing pull request #${branchPR.number} into the default branch`
			: `open a pull request into the default branch with a descriptive title and summary, then ${method} it`;
		return `Ship this branch end to end${branchSuffix()}: commit all current changes with a clear conventional-commit message, push the branch, ${prStep}${cleanup}. If anything fails along the way, stop and tell me what went wrong.`;
	}

	/**
	 * Send a canned git-action prompt to the active chat session.
	 * The buttons are disabled while streaming, so sending here won't interrupt.
	 */
	async function sendGitPrompt(prompt: string) {
		const text = prompt.trim();
		if (!text) return;
		try {
			await activeSession.sendMessage(text);
		} catch (err) {
			console.error('[MainPanel] Failed to send git action prompt', err);
		}
	}

	// --- Quick-action handlers: main button click sends the prompt immediately ---
	function handleQuickCommit() { sendGitPrompt(buildCommitPrompt()); }
	function handlePush() { sendGitPrompt(buildPushPrompt()); }
	function handleQuickPull() { sendGitPrompt(buildPullPrompt()); }
	function handleQuickCreatePR() { sendGitPrompt(buildCreatePRPrompt()); }
	function handleQuickFinish() { sendGitPrompt(buildFinishPrompt()); }
	function handleShipIt() { sendGitPrompt(buildShipItPrompt()); }

	// Download the raw Claude session history (.jsonl) for the active chat.
	let downloadingJsonl = $state(false);
	async function handleDownloadJsonl() {
		const sessionId = activeTab?.sessionId;
		if (!sessionId || downloadingJsonl) return;
		downloadingJsonl = true;
		try {
			await exportSession(sessionId, 'jsonl');
		} catch (err) {
			const detail = (err as { detail?: string })?.detail;
			toast.push(detail || 'Failed to download chat history', { tone: 'error' });
		} finally {
			downloadingJsonl = false;
		}
	}
</script>

<div class="main-panel">
	{#if $activeView}
		<!-- Only show PageHeader for chat views and non-embedded feature pages -->
		{#if $activeView.type === 'chat' || !embeddedViews.has($activeView.type)}
			<PageHeader
				title={headerTitle}
				projectName={headerProjectNameForBadge}
				worktreeBranch={headerWorktreeBranch}
				worktreeDeleted={isWorktreeBadgeDeleted}
				showSidebarToggle={$sidebarCollapsed}
				{isMobile}
				hideGlobalStt={$activeView?.type === 'chat'}
			>
				{#snippet profile()}
					{#if $activeView?.type === 'chat' && activeTab}
						<ProfileSelector />
					{/if}
				{/snippet}
				{#snippet leftActions()}
					{#if $activeView?.type === 'chat' && activeTab?.sessionId && activeTab.messages.length > 0}
						<button
							class="download-jsonl-btn"
							onclick={handleDownloadJsonl}
							disabled={downloadingJsonl}
							title="Download chat history (.jsonl)"
							aria-label="Download chat history as JSONL"
						>
							<Download size={16} strokeWidth={1.5} />
						</button>
					{/if}
				{/snippet}
				{#snippet actions()}
					{#if $activeView?.type === 'chat' && activeTab?.project && $githubAuthenticated && !activeTab?.worktreeDeleted}
						<GitHeaderActions
							projectId={activeTab.project}
							isStreaming={$gitControlsStreaming}
							isGitRepo={$gitStatus?.is_git_repo !== false}
							projectName={headerProjectName}
							worktreeBranch={activeWorktreeBranch}
							openPR={branchPR}
							onClickCommit={handleQuickCommit}
							onClickCreatePR={handleQuickCreatePR}
							onClickFinish={handleQuickFinish}
							onClickNewBranch={() => showCreateBranchModal = true}
							onClickPush={handlePush}
							onClickPull={handleQuickPull}
							onClickInitRepo={() => showInitRepoModal = true}
							onClickShipIt={handleShipIt}
							onSendPrompt={sendGitPrompt}
							commitPrompt={buildCommitPrompt()}
							createPRPrompt={buildCreatePRPrompt()}
							finishPrompt={buildFinishPrompt()}
							pullPrompt={buildPullPrompt()}
							onOpenDiffReview={() => { showChangedFilesPanel = true; reviewMode = true; git.refreshChangedFiles(); }}
						/>
					{/if}
				{/snippet}
			</PageHeader>
		{/if}
		{#if $activeView.type === 'chat'}
			<ChatView />
		{:else if $activeView.type === 'profile'}
			<!-- Embedded: uses FeaturePageLayout with its own header -->
			<div class="feature-panel">
				<ProfileCard />
			</div>
		{:else if $activeView.type === 'project'}
			<!-- Embedded: uses FeaturePageLayout with its own header -->
			<div class="feature-panel">
				<ProjectCard />
			</div>
		{:else if $activeView.type === 'settings'}
			<div class="feature-panel">
				<SettingsCard />
			</div>
		{:else if $activeView.type === 'subagent'}
			<div class="feature-panel">
				<SubagentCard />
			</div>
		{:else if $activeView.type === 'file-browser'}
			<div class="feature-panel">
				<FileBrowserCard />
			</div>
		{:else if $activeView.type === 'plugins'}
			<div class="feature-panel">
				<PluginManagerCard />
			</div>
		{:else if $activeView.type === 'mcp'}
			<div class="feature-panel">
				<McpManagerCard />
			</div>
		{:else if $activeView.type === 'notes'}
			<div class="feature-panel">
				<NotesCard />
			</div>
		{:else if $activeView.type === 'hooks'}
			<div class="feature-panel">
				<HooksCard />
			</div>
		{:else if $activeView.type === 'automations'}
			<div class="feature-panel">
				<AutomationsCard />
			</div>
		{:else if $activeView.type === 'stats'}
			<div class="feature-panel">
				<StatsCard
					onSessionClick={onOpenSession || (() => {})}
				/>
			</div>
		{:else if $activeView.type === 'docs'}
			<div class="feature-panel">
				<DocsCard mobile={isMobile} />
			</div>
		{/if}
	{:else}
		<!-- No active view - show a minimal placeholder -->
		<div class="no-view">
			<p>Select a session or create a new one</p>
		</div>
	{/if}
</div>

<!-- Git modals (rendered outside main-panel for proper z-index stacking) -->
{#if activeTab?.project && $githubAuthenticated}
	<CreateBranchModal
		open={showCreateBranchModal}
		projectId={activeTab.project}
		defaultName={activeTab.title || ''}
		onClose={() => showCreateBranchModal = false}
		onCreated={() => git.refreshStatus()}
	/>

	<InitRepoModal
		open={showInitRepoModal}
		projectId={activeTab.project}
		projectName={headerProjectName ?? ''}
		onClose={() => showInitRepoModal = false}
		onInitialized={() => {
			if (activeTab?.project) {
				git.loadRepository(activeTab.project, true);
			}
		}}
	/>

	<ChangedFilesPanel
		projectId={activeTab.project}
		open={showChangedFilesPanel}
		{reviewMode}
		onClose={() => { showChangedFilesPanel = false; reviewMode = false; selectedDiffFile = null; }}
		onViewDiff={(file) => { selectedDiffFile = file; }}
	/>

	{#if selectedDiffFile && !reviewMode}
		<DiffViewer
			projectId={activeTab.project}
			file={selectedDiffFile}
			onClose={() => { selectedDiffFile = null; }}
		/>
	{/if}
{/if}

<style>
	.main-panel {
		flex: 1;
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		min-width: 0;
		overflow: hidden;
		background: var(--background);
	}

	/* Download chat history button — matches PageHeader's .header-btn styling */
	.download-jsonl-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		flex-shrink: 0;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background-color 150ms ease, color 150ms ease;
		-webkit-tap-highlight-color: transparent;
	}

	.download-jsonl-btn:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.download-jsonl-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.download-jsonl-btn:disabled {
		opacity: 0.5;
		cursor: default;
	}

	@media (max-width: 639px) {
		.download-jsonl-btn {
			width: 36px;
			height: 36px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.download-jsonl-btn {
			transition: none;
		}
	}

	.feature-panel {
		flex: 1;
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
	}

	/* Feature panels rendered as cards need to fill the space */
	.feature-panel :global(> *) {
		flex: 1;
		height: 100%;
	}

	.no-view {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.no-view p {
		color: var(--muted-foreground);
		font-size: 0.875rem;
	}
</style>

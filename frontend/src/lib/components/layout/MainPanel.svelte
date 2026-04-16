<script lang="ts">
	/**
	 * MainPanel - Content area that renders the active view
	 *
	 * Routes between ChatView, SettingsView, and other feature pages
	 * based on the layout store's activeView state.
	 */
	import { untrack } from 'svelte';
	import { activeView, sidebarCollapsed } from '$lib/stores/layout';
	import type { ViewType } from '$lib/stores/layout';
	import { projects } from '$lib/stores/app-data';
	import { activeSession } from '$lib/stores/active-session';
	import { git, gitStatus, branches as branchStore, diffStats } from '$lib/stores/git';
	import { githubAuthenticated } from '$lib/stores/auth';
	import { githubRepoInfo } from '$lib/stores/github';
	import { gitControlsStreaming } from '$lib/stores/derived/active-streaming';
	import { createPullRequest, getPullRequests, mergePullRequest } from '$lib/api/github';
	import { generateCommitMessage, generatePRDescription, deleteWorktree } from '$lib/api/git';
	import { gitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';
	import ChatView from './ChatView.svelte';
	import PageHeader from './PageHeader.svelte';
	import GitHeaderActions from '$lib/components/git/GitHeaderActions.svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import CommitModal from '$lib/components/git/CommitModal.svelte';
	import CommitProgressModal from '$lib/components/git/CommitProgressModal.svelte';
	import FinishModal from '$lib/components/git/FinishModal.svelte';
	import PullModal from '$lib/components/git/PullModal.svelte';
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
	// When GitHub CLI is authenticated, GitHeaderActions shows the project name in its combined badge,
	// so we hide the separate PageHeader badge to avoid redundancy.
	const headerProjectNameForBadge = $derived(
		$githubAuthenticated ? undefined : headerProjectName
	);

	// Worktree branch for header context.
	// When GitHub CLI is authenticated, the GitHeaderActions branch selector shows this instead,
	// so we hide the separate badge to avoid redundancy.
	const activeWorktreeBranch = $derived.by(() => {
		if (!activeTab) return undefined;
		if (activeTab.executionMode !== 'worktree' || activeTab.messages.length === 0) return undefined;
		return activeTab.selectedWorktreeBranch || activeTab.worktreeBranch || undefined;
	});
	const headerWorktreeBranch = $derived(
		$githubAuthenticated ? undefined : activeWorktreeBranch
	);

	// =========================================================================
	// Git modal state
	// =========================================================================

	let showCreateBranchModal = $state(false);
	let showCommitModal = $state(false);
	let showCommitModalMode = $state<'commit' | 'pr-only'>('commit');
	let showProgressModal = $state(false);
	let progressAction = $state<'commit' | 'push' | 'pr' | 'finish' | 'pull' | 'pipeline'>('commit');
	let progressMode = $state<'commit' | 'pr-only' | 'finish' | 'pull' | 'push' | 'pipeline'>('commit');
	let progressStep = $state(1);
	let progressCompleted = $state(false);
	let progressError = $state<string | null>(null);
	let progressIsGenerating = $state(false);
	let progressHasCleanup = $state(false);

	// Finish modal state
	let showFinishModal = $state(false);

	// Pull modal state
	let showPullModal = $state(false);

	// Init repo modal state
	let showInitRepoModal = $state(false);

	// Changed files panel state
	let showChangedFilesPanel = $state(false);
	let reviewMode = $state(false);
	let selectedDiffFile = $state<import('$lib/api/git').FileChangeInfo | null>(null);

	// PR detection for current branch
	let branchPR = $state<{ number: number; title: string; url: string; head_branch: string } | null>(null);
	let _pendingPRModal = $state(false);
	let _finishedWorktreeCleanup = false;

	const isWorktreeMode = $derived(
		activeTab?.executionMode === 'worktree' && !!activeTab?.worktreeId
	);

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
	 * Suspend the git poll timer entirely for the duration of a git operation
	 * (commit/push/PR/pull). Unlike the `operationInProgress` guard (which
	 * lets the timer fire and skip), this cancels the timer so no HTTP
	 * connections are attempted at all, leaving the full browser connection
	 * pool available for the operation itself.
	 */
	let _gitPollPausedProject: string | null = null;

	function pauseGitPoll() {
		if (_gitPollTimer) {
			clearTimeout(_gitPollTimer);
			_gitPollTimer = null;
		}
		// Remember the project so we can resume after the operation
		_gitPollPausedProject = _gitPollProject;
	}

	function resumeGitPoll() {
		const project = _gitPollPausedProject;
		_gitPollPausedProject = null;
		if (project && _gitPollProject) {
			// Restart the timer — the operation's own refreshes already
			// fetched fresh data, so schedule the next poll at idle interval.
			scheduleGitPoll();
		}
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

	/**
	 * Handle the commit workflow (commit → push → PR)
	 */
	async function handleStartCommit(
		action: 'commit' | 'push' | 'pr',
		message: string,
		includeUnstaged: boolean
	) {
		const needsGeneration = !message.trim();
		progressAction = action;
		progressMode = 'commit';
		progressStep = 1;
		progressCompleted = false;
		progressError = null;
		progressIsGenerating = needsGeneration;
		showProgressModal = true;

		const projectId = activeTab?.project;
		if (!projectId) {
			progressError = 'No project selected';
			return;
		}

		const worktreeId = activeTab?.worktreeId ?? undefined;

		// Pause git polling while the operation runs to prevent
		// HTTP request accumulation that saturates the connection pool.
		// Both cancels the poll timer AND aborts in-flight poll fetches.
		pauseGitPoll();
		git.setOperationInProgress(true);
		try {
			let finalMessage = message;

			// Generate commit message with AI if empty
			if (needsGeneration) {
				try {
					const generated = await generateCommitMessage(projectId, {
						include_unstaged: includeUnstaged
					}, worktreeId);
					finalMessage = generated.message;
				} catch (e: any) {
					progressIsGenerating = false;
					progressError = e?.detail || e?.message || 'Failed to generate commit message';
					return;
				}
				progressIsGenerating = false;
				progressStep = 2;
			}

			// Commit step
			const commitStep = needsGeneration ? 2 : 1;
			progressStep = commitStep;
			await git.commitChanges(finalMessage, includeUnstaged);

			if (action === 'push' || action === 'pr') {
				// Push step
				progressStep = commitStep + 1;
				const branch = $gitStatus?.current_branch ?? undefined;
				await git.pushCommits(branch, true);

				if (action === 'pr') {
					_pendingPRModal = true;
				}
			}

			progressCompleted = true;
		} catch (e: any) {
			_pendingPRModal = false;
			progressError = e?.detail || e?.message || 'An error occurred';
		} finally {
			git.setOperationInProgress(false);
			resumeGitPoll();
		}
	}

	/**
	 * Handle the PR-only workflow (generate → push → create PR)
	 */
	async function handleCreatePR(title: string, body: string) {
		const needsGeneration = !title.trim();
		progressAction = 'pr';
		progressMode = 'pr-only';
		progressStep = 1;
		progressCompleted = false;
		progressError = null;
		progressIsGenerating = needsGeneration;
		showProgressModal = true;

		const projectId = activeTab?.project;
		if (!projectId) {
			progressError = 'No project selected';
			return;
		}

		const worktreeId = activeTab?.worktreeId ?? undefined;

		pauseGitPoll();
		git.setOperationInProgress(true);
		try {
			let prTitle = title;
			let prBody = body;

			// Generate PR description with AI if title is empty
			if (needsGeneration) {
				try {
					const generated = await generatePRDescription(projectId, {
						base_branch: 'main'
					}, worktreeId);
					prTitle = generated.title;
					prBody = generated.body;
				} catch {
					prTitle = $gitStatus?.current_branch || 'Update';
					prBody = '';
				}
				progressIsGenerating = false;
			}

			// Push step
			const pushStep = needsGeneration ? 2 : 1;
			progressStep = pushStep;
			const branch = $gitStatus?.current_branch ?? undefined;
			await git.pushCommits(branch, true);

			// Create PR step
			progressStep = pushStep + 1;
			if (branch) {
				await createPullRequest(projectId, {
					title: prTitle,
					body: prBody,
					head: branch,
					base: 'main'
				});
				// Detect the new PR so UI transitions to Finish state
				await checkPullRequests(projectId, branch);
			}

			progressCompleted = true;
		} catch (e: any) {
			progressError = e?.detail || e?.message || 'An error occurred';
		} finally {
			git.setOperationInProgress(false);
			resumeGitPoll();
		}
	}

	/**
	 * Handle the Finish workflow (merge PR → optional cleanup)
	 */
	async function handleFinish(mergeMethod: 'squash' | 'merge' | 'rebase', cleanup: boolean) {
		if (!branchPR) return;

		const projectId = activeTab?.project;
		if (!projectId) {
			progressError = 'No project selected';
			return;
		}

		// Safety check: verify the PR's branch matches the current branch
		const currentBranch = $gitStatus?.current_branch;
		if (currentBranch && branchPR.head_branch !== currentBranch) {
			progressError = `PR #${branchPR.number} is for branch "${branchPR.head_branch}" but current branch is "${currentBranch}"`;
			showFinishModal = false;
			untrack(() => git.refreshDiffStats());
			return;
		}

		progressAction = 'finish';
		progressMode = 'finish';
		progressStep = 1;
		progressCompleted = false;
		progressError = null;
		progressIsGenerating = false;
		progressHasCleanup = cleanup;
		showProgressModal = true;

		const prNumber = branchPR.number;
		const worktreeId = activeTab?.worktreeId ?? null;

		// Track for post-cleanup worktree state update
		_finishedWorktreeCleanup = !!(cleanup && isWorktreeMode && worktreeId);

		pauseGitPoll();
		git.setOperationInProgress(true);
		try {
			// Step 1: Merge pull request
			await mergePullRequest(projectId, prNumber, mergeMethod);

			if (cleanup) {
				// Step 2: Cleanup
				progressStep = 2;

				if (isWorktreeMode && worktreeId) {
					// Delete worktree and branch
					await deleteWorktree(projectId, worktreeId, false);
				} else {
					// Non-worktree: checkout default branch, then delete feature branch
					const currentBranch = $gitStatus?.current_branch;
					const defaultBranch = 'main';

					if (currentBranch && currentBranch !== defaultBranch) {
						await git.checkout(defaultBranch);
						await git.deleteBranch(currentBranch);
					}
				}
			}

			progressCompleted = true;
			branchPR = null;

			// Only refresh git state if worktree wasn't deleted — polling a
			// deleted worktree produces 404 spam that saturates connections.
			if (!(cleanup && isWorktreeMode && worktreeId)) {
				await git.refreshStatus();
				await git.refreshDiffStats();
			}
		} catch (e: any) {
			progressError = e?.detail || e?.message || 'An error occurred';
		} finally {
			git.setOperationInProgress(false);
			// Don't resume polling if the worktree was deleted — it will
			// just fire 404s until the user closes the modal and
			// markWorktreeDeleted() clears the worktree context.
			if (!(cleanup && isWorktreeMode && worktreeId)) {
				resumeGitPoll();
			}
		}
	}

	// Pull modal derived state
	const commitsBehindForPull = $derived.by(() => {
		const current = $branchStore.find(b => b.current);
		return current?.behind ?? 0;
	});
	const commitsAheadForPull = $derived.by(() => {
		const current = $branchStore.find(b => b.current);
		return current?.ahead ?? 0;
	});
	const isDivergedForPull = $derived(commitsBehindForPull > 0 && commitsAheadForPull > 0);
	// Show pull when behind upstream, including diverged state (pull is the safe default)
	const needsPull = $derived(commitsBehindForPull > 0);
	const pullHasChanges = $derived(
		($diffStats?.additions ?? 0) > 0 ||
		($diffStats?.deletions ?? 0) > 0 ||
		($diffStats?.files_changed ?? 0) > 0
	);

	async function handlePush(force: boolean = false) {
		progressAction = 'push';
		progressMode = 'push';
		progressStep = 1;
		progressCompleted = false;
		progressError = null;
		progressIsGenerating = false;
		progressHasCleanup = false;
		showProgressModal = true;

		pauseGitPoll();
		git.setOperationInProgress(true);
		try {
			const branch = $gitStatus?.current_branch ?? undefined;
			await git.pushCommits(branch, true, force);
			progressCompleted = true;
		} catch (e: any) {
			progressError = e?.detail || e?.message || 'Failed to push';
		} finally {
			git.setOperationInProgress(false);
			resumeGitPoll();
		}
	}

	// Derived state for quick-action guards
	const isOnDefaultBranch = $derived(
		($gitStatus?.current_branch ?? null) === ($githubRepoInfo?.default_branch ?? 'main')
	);

	// =========================================================================
	// Quick-action handlers (use saved preferences, bypass modals)
	// =========================================================================

	/** Quick commit: uses saved preferences, empty message triggers AI generation */
	function handleQuickCommit() {
		const prefs = gitActionPreferences.current;
		let action = prefs.nextStep;
		// Guard: can't push/pr when behind upstream
		if (needsPull) action = 'commit';
		// Guard: can't create PR from default branch
		else if (action === 'pr' && isOnDefaultBranch) action = 'push';
		handleStartCommit(action, '', prefs.includeUnstaged);
	}

	/** Quick create PR: empty title/body triggers AI generation */
	function handleQuickCreatePR() {
		handleCreatePR('', '');
	}

	/** Quick pull: uses saved strategy and autoStash */
	function handleQuickPull() {
		const prefs = gitActionPreferences.current;
		handlePull(prefs.pullStrategy, prefs.autoStash && pullHasChanges);
	}

	/** Quick finish: uses saved mergeMethod and cleanup */
	function handleQuickFinish() {
		const prefs = gitActionPreferences.current;
		handleFinish(prefs.mergeMethod, prefs.cleanupAfterMerge);
	}

	// =========================================================================
	// Ship It pipeline (commit → push → PR → merge → cleanup)
	// =========================================================================

	/** Pipeline step descriptors, built dynamically based on current state */
	let pipelineSteps = $state<{ id: string; label: string; sublabel?: string }[]>([]);

	/**
	 * Ship It — one-click pipeline using all saved preferences.
	 *
	 * Dynamically determines which steps are needed:
	 * - Skip commit if no uncommitted changes
	 * - Skip push if already up-to-date with remote
	 * - Skip PR creation if PR already exists
	 * - Always merge + cleanup (per user prefs)
	 */
	async function handleShipIt() {
		const projectId = activeTab?.project;
		if (!projectId) return;

		const prefs = gitActionPreferences.current;
		const worktreeId = activeTab?.worktreeId ?? undefined;
		const branch = $gitStatus?.current_branch ?? undefined;
		const defaultBranch = $githubRepoInfo?.default_branch ?? 'main';

		// Build the pipeline steps based on current state
		const steps: { id: string; label: string; sublabel?: string }[] = [];

		const hasUncommittedChanges = ($diffStats?.files_changed ?? 0) > 0 ||
			($diffStats?.additions ?? 0) > 0 ||
			($diffStats?.deletions ?? 0) > 0;

		if (hasUncommittedChanges) {
			steps.push({ id: 'generate-commit', label: 'Generating commit message' });
			steps.push({ id: 'commit', label: 'Committing changes' });
		}

		steps.push({ id: 'push', label: branch ? `Pushing to ${branch}` : 'Pushing to remote' });

		// Check if PR already exists for this branch
		const existingPR = branchPR;
		if (!existingPR) {
			steps.push({ id: 'generate-pr', label: 'Generating PR description' });
			steps.push({ id: 'create-pr', label: 'Creating pull request' });
		}

		steps.push({ id: 'merge', label: `Merging pull request (${prefs.mergeMethod})` });

		if (prefs.cleanupAfterMerge) {
			steps.push({
				id: 'cleanup',
				label: isWorktreeMode ? 'Cleaning up worktree and branch' : 'Cleaning up branch'
			});
		}

		pipelineSteps = steps;

		// Initialize progress modal in pipeline mode
		progressAction = 'pipeline';
		progressMode = 'pipeline';
		progressStep = 1;
		progressCompleted = false;
		progressError = null;
		progressIsGenerating = false;
		progressHasCleanup = prefs.cleanupAfterMerge;
		showProgressModal = true;

		// Track for post-cleanup worktree state update
		_finishedWorktreeCleanup = !!(prefs.cleanupAfterMerge && isWorktreeMode && worktreeId);

		let currentStepIndex = 0;

		function advanceStep() {
			currentStepIndex++;
			progressStep = currentStepIndex;
		}

		pauseGitPoll();
		git.setOperationInProgress(true);

		try {
			// --- Step: Commit (if needed) ---
			if (hasUncommittedChanges) {
				// Generate commit message
				advanceStep(); // generate-commit
				let commitMessage: string;
				try {
					const generated = await generateCommitMessage(projectId, {
						include_unstaged: prefs.includeUnstaged
					}, worktreeId);
					commitMessage = generated.message;
				} catch (e: any) {
					progressError = e?.detail || e?.message || 'Failed to generate commit message';
					return;
				}

				// Commit
				advanceStep(); // commit
				await git.commitChanges(commitMessage, prefs.includeUnstaged);
			}

			// --- Step: Push ---
			advanceStep(); // push
			await git.pushCommits(branch, true);

			// --- Step: Create PR (if none exists) ---
			let prNumber: number;

			if (!existingPR) {
				// Generate PR description
				advanceStep(); // generate-pr
				let prTitle: string;
				let prBody: string;
				try {
					const generated = await generatePRDescription(projectId, {
						base_branch: defaultBranch
					}, worktreeId);
					prTitle = generated.title;
					prBody = generated.body;
				} catch {
					prTitle = branch || 'Update';
					prBody = '';
				}

				// Create PR
				advanceStep(); // create-pr
				if (branch) {
					const pr = await createPullRequest(projectId, {
						title: prTitle,
						body: prBody,
						head: branch,
						base: defaultBranch
					});
					prNumber = pr.number;
					// Update branchPR state so Finish logic works
					await checkPullRequests(projectId, branch);
				} else {
					progressError = 'No branch detected';
					return;
				}
			} else {
				prNumber = existingPR.number;
			}

			// --- Step: Merge ---
			advanceStep(); // merge
			await mergePullRequest(projectId, prNumber, prefs.mergeMethod);

			// --- Step: Cleanup (if enabled) ---
			if (prefs.cleanupAfterMerge) {
				advanceStep(); // cleanup
				if (isWorktreeMode && worktreeId) {
					await deleteWorktree(projectId, worktreeId, false);
				} else if (branch && branch !== defaultBranch) {
					await git.checkout(defaultBranch);
					await git.deleteBranch(branch);
				}
			}

			progressCompleted = true;
			branchPR = null;

			// Only refresh git state if worktree wasn't deleted — polling a
			// deleted worktree produces 404 spam that saturates connections.
			const worktreeWasDeleted = prefs.cleanupAfterMerge && isWorktreeMode && !!worktreeId;
			if (!worktreeWasDeleted) {
				await git.refreshStatus();
				await git.refreshDiffStats();
			}
		} catch (e: any) {
			progressError = e?.detail || e?.message || 'An error occurred during the pipeline';
		} finally {
			git.setOperationInProgress(false);
			// Don't resume polling if the worktree was deleted — it will
			// just fire 404s until the user closes the modal and
			// markWorktreeDeleted() clears the worktree context.
			const worktreeWasDeleted = prefs.cleanupAfterMerge && isWorktreeMode && !!worktreeId;
			if (!worktreeWasDeleted) {
				resumeGitPoll();
			}
		}
	}

	async function handlePull(strategy: 'merge' | 'rebase', autoStash: boolean = false) {
		progressAction = 'pull';
		progressMode = 'pull';
		progressStep = 1;
		progressCompleted = false;
		progressError = null;
		progressIsGenerating = false;
		progressHasCleanup = false;
		showProgressModal = true;

		const projectId = activeTab?.project;
		if (!projectId) {
			progressError = 'No project selected';
			return;
		}

		pauseGitPoll();
		git.setOperationInProgress(true);
		try {
			const result = await git.pull(strategy, autoStash);

			if (result.has_conflicts) {
				const fileList = result.conflicts.join(', ');
				progressError = `Merge conflicts in ${result.conflicts.length} file(s): ${fileList}. Resolve conflicts manually using your editor or CLI.`;
				return;
			}

			if (!result.success) {
				progressError = result.message || 'Failed to pull';
				return;
			}

			progressCompleted = true;
		} catch (e: any) {
			progressError = e?.detail || e?.message || 'Failed to pull';
		} finally {
			git.setOperationInProgress(false);
			resumeGitPoll();
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
				showSidebarToggle={$sidebarCollapsed}
				{isMobile}
				hideGlobalStt={$activeView?.type === 'chat'}
			>
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
							onClickPush={() => handlePush(false)}
							onClickPull={handleQuickPull}
							onClickInitRepo={() => showInitRepoModal = true}
							onClickShipIt={handleShipIt}
							onOpenCommitModal={() => { showCommitModalMode = 'commit'; showCommitModal = true; }}
							onOpenCreatePRModal={() => { showCommitModalMode = 'pr-only'; showCommitModal = true; }}
							onOpenFinishModal={() => showFinishModal = true}
							onOpenPullModal={() => showPullModal = true}
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

	<CommitModal
		open={showCommitModal}
		projectId={activeTab.project}
		mode={showCommitModalMode}
		defaultBranch={$githubRepoInfo?.default_branch ?? 'main'}
		{needsPull}
		onClose={() => showCommitModal = false}
		onStartCommit={handleStartCommit}
		onCreatePR={handleCreatePR}
	/>

	<FinishModal
		open={showFinishModal}
		pr={branchPR}
		branchName={$gitStatus?.current_branch ?? ''}
		{isWorktreeMode}
		onClose={() => showFinishModal = false}
		onFinish={handleFinish}
	/>

	<PullModal
		open={showPullModal}
		branchName={$gitStatus?.current_branch ?? ''}
		commitsBehind={commitsBehindForPull}
		commitsAhead={commitsAheadForPull}
		hasChanges={pullHasChanges}
		isDiverged={isDivergedForPull}
		onClose={() => showPullModal = false}
		onPull={(strategy, autoStash) => { showPullModal = false; handlePull(strategy, autoStash); }}
		onForcePush={() => { showPullModal = false; handlePush(true); }}
	/>

	<CommitProgressModal
		open={showProgressModal}
		action={progressAction}
		mode={progressMode}
		currentStep={progressStep}
		completed={progressCompleted}
		error={progressError}
		branchName={$gitStatus?.current_branch ?? ''}
		isGenerating={progressIsGenerating}
		hasCleanup={progressHasCleanup}
		{pipelineSteps}
		onClose={() => {
			showProgressModal = false;
			progressHasCleanup = false;
			pipelineSteps = [];
			if (progressCompleted && (progressMode === 'finish' || progressMode === 'pipeline') && _finishedWorktreeCleanup) {
				activeSession.markWorktreeDeleted();
				_finishedWorktreeCleanup = false;
			}
			if (progressCompleted && _pendingPRModal) {
				_pendingPRModal = false;
				showCommitModalMode = 'pr-only';
				showCommitModal = true;
			}
		}}
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

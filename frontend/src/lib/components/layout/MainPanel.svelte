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
	import ChatView from './ChatView.svelte';
	import PageHeader from './PageHeader.svelte';
	import GitHeaderActions from '$lib/components/git/GitHeaderActions.svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import CommitModal from '$lib/components/git/CommitModal.svelte';
	import CommitProgressModal from '$lib/components/git/CommitProgressModal.svelte';
	import FinishModal from '$lib/components/git/FinishModal.svelte';
	import PullModal from '$lib/components/git/PullModal.svelte';
	import InitRepoModal from '$lib/components/git/InitRepoModal.svelte';

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
		'file-browser', 'plugins', 'mcp', 'notes', 'hooks', 'stats', 'docs'
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
	let progressAction = $state<'commit' | 'push' | 'pr' | 'finish' | 'pull'>('commit');
	let progressMode = $state<'commit' | 'pr-only' | 'finish' | 'pull' | 'push'>('commit');
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
		activeTab?.executionMode === 'worktree' ? (activeTab.worktreeId ?? null) : null
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
	// Event-driven git state updates (three-layer system)
	// =========================================================================
	// Git state updates arrive via three mechanisms, all pushing through WebSocket:
	// 1. Git hooks (post-commit, post-checkout, post-merge, post-rewrite) — instant,
	//    bypasses debounce, handles external CLI/IDE git operations.
	// 2. Watchdog file watcher — detects staging, file edits, and other .git/ changes
	//    with 500ms debounce.
	// 3. Programmatic broadcast — API mutation endpoints (commit, push, checkout, etc.)
	//    immediately recompute and broadcast state after operations complete.
	// A 10-second safety-net fallback poll catches any missed events
	// (e.g. working-tree edits made outside the app, which don't touch .git/).

	const GIT_FALLBACK_INTERVAL_MS = 10_000;
	let _gitFallbackTimer: ReturnType<typeof setInterval> | null = null;

	/**
	 * Refresh diff stats via API (used for fallback poll and after streaming).
	 */
	async function refreshDiffStats(projectId: string) {
		git.refreshDiffStats();
	}

	/**
	 * Check for open PRs matching the current branch.
	 * Only called when the branch changes (event-driven), not on a timer.
	 */
	async function checkPullRequests(projectId: string, branch: string | null) {
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

	// Safety-net fallback poll: refreshes diff stats every 10s when tab is visible.
	// Real-time updates come via WebSocket from git hooks, file watcher, and API broadcasts.
	$effect(() => {
		const project = activeProject;
		const worktreeId = activeWorktreeId; // Track worktree changes for tab switches
		const authenticated = $githubAuthenticated;
		const _sessionId = activeSessionId; // Track session changes for tab switches

		// Clear stale PR state immediately when any dependency changes.
		branchPR = null;

		if (_gitFallbackTimer) {
			clearInterval(_gitFallbackTimer);
			_gitFallbackTimer = null;
		}

		if (project && authenticated) {
			// Fallback poll — only when tab is visible
			// (Initial load is handled by git.loadRepository / git.loadForWorktree above)
			_gitFallbackTimer = setInterval(() => {
				if (!document.hidden) {
					refreshDiffStats(project);
				}
			}, GIT_FALLBACK_INTERVAL_MS);
		}

		return () => {
			if (_gitFallbackTimer) {
				clearInterval(_gitFallbackTimer);
				_gitFallbackTimer = null;
			}
		};
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
	let _wasStreaming = false;
	$effect(() => {
		const streaming = $gitControlsStreaming;
		if (streaming) {
			_wasStreaming = true;
		} else if (_wasStreaming) {
			_wasStreaming = false;
			const project = activeProject;
			if (project) {
				git.refreshStatus();
				untrack(() => refreshDiffStats(project));
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
			untrack(() => refreshDiffStats(projectId));
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

			// Refresh git status
			await git.refreshStatus();
			await git.refreshDiffStats();
		} catch (e: any) {
			progressError = e?.detail || e?.message || 'An error occurred';
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
	// Diverged (both ahead and behind) means rebase/amend — don't treat as needing pull
	const needsPull = $derived(commitsBehindForPull > 0 && commitsAheadForPull === 0);
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

		try {
			const branch = $gitStatus?.current_branch ?? undefined;
			await git.pushCommits(branch, true, force);
			progressCompleted = true;
		} catch (e: any) {
			progressError = e?.detail || e?.message || 'Failed to push';
		}
	}

	async function handlePull(strategy: 'merge' | 'rebase') {
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

		try {
			const result = await git.pull(strategy);

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
		}
	}
</script>

<div class="main-panel">
	{#if $activeView}
		<!-- Only show PageHeader for chat views and non-embedded feature pages -->
		{#if $activeView.type === 'chat' || !embeddedViews.has($activeView.type)}
			<PageHeader
				title={headerTitle}
				projectName={headerProjectName}
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
							worktreeBranch={activeWorktreeBranch}
							openPR={branchPR}
							onClickCommit={() => { showCommitModalMode = 'commit'; showCommitModal = true; }}
							onClickCreatePR={() => { showCommitModalMode = 'pr-only'; showCommitModal = true; }}
							onClickFinish={() => showFinishModal = true}
							onClickNewBranch={() => showCreateBranchModal = true}
							onClickPush={() => handlePush(false)}
							onClickForcePush={() => handlePush(true)}
							onClickPull={() => showPullModal = true}
							onClickInitRepo={() => showInitRepoModal = true}
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
				<ProfileCard
					onOpenPlugins={onOpenPlugins || (() => {})}
				/>
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
		hasChanges={pullHasChanges}
		onClose={() => showPullModal = false}
		onPull={(strategy) => { showPullModal = false; handlePull(strategy); }}
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
		onClose={() => {
			showProgressModal = false;
			progressHasCleanup = false;
			if (progressCompleted && progressMode === 'finish' && _finishedWorktreeCleanup) {
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

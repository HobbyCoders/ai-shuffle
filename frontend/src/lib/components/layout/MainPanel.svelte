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
	import type { CardType, DeckCard } from '$lib/components/deck/cards/types';
	import { allTabs, projects, tabs } from '$lib/stores/tabs';
	import { git, gitStatus, branches as branchStore, diffStats } from '$lib/stores/git';
	import { githubAuthenticated } from '$lib/stores/auth';
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

	// Feature page components
	import SettingsCard from '$lib/components/deck/cards/SettingsCard.svelte';
	import UserSettingsCard from '$lib/components/deck/cards/UserSettingsCard.svelte';
	import ProfileCard from '$lib/components/deck/cards/ProfileCard.svelte';
	import SubagentCard from '$lib/components/deck/cards/SubagentCard.svelte';
	import ProjectCard from '$lib/components/deck/cards/ProjectCard.svelte';
	import FileBrowserCard from '$lib/components/deck/cards/FileBrowserCard.svelte';
	import PluginManagerCard from '$lib/components/deck/cards/PluginManagerCard.svelte';
	import McpManagerCard from '$lib/components/deck/cards/McpManagerCard.svelte';
	import NotesCard from '$lib/components/deck/cards/NotesCard.svelte';
	import HooksCard from '$lib/components/deck/cards/HooksCard.svelte';
	import StatsCard from '$lib/components/deck/cards/StatsCard.svelte';
	import ApiUserSessionsCard from '$lib/components/deck/cards/ApiUserSessionsCard.svelte';

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

	// Create a fake card object for feature page components that still expect it
	// This is a compatibility shim until feature pages are fully refactored
	function makeFeatureCard(type: CardType, title: string): DeckCard {
		return {
			id: `panel-${type}`,
			type,
			title,
			x: 0,
			y: 0,
			width: 0,
			height: 0,
			zIndex: 1,
			maximized: true,
			focused: true,
			snappedTo: undefined,
			data: {},
			createdAt: new Date(),
			updatedAt: new Date()
		};
	}

	// No-op handlers for card props that don't apply in panel mode
	const noop = () => {};
	const noopXY = (_x: number, _y: number) => {};

	// View type → page title mapping
	const viewTitles: Record<string, string> = {
		'settings': 'Settings',
		'user-settings': 'My Settings',
		'profile': 'Profiles',
		'subagent': 'Subagents',
		'project': 'Projects',
		'file-browser': 'Files',
		'plugins': 'Plugins',
		'mcp': 'MCP Servers',
		'notes': 'Notes',
		'hooks': 'Hooks',
		'stats': 'Stats',
		'api-user-sessions': 'API User Sessions'
	};

	// Feature pages that use FeaturePageLayout (provide their own header)
	// These don't need the generic PageHeader
	const embeddedViews = new Set([
		'profile', 'project', 'settings', 'user-settings', 'subagent',
		'file-browser', 'plugins', 'mcp', 'notes', 'hooks', 'stats', 'api-user-sessions'
	]);

	// Get the active chat tab (if in chat view)
	const activeTab = $derived(
		$activeView?.type === 'chat' && $activeView.tabId
			? $allTabs.find(t => t.id === $activeView!.tabId)
			: null
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
	let progressMode = $state<'commit' | 'pr-only' | 'finish' | 'pull'>('commit');
	let progressStep = $state(1);
	let progressCompleted = $state(false);
	let progressError = $state<string | null>(null);
	let progressIsGenerating = $state(false);
	let progressHasCleanup = $state(false);

	// Finish modal state
	let showFinishModal = $state(false);

	// Pull modal state
	let showPullModal = $state(false);

	// PR detection for current branch
	let branchPR = $state<{ number: number; title: string; url: string; head_branch: string } | null>(null);
	let _finishedWorktreeTabId: string | null = null;

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

	// Debounce rapid project changes (e.g., clicking through chats quickly)
	// to prevent cascading git loads that break commit/branch buttons.
	let _projectDebounceTimer: ReturnType<typeof setTimeout> | null = null;
	$effect(() => {
		const project = activeProject;
		const worktreeId = activeWorktreeId;
		const authenticated = $githubAuthenticated;

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

		// Clear stale PR state immediately when any dependency changes.
		branchPR = null;

		if (_gitFallbackTimer) {
			clearInterval(_gitFallbackTimer);
			_gitFallbackTimer = null;
		}

		if (project && authenticated) {
			// Initial refresh on project/auth change
			untrack(() => refreshDiffStats(project));

			// Fallback poll — only when tab is visible
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
					// Create PR step
					progressStep = commitStep + 2;
					const currentBranch = $gitStatus?.current_branch;
					if (currentBranch) {
						await createPullRequest(projectId, {
							title: finalMessage,
							body: '',
							head: currentBranch,
							base: 'main'
						});
						// Detect the new PR so UI transitions to Finish state
						await checkPullRequests(projectId, currentBranch);
					}
				}
			}

			progressCompleted = true;
		} catch (e: any) {
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
		const tabId = activeTab?.id;

		// Track tab for post-cleanup close
		_finishedWorktreeTabId = (cleanup && isWorktreeMode && worktreeId && tabId) ? tabId : null;

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
	const pullHasChanges = $derived(
		($diffStats?.additions ?? 0) > 0 ||
		($diffStats?.deletions ?? 0) > 0 ||
		($diffStats?.files_changed ?? 0) > 0
	);

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
							worktreeBranch={activeWorktreeBranch}
							openPR={branchPR}
							onClickCommit={() => { showCommitModalMode = 'commit'; showCommitModal = true; }}
							onClickCreatePR={() => { showCommitModalMode = 'pr-only'; showCommitModal = true; }}
							onClickFinish={() => showFinishModal = true}
							onClickNewBranch={() => showCreateBranchModal = true}
							onClickPull={() => showPullModal = true}
						/>
					{/if}
				{/snippet}
			</PageHeader>
		{/if}
		{#if $activeView.type === 'chat' && $activeView.tabId}
			<ChatView tabId={$activeView.tabId} />
		{:else if $activeView.type === 'profile'}
			<!-- Embedded: uses FeaturePageLayout with its own header -->
			<div class="feature-panel">
				<ProfileCard
					card={makeFeatureCard('profile', 'Profiles')}
					onClose={noop}
					onMaximize={noop}
					onFocus={noop}
					onMove={noopXY}
					onResize={noopXY}
					onOpenPlugins={onOpenPlugins || noop}
					embedded={true}
				/>
			</div>
		{:else if $activeView.type === 'project'}
			<!-- Embedded: uses FeaturePageLayout with its own header -->
			<div class="feature-panel">
				<ProjectCard
					card={makeFeatureCard('project', 'Projects')}
					onClose={noop}
					onMaximize={noop}
					onFocus={noop}
					onMove={noopXY}
					onResize={noopXY}
					embedded={true}
				/>
			</div>
		{:else if $activeView.type === 'settings'}
			<div class="feature-panel">
				<SettingsCard
					card={makeFeatureCard('settings', 'Settings')}
					onClose={noop}
					onMaximize={noop}
					onFocus={noop}
					onMove={noopXY}
					onResize={noopXY}
					embedded={true}
				/>
			</div>
		{:else if $activeView.type === 'user-settings'}
			<div class="feature-panel">
				<UserSettingsCard
					card={makeFeatureCard('user-settings', 'My Settings')}
					onClose={noop}
					onMaximize={noop}
					onFocus={noop}
					onMove={noopXY}
					onResize={noopXY}
					embedded={true}
				/>
			</div>
		{:else if $activeView.type === 'subagent'}
			<div class="feature-panel">
				<SubagentCard
					card={makeFeatureCard('subagent', 'Subagents')}
					onClose={noop}
					onMaximize={noop}
					onFocus={noop}
					onMove={noopXY}
					onResize={noopXY}
					embedded={true}
				/>
			</div>
		{:else if $activeView.type === 'file-browser'}
			<div class="feature-panel">
				<FileBrowserCard
					card={makeFeatureCard('file-browser', 'Files')}
					onClose={noop}
					onMaximize={noop}
					onFocus={noop}
					onMove={noopXY}
					onResize={noopXY}
					embedded={true}
				/>
			</div>
		{:else if $activeView.type === 'plugins'}
			<div class="feature-panel">
				<PluginManagerCard
					card={makeFeatureCard('plugins', 'Plugins')}
					onClose={noop}
					onMaximize={noop}
					onFocus={noop}
					onMove={noopXY}
					onResize={noopXY}
					embedded={true}
				/>
			</div>
		{:else if $activeView.type === 'mcp'}
			<div class="feature-panel">
				<McpManagerCard
					card={makeFeatureCard('mcp', 'MCP Servers')}
					onClose={noop}
					onMaximize={noop}
					onFocus={noop}
					onMove={noopXY}
					onResize={noopXY}
					embedded={true}
				/>
			</div>
		{:else if $activeView.type === 'notes'}
			<div class="feature-panel">
				<NotesCard
					card={makeFeatureCard('notes', 'Notes')}
					onClose={noop}
					onMaximize={noop}
					onFocus={noop}
					onMove={noopXY}
					onResize={noopXY}
					embedded={true}
				/>
			</div>
		{:else if $activeView.type === 'hooks'}
			<div class="feature-panel">
				<HooksCard
					card={makeFeatureCard('hooks', 'Hooks')}
					onClose={noop}
					onMaximize={noop}
					onFocus={noop}
					onMove={noopXY}
					onResize={noopXY}
					embedded={true}
				/>
			</div>
		{:else if $activeView.type === 'stats'}
			<div class="feature-panel">
				<StatsCard
					card={makeFeatureCard('stats', 'Stats')}
					onClose={noop}
					onMaximize={noop}
					onFocus={noop}
					onMove={noopXY}
					onResize={noopXY}
					onSessionClick={onOpenSession || noop}
					embedded={true}
				/>
			</div>
		{:else if $activeView.type === 'api-user-sessions'}
			<div class="feature-panel">
				<ApiUserSessionsCard
					card={makeFeatureCard('api-user-sessions', 'API User Sessions')}
					onClose={noop}
					onMaximize={noop}
					onFocus={noop}
					onMove={noopXY}
					onResize={noopXY}
					onSessionClick={onOpenSession || noop}
					embedded={true}
				/>
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
			if (progressCompleted && progressMode === 'finish' && _finishedWorktreeTabId) {
				tabs.updateTab(_finishedWorktreeTabId, { worktreeDeleted: true, worktreeId: null });
				_finishedWorktreeTabId = null;
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

<script lang="ts">
	/**
	 * MainPanel - Content area that renders the active view
	 *
	 * Routes between ChatView, SettingsView, and other feature pages
	 * based on the layout store's activeView state.
	 */
	import { activeView, sidebarCollapsed } from '$lib/stores/layout';
	import type { ViewType } from '$lib/stores/layout';
	import type { CardType, DeckCard } from '$lib/components/deck/cards/types';
	import { allTabs, projects } from '$lib/stores/tabs';
	import { git, gitStatus } from '$lib/stores/git';
	import { githubAuthenticated } from '$lib/stores/auth';
	import { workspaceIsStreaming } from '$lib/stores/derived/active-streaming';
	import { createPullRequest } from '$lib/api/github';
	import { generateCommitMessage, generatePRDescription } from '$lib/api/git';
	import ChatView from './ChatView.svelte';
	import PageHeader from './PageHeader.svelte';
	import GitHeaderActions from '$lib/components/git/GitHeaderActions.svelte';
	import CreateBranchModal from '$lib/components/git/CreateBranchModal.svelte';
	import CommitModal from '$lib/components/git/CommitModal.svelte';
	import CommitProgressModal from '$lib/components/git/CommitProgressModal.svelte';

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

	// Worktree branch for header context (hidden when GitHub CLI not authenticated)
	const headerWorktreeBranch = $derived.by(() => {
		if (!activeTab || !$githubAuthenticated) return undefined;
		if (activeTab.executionMode !== 'worktree' || activeTab.messages.length === 0) return undefined;
		return activeTab.selectedWorktreeBranch || activeTab.worktreeBranch || undefined;
	});

	// =========================================================================
	// Git modal state
	// =========================================================================

	let showCreateBranchModal = $state(false);
	let showCommitModal = $state(false);
	let showCommitModalMode = $state<'commit' | 'pr-only'>('commit');
	let showProgressModal = $state(false);
	let progressAction = $state<'commit' | 'push' | 'pr'>('commit');
	let progressMode = $state<'commit' | 'pr-only'>('commit');
	let progressStep = $state(1);
	let progressCompleted = $state(false);
	let progressError = $state<string | null>(null);
	let progressIsGenerating = $state(false);

	// Load git repository when active tab's project changes (only when GitHub CLI is authenticated).
	// Extract the project string OUTSIDE the effect to avoid tracking the entire activeTab object,
	// which changes on every streaming poll and would re-trigger loadRepository needlessly.
	const activeProject = $derived(activeTab?.project ?? null);

	$effect(() => {
		const project = activeProject;
		if (project && $githubAuthenticated) {
			git.loadRepository(project);
		} else {
			git.clearRepository();
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

		try {
			let finalMessage = message;

			// Generate commit message with AI if empty
			if (needsGeneration) {
				try {
					const generated = await generateCommitMessage(projectId, {
						include_unstaged: includeUnstaged
					});
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

		try {
			let prTitle = title;
			let prBody = body;

			// Generate PR description with AI if title is empty
			if (needsGeneration) {
				try {
					const generated = await generatePRDescription(projectId, {
						base_branch: 'main'
					});
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
			}

			progressCompleted = true;
		} catch (e: any) {
			progressError = e?.detail || e?.message || 'An error occurred';
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
					{#if $activeView?.type === 'chat' && activeTab?.project && $githubAuthenticated}
						<GitHeaderActions
							projectId={activeTab.project}
							isStreaming={$workspaceIsStreaming}
							onClickCommit={() => { showCommitModalMode = 'commit'; showCommitModal = true; }}
							onClickCreatePR={() => { showCommitModalMode = 'pr-only'; showCommitModal = true; }}
							onClickNewBranch={() => showCreateBranchModal = true}
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

	<CommitProgressModal
		open={showProgressModal}
		action={progressAction}
		mode={progressMode}
		currentStep={progressStep}
		completed={progressCompleted}
		error={progressError}
		branchName={$gitStatus?.current_branch ?? ''}
		isGenerating={progressIsGenerating}
		onClose={() => showProgressModal = false}
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

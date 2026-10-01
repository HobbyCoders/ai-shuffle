<script lang="ts">
	import { captureGitRetryGuard } from '$lib/stores/gitCheckoutAction';
	/**
	 * MainPanel - Content area that renders the active view
	 *
	 * Routes between ChatView, SettingsView, and other feature pages
	 * based on the layout store's activeView state.
	 */
	import { untrack, type Snippet } from 'svelte';
	import { openHeaderDropdown } from '$lib/stores/header-dropdowns';
	import { MediaQuery } from 'svelte/reactivity';
	import { activeView } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { projects, sessions } from '$lib/stores/app-data.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import {
		git,
		gitStatus,
		gitPrState,
		githubFeaturesAvailable,
		branches as branchStore
	} from '$lib/stores/git.svelte';
	import { githubAuthenticated } from '$lib/stores/auth.svelte';
	import { githubRepoInfo } from '$lib/stores/github.svelte';
	import { gitControlsStreaming } from '$lib/stores/derived/active-streaming.svelte';
	import { createPullRequest } from '$lib/api/github';
	import {
		generateCommitMessage,
		generatePRDescription,
		startShipOperation,
		type GitOperation
	} from '$lib/api/git';
	import type {
		CommitNextStep,
		CommitOptions,
		PushOptions,
		PullOptions,
		CreatePROptions,
		ShipItOptions,
		OperationOrigin
	} from '$lib/stores/derived/git-action-options';
	import { computeOperationSideEffects } from '$lib/stores/derived/git-action-options';
	import { isApiError } from '$lib/api/client';
	import { exportSession } from '$lib/api/sessions';
	import { notify } from '$lib/stores/notifications';
	import { Download } from 'lucide-svelte';
	import { browserPanel } from '$lib/stores/browser-panel.svelte';
	import { sidePanel, sessionPanelKey } from '$lib/stores/side-panel.svelte';
	import SessionSidePanel from './SessionSidePanel.svelte';
	import { gitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';
	import { gitOperations, gitOperationRunning } from '$lib/stores/gitOperations.svelte';
	import ChatView from './ChatView.svelte';
	import AssistantHome from '$lib/components/assistant/AssistantHome.svelte';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { sessionOwnerBoundary } from '$lib/stores/session-owner';
	import ChatAccess from '$lib/components/chat/ChatAccess.svelte';
	import SwarmDropdown from '$lib/components/chat/SwarmDropdown.svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import type { MainPanelHeaderContext } from './main-panel-header';
	import StudioTopBar from './StudioTopBar.svelte';
	import FeaturePanelHost from './FeaturePanelHost.svelte';
	import { pageHeader } from '$lib/stores/page-header.svelte';
	import GitHeaderActions from '$lib/components/git/GitHeaderActions.svelte';
	import { shouldShowHeaderGitActions } from './git-header-visibility';
	import { chatHeaderTitle } from './chat-empty-state';
	import { warmHighlighter } from '$lib/utils/shiki';
	import {
		gitPollIntervalMs,
		shouldExecuteGitPollTick,
		shouldPollDiffStats,
		shouldRunGitPoll
	} from './git-poll-cadence';

	// ── Lazy feature surfaces ────────────────────────────────────────────────
	// Every secondary surface (Settings, Project, Files, Notes, Automations,
	// Stats, Docs, Build, the live-browser panel and the git modals) is behind a
	// dynamic import so it gets its own Rollup chunk instead of riding in the
	// primary route bundle. The CHAT surface is intentionally NOT lazy — a
	// spinner on the core interaction would be a regression. See
	// `deck/lazy/panels.ts` for the registry and the prefetch entry points.
	import LazyPanel from '$lib/components/deck/lazy/LazyPanel.svelte';
	import { preloadWhenIdle, runWhenIdle } from '$lib/components/deck/lazy/lazy-component';
	import {
		featurePanelKeyForView,
		panelForKey,
		IDLE_PRELOAD_ORDER,
		BrowserSidePanel,
		ChangedFilesPanelLazy,
		InitRepoModalLazy,
		type FeaturePanelKey
	} from '$lib/components/deck/lazy/panels';

	interface Props {
		onOpenSession?: (sessionId: string) => void;
		/**
		 * Replaces the StudioTopBar with the shell's own header. The mobile app
		 * uses this for its PageHeader; the web UI leaves it unset.
		 */
		header?: Snippet<[MainPanelHeaderContext]>;
		/** Desktop shell: sidebar state + toggle, surfaced in the top bar. */
		sidebarCollapsed?: boolean;
		onToggleSidebar?: () => void;
	}

	let {
		onOpenSession,
		header,
		sidebarCollapsed = false,
		onToggleSidebar
	}: Props = $props();

	// Shared behaviour switches read the build's platform (see platform.svelte.ts).
	const isMobile = $derived(platform.isMobile);
	// The web shell overlays panels at narrow widths too, independently of
	// native-mobile platform identity. Match SessionSidePanel's CSS breakpoint.
	const narrowPanel = new MediaQuery('(max-width: 700px)');
	const panelOverlay = $derived(isMobile || narrowPanel.current);

	// View type → page title mapping
	const viewTitles: Record<string, string> = {
		'assistant': 'Assistant',
		'settings': 'Settings',
		'profile': 'Agents',
		'project': 'Projects',
		'file-browser': 'Files',
		'plugins': 'Plugins',
		'mcp': 'MCP Servers',
		'notes': 'Notes',
		'hooks': 'Hooks',
		'stats': 'Stats',
		'docs': 'Documentation',
	};

	// Feature pages that provide their own header.
	// These don't need the generic PageHeader
	const embeddedViews = new Set([
		'board', 'build',
		'profile', 'project', 'settings',
		'file-browser', 'plugins', 'mcp', 'notes', 'hooks', 'automations', 'stats', 'docs'
	]);

	// The Build page hosts the agent-config tools under one surface. The
	// member view types remain as deep-link targets ("Configure agents →",
	// "Manage plugins") and resolve to BuildPage with the right sub-tab.
	type BuildSub = 'profile' | 'mcp' | 'plugins' | 'hooks';
	const BUILD_SUBS: BuildSub[] = ['profile', 'mcp', 'plugins', 'hooks'];
	const isBuildView = $derived(
		$activeView?.type === 'build' || BUILD_SUBS.includes($activeView?.type as BuildSub)
	);
	const buildRequestedSub = $derived(
		BUILD_SUBS.includes($activeView?.type as BuildSub)
			? ($activeView!.type as BuildSub)
			: undefined
	);
	// One-shot deep-link target (agent id) to open in its editor.
	const buildRequestedItemId = $derived($activeView?.itemId ?? undefined);

	// =========================================================================
	// Feature-panel keep-alive (R14)
	// =========================================================================
	// The panels used to be `{#if}` branches: leaving Build/Notes unmounted them,
	// and coming back remounted + refetched everything behind a spinner. We now
	// keep the current panel plus the most recently visited one mounted (hidden
	// with `display:none` + `inert`), so a chat → Build → chat → Build round trip
	// renders instantly from live component state instead of re-running mount
	// loads. Capped at two so we never accumulate every surface in memory.
	const KEEP_ALIVE_LIMIT = 2;
	const featureKey = $derived<FeaturePanelKey | null>(
		isBuildView ? 'build' : featurePanelKeyForView($activeView?.type)
	);
	/** True when the chat surface (not a feature panel) owns the canvas. */
	const showAssistantSurface = $derived($activeView?.type === 'assistant');
	const assistantAgentId = $derived(showAssistantSurface ? $activeView?.itemId ?? null : null);
	const showChatSurface = $derived(featureKey === null && !showAssistantSurface);
	let mountedPanels = $state<FeaturePanelKey[]>([]);

	$effect(() => {
		const key = featureKey;
		if (!key) return;
		untrack(() => {
			if (mountedPanels[0] === key) return;
			mountedPanels = [key, ...mountedPanels.filter((k) => k !== key)].slice(0, KEEP_ALIVE_LIMIT);
		});
	});

	/**
	 * Grammars worth warming ahead of time. Shiki is dynamically imported now
	 * (R6), and a *settled* transcript renders each code block once — so a
	 * grammar that arrives after that render leaves the block plain until
	 * something re-renders it. Warming the languages an agent transcript
	 * actually contains closes that window; the rest (rust/go/sql/svelte/html/
	 * css) still load on demand. This is ~45 kB gzipped fetched at idle, against
	 * the ~180 kB gzipped of grammars that used to sit in the entry chunk.
	 */
	const WARM_LANGS = [
		'typescript',
		'javascript',
		'python',
		'bash',
		'json',
		'diff',
		'markdown',
		'yaml'
	];

	// Warm the secondary chunks once the app is idle, so the first visit to a
	// panel is already a cache hit and LazyPanel renders it in the same frame.
	$effect(() => {
		const cancelPreload = preloadWhenIdle(IDLE_PRELOAD_ORDER);
		const cancelShiki = runWhenIdle(() => warmHighlighter(WARM_LANGS));
		return () => {
			cancelPreload();
			cancelShiki();
		};
	});

	// Git dialogs mount on first use.
	let initRepoEverOpened = $state(false);

	// Get the active session state (if the conversation panel is open)
	const activeTab = $derived(
		$activeView?.type === 'session' ? $activeSession : null
	);
	const sessionBrowser = $derived(browserPanel.forSession(activeTab?.sessionId ?? null));


	// Header title. For a chat, a session BOUND to an id always shows its own
	// title — including when it currently has zero messages (just cleared, or
	// mid-load). Previously an open, titled session with no messages fell through
	// to "New Chat", which read as "clearing deleted my session" (see #558 +
	// chat-empty-state.ts). Only a genuinely unbound, empty chat shows "New Chat".
	const headerTitle = $derived.by(() => {
		if (!$activeView) return '';
		if ($activeView.type === 'session') {
			return chatHeaderTitle({
				sessionId: activeTab?.sessionId ?? null,
				title: activeTab?.title,
				messageCount: activeTab?.messages.length ?? 0
			});
		}
		return viewTitles[$activeView.type] || '';
	});

	// Project name for header context
	const headerProjectName = $derived(
		activeTab ? $projects.find(p => p.id === activeTab.project)?.name : undefined
	);

	// Breadcrumbs for the desktop top bar: `project › session title` in a chat,
	// just the section name everywhere else. The project crumb returns to the
	// sessions board.
	const crumbProject = $derived(
		$activeView?.type === 'session' ? (headerProjectName || 'Sessions') : null
	);
	const crumbTitle = $derived.by(() => {
		const type = $activeView?.type;
		if (!type) return 'Sessions';
		if (type === 'session') return headerTitle;
		if (type === 'board') return 'Sessions';
		if (isBuildView) return 'Build';
		return viewTitles[type] || '';
	});

	// Worktree branch for the GitHeaderActions context (passed through to its
	// branch display). The read-only project/worktree context badge that used to
	// live in the PageHeader now renders in the ContextPillBar above the input.
	const activeWorktreeBranch = $derived.by(() => {
		if (!activeTab) return undefined;
		if (!(activeTab.worktreeId || activeTab.worktreeBranch)) return undefined;
		return activeTab.selectedWorktreeBranch || activeTab.worktreeBranch || undefined;
	});

	// =========================================================================
	// Git modal state (retained: branch creation, init repo, diff review)
	// =========================================================================


	// Init repo modal state
	let showInitRepoModal = $state(false);

	// Visibility is scoped, so switching sessions cannot show another checkout's diff.
	function openDiffPanel() {
		sidePanel.open(panelScope, 'changes');
		git.refreshChangedFiles();
	}
	function closeSidePanel() {
		if (activeSidePanel.active === 'browser') browserPanel.closePanel(activeTab?.sessionId ?? null);
		else sidePanel.close(panelScope);
	}

	function handleRepoInitialized() {
		if (activeProject) git.loadRepository(activeProject, true, activeSessionId ?? undefined);
	}

	// PR lifecycle for the active session's branch. Fetched by the git store's
	// session-scoped /git/pr-state call and refreshed after every PR-relevant
	// event (streaming end, pull, commit-with-PR, Create PR, Finish, Ship It,
	// cross-device branch_changed WS nudge). The old local checkPullRequests()
	// went stale whenever a PR was created/merged without a branch change —
	// this derivation can't.
	const openPrForBranch = $derived($gitPrState?.open_pr ?? null);

	// Load Git for the active session on its owning worker.
	// Extract the project and worktree strings OUTSIDE the effect to avoid tracking the entire
	// activeTab object, which changes on every streaming poll and would re-trigger load needlessly.
	// A legacy worktree draft still needs its local project ID. Existing
	// sessions and canonical drafts use the portable identity, not a previous
	// owner's legacy project alias (which may not exist on this worker).
	const activeProject = $derived(!activeTab?.sessionId && activeTab?.worktreeId
		? activeTab.project || activeTab.projectV2Id || null
		: activeTab?.projectV2Id || activeTab?.project || null);
	const activeSessionId = $derived(activeTab?.sessionId ?? null);
	const activeSessionRow = $derived($sessions.find((s) => s.id === activeSessionId));
	// Adoption refreshes the session list, not the tab's initial snapshot.
	const activeWorktreeDeleted = $derived(activeSessionRow?.worktree_deleted ?? activeTab?.worktreeDeleted ?? false);
	const selectedWorkerTarget = $derived(!activeSessionId && (activeTab?.deviceIntent
		|| (activeTab?.projectV2Id && !activeTab.worktreeId)
		|| (activeTab?.worktreeBranch && !activeTab.worktreeId))
		? { workerId: activeTab?.deviceIntent?.id ?? 'local', branch: activeTab?.worktreeBranch || activeTab?.selectedWorktreeBranch || null }
		: null);
	const activeWorktreeId = $derived(
		!selectedWorkerTarget && (activeTab?.worktreeId || activeTab?.worktreeBranch) && !activeWorktreeDeleted
			? (activeTab.worktreeId ?? null)
			: null
	);
	const panelScope = $derived(activeTab?.sessionId ? sessionPanelKey(activeTab.sessionId)
		: activeProject ? `draft:${JSON.stringify([activeProject, selectedWorkerTarget, activeWorktreeId])}` : null);
	const activeSidePanel = $derived(sidePanel.forScope(panelScope));
	// Session-scoped Git requests are executed by the owning worker.
	const activeIsRemote = $derived(
		activeSessionRow?.is_remote ?? false
	);

	const activeGithubAvailable = $derived(selectedWorkerTarget ? false : activeIsRemote
		? $gitPrState?.has_github === true
		: $githubFeaturesAvailable);

	// GitHub auth verdicts feed the git store's hysteresis guard rather than
	// gating the UI directly: `gh auth status` is a subprocess that transiently
	// fails under load, and a single blip used to blank the whole git surface
	// for up to two minutes (G10c). The store only believes a `false` after two
	// consecutive failures ≥60s apart.
	$effect(() => {
		git.setGithubAuth($githubAuthenticated);
	});

	// Debounce rapid project changes (e.g., clicking through chats quickly)
	// to prevent cascading git loads that break commit/branch buttons.
	let _projectDebounceTimer: ReturnType<typeof setTimeout> | null = null;
	$effect(() => {
		const project = activeProject;
		const worktreeId = activeWorktreeId;
		const isRemote = activeIsRemote;
		const workerTarget = selectedWorkerTarget;
		// Track session ID so the effect re-fires on every tab switch,
		// even when two sessions share the same project/worktree.
		// This ensures git state is always fresh after a session change.
		const sessionId = activeSessionId ?? undefined;

		if (_projectDebounceTimer) {
			clearTimeout(_projectDebounceTimer);
			_projectDebounceTimer = null;
		}

		// NOTE: no `githubAuthenticated` term. Local git (status, branches,
		// commit, diff) needs a local checkout and nothing else; requiring GitHub
		// auth here is what made a `gh` blip call clearRepository() and hide the
		// UI. GitHub-backed actions consult `githubFeaturesAvailable` instead.
		if (project) {
			git.setWorkerTarget(workerTarget);
			git.setRemoteSession(sessionId, isRemote);
			// IMMEDIATE: restore cached state so UI updates within the same frame.
			// sessionId scopes v2 branch sessions to their own checkout — without it
			// a branch session with no legacy worktree row would poll the base checkout.
			// The branch hint resolves the checkout identity in the same frame, so a
			// FIRST visit to a sibling session on a known checkout is instant too.
			const mode = worktreeId ? 'worktree' : 'project';
			git.restoreCacheImmediate(project, worktreeId, mode, sessionId, activeWorktreeBranch ?? null);

			// DEBOUNCED: network fetch for fresh data
			_projectDebounceTimer = setTimeout(() => {
				_projectDebounceTimer = null;
				if (worktreeId) {
					git.loadForWorktree(project, worktreeId, false, sessionId);
				} else {
					git.loadRepository(project, false, sessionId);
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
	// Adaptive git polling
	// =========================================================================
	// Frontend-driven polling scoped to the active tab only. The cadence rules
	// live in ./git-poll-cadence.ts so they can be unit-tested:
	// - 2s while the agent is streaming OR just after a user git action
	// - 30s idle fallback (was 5s: the backend status cache TTL is 2s, so every
	//   idle tick was a guaranteed miss → a full pygit2 worktree scan; F3)
	// - diff stats at most once a minute while idle (G8: the old gate compared
	//   against the tick interval itself, so it fired every tick)
	// - paused entirely when the tab is hidden OR the git UI is off screen

	let _gitPollTimer: ReturnType<typeof setTimeout> | null = null;
	let _gitPollRunning = false;
	let _gitLastActivity = 0; // timestamp of last streaming event / user git action

	let _lastIdleDiffStatsPoll = 0;

	/**
	 * The git UI is on screen for this session. Polling status for a surface
	 * nobody can see is pure idle churn — the panel refreshes on return.
	 */
	// Deliberately NOT gated on GitHub auth (see the repo-load effect): the poll
	// feeds the LOCAL git status, which works without `gh`.
	const gitUiVisible = $derived(
		!!activeTab &&
			shouldShowHeaderGitActions({
				viewType: $activeView?.type,
				hasProject: !!activeProject,
				githubAuthenticated: $githubAuthenticated,
				githubAvailable: activeGithubAvailable,
				worktreeDeleted: activeWorktreeDeleted,
				isRemote: activeIsRemote,
				sessionMode: activeTab.mode
			})
	);

	function gitPollNow() {
		// Skip polling while a commit/push/PR/pull operation is in progress
		// (those make their own calls and would saturate the 6-connection pool),
		// or once the worktree 404'd.
		if (!shouldExecuteGitPollTick({
			operationInProgress: git.operationInProgress,
			worktreeGone: git.worktreeGone
		})) return;

		const now = Date.now();
		git.refreshStatus();
		if (shouldPollDiffStats(now, _gitLastActivity, _lastIdleDiffStatsPoll)) {
			git.refreshDiffStats();
			_lastIdleDiffStatsPoll = now;
		}
	}

	const gitMenuOpen = $derived($openHeaderDropdown?.startsWith('git-') ?? false);

	function scheduleGitPoll() {
		if (_gitPollTimer) clearTimeout(_gitPollTimer);
		if (!_gitPollRunning || document.hidden) return;
		_gitPollTimer = setTimeout(() => {
			gitPollNow();
			scheduleGitPoll();
		}, gitPollIntervalMs(Date.now(), _gitLastActivity, gitMenuOpen));
	}

	function startGitPoll() {
		stopGitPoll();
		_gitPollRunning = true;
		// Don't reset _gitLastActivity — keep idle mode unless there's actual
		// git activity (streaming / a user action). This prevents session/tab
		// switches from spuriously triggering 30s of active polling.
		scheduleGitPoll();
	}

	function stopGitPoll() {
		if (_gitPollTimer) {
			clearTimeout(_gitPollTimer);
			_gitPollTimer = null;
		}
		_gitPollRunning = false;
	}

	/**
	 * A user git action (or the end of a turn) puts the poll back on the fast
	 * cadence for the activity window, so the header buttons settle promptly.
	 */
	function markGitActivity() {
		_gitLastActivity = Date.now();
		if (_gitPollRunning) scheduleGitPoll();
	}

	// Pause/resume git poll when tab visibility changes.
	function handleVisibilityChange() {
		if (document.hidden) {
			stopGitPoll();
		} else if (!_gitPollRunning && shouldRunGitPoll({ gitUiVisible, documentHidden: false })) {
			startGitPoll();
			gitPollNow(); // Immediate refresh on return
		}
	}

	// Main effect: start/stop adaptive poll when the git UI's visibility or
	// target changes. NOTE: activeSessionId deliberately excluded — session
	// switches within the same project shouldn't restart polling (worktreeId
	// covers tab-switch context).
	$effect(() => {
		const visible = gitUiVisible;
		void activeProject;
		void activeWorktreeId; // Track worktree changes for tab switches

		stopGitPoll();

		if (shouldRunGitPoll({ gitUiVisible: visible, documentHidden: document.hidden })) {
			startGitPoll();
		}

		document.addEventListener('visibilitychange', handleVisibilityChange);

		return () => {
			stopGitPoll();
			document.removeEventListener('visibilitychange', handleVisibilityChange);
		};
	});

	// Inspecting the dropdown needs fresh owner activity, even if THIS chat
	// is idle. Reschedule on open/close without tracking the whole git store.
	$effect(() => {
		const opened = gitMenuOpen;
		untrack(() => {
			if (!_gitPollRunning) return;
			if (opened) gitPollNow();
			scheduleGitPoll();
		});
	});

	// When streaming ends, do an immediate refresh so buttons update promptly.
	// Uses forceRefresh() to abort any in-flight poll requests whose responses
	// may predate the session's final git operations.
	let _wasStreaming = false;
	$effect(() => {
		const streaming = $gitControlsStreaming;
		if (streaming) {
			_wasStreaming = true;
		} else if (_wasStreaming) {
			_wasStreaming = false;
			if (activeProject) {
				untrack(() => {
					// forceRefresh() now covers PR state; a separate refreshPrState()
					// would just duplicate the request.
					git.forceRefresh();
					markGitActivity(); // Reset to active interval for post-streaming cool-down
				});
			}
		}
	});

	// =========================================================================
	// Git action handlers — execute real git / GitHub operations
	// =========================================================================
	// The mutating git buttons (Commit / Push / Pull / Create PR / Finish /
	// Ship It) run the operation directly against the git/GitHub API. Multi-step
	// chains (Commit's optional push/PR, and the server-driven Finish / Ship It)
	// report progress through the gitOperations store, which drives
	// GitProgressPopover and pauses the git poller while an op is in flight.
	//
	// Every handler accepts an options bag so the (upcoming) chevron dropdowns
	// can override the saved defaults; a plain click uses the gitActionPreferences
	// defaults. Guarding: no action fires while the session is streaming, while
	// another operation is running, or without an active project.

	// Upstream tracking state (drives quick-action guards / next-step fallbacks).
	const currentBranchForPrompt = $derived($branchStore.find(b => b.current));
	const needsPull = $derived((currentBranchForPrompt?.behind ?? 0) > 0);
	const isOnDefaultBranch = $derived(
		($gitStatus?.current_branch ?? null) === ($gitStatus?.default_branch ?? $githubRepoInfo?.default_branch ?? 'main')
	);

	// Git-action option-bag types are shared with GitHeaderActions /
	// GitActionOptionsDropdown via $lib/stores/derived/git-action-options.

	/** Whether git action buttons may fire (guards double-taps + in-flight ops). */
	function gitActionsBlocked(): boolean {
		return gitControlsStreaming.current || gitOperationRunning.current || !activeProject || !git.current.hydrated || !!git.current.error;
	}

	function errText(e: unknown): string {
		if (isApiError(e)) return e.detail || 'Something went wrong';
		const obj = e as { detail?: string; message?: string };
		return obj?.detail || obj?.message || 'Something went wrong';
	}

	/** A push rejection that means "your branch is behind — pull first". */
	function isNonFastForward(detail: string): boolean {
		const d = detail.toLowerCase();
		return d.includes('fast-forward') || d.includes('non-fast') ||
			d.includes('fetch first') || d.includes('rejected') || d.includes('behind');
	}

	function openPRUrl(url: string) {
		window.open(url, '_blank', 'noopener');
	}

	/**
	 * Push the current branch, then generate a title/description (unless
	 * overridden) and open a PR into the repo's default branch. Shared by
	 * handleCreatePR and Commit's "next step = PR" path. Assumes the caller has
	 * already pushed if it drives its own push step.
	 */
	async function runCreatePR(opts: CreatePROptions, target = git.captureTarget()): Promise<{ url: string; number: number }> {
		if (target.workerTarget) throw new Error('Create a session on the selected worker before opening a pull request');
		const { projectId, worktreeId, sessionId } = target;
		const base = opts.base ?? target.base ?? 'main';
		const head = target.head;
		if (!head) throw new Error('No current branch to open a pull request from');
		let title = opts.title?.trim();
		let body = opts.body;
		if (!title) {
			const gen = await generatePRDescription(projectId, { base_branch: base }, worktreeId, sessionId);
			title = gen.title;
			body = body ?? gen.body;
		}
		const pr = await createPullRequest(projectId, { title, body: body ?? '', head, base, draft: opts.draft ?? false, session_id: sessionId });
		return { url: pr.url, number: pr.number };
	}

	async function handleCommit(opts: CommitOptions = {}) {
		if (gitActionsBlocked()) return;
		markGitActivity(); // user git action → responsive poll cadence
		const target = git.captureTarget();
		const projectId = target.projectId;
		const prefs = gitActionPreferences.current;
		const includeUnstaged = opts.includeUnstaged ?? prefs.includeUnstaged;
		const userMessage = opts.message?.trim();

		// Guard the next step against invalid combos (mirrors the old logic):
		// never push while behind, and downgrade PR→push on the default branch.
		let nextStep: CommitNextStep = opts.nextStep ?? prefs.nextStep;
		if (needsPull) nextStep = 'commit';
		else if (nextStep === 'pr' && isOnDefaultBranch) nextStep = 'push';

		const steps: { id: string; label: string }[] = [];
		if (!userMessage) steps.push({ id: 'message', label: 'Generate commit message' });
		steps.push({ id: 'commit', label: 'Commit changes' });
		if (nextStep === 'push' || nextStep === 'pr') steps.push({ id: 'push', label: 'Push to remote' });
		if (nextStep === 'pr') steps.push({ id: 'pr', label: 'Open pull request' });

		gitOperations.begin('Commit', steps);
		try {
			let message = userMessage;
			if (!message) {
				gitOperations.setStep('message', 'running');
				const gen = await generateCommitMessage(projectId, { include_unstaged: includeUnstaged }, target.worktreeId, target.sessionId ?? target.workerTarget);
				message = gen.message;
				gitOperations.setStep('message', 'done');
			}

			gitOperations.setStep('commit', 'running');
			await git.commitChanges(message, includeUnstaged, target);
			gitOperations.setStep('commit', 'done');

			if (nextStep === 'push' || nextStep === 'pr') {
				gitOperations.setStep('push', 'running');
				await git.pushCommits(undefined, true, false, target);
				gitOperations.setStep('push', 'done');
			}

			let prInfo: { url: string; number: number } | null = null;
			if (nextStep === 'pr') {
				gitOperations.setStep('pr', 'running');
				prInfo = await runCreatePR({}, target);
				gitOperations.setStep('pr', 'done', { detail: `#${prInfo.number}` });
				if (activeSessionId === (target.sessionId ?? null)) git.refreshPrState(true).catch(() => {});
			}

			gitOperations.succeed();
			if (prInfo) {
				const url = prInfo.url;
				notify(`Opened pull request #${prInfo.number}`, { severity: 'success', action: { label: 'View PR', onClick: () => openPRUrl(url) } });
			} else {
				notify('Committed changes', { severity: 'success' });
			}
		} catch (e) {
			gitOperations.failCurrent(errText(e));
		}
	}

	async function handlePush(opts: PushOptions = {}) {
		if (gitActionsBlocked()) return;
		const guardRetry = captureGitRetryGuard();
		markGitActivity(); // user git action → responsive poll cadence
		const force = opts.force ?? false;
		gitOperations.begin(force ? 'Force push' : 'Push', []);
		try {
			await git.pushCommits(undefined, true, force);
			gitOperations.succeed();
			notify('Pushed to remote', { severity: 'success' });
		} catch (e) {
			gitOperations.clear();
			const detail = errText(e);
			if (!force && isNonFastForward(detail)) {
				notify(detail, { severity: 'error', action: { label: 'Pull first', onClick: () => guardRetry(() => { void handlePull(); }) } });
			} else {
				notify(detail, { severity: 'error' });
			}
		}
	}

	async function handlePull(opts: PullOptions = {}) {
		if (gitActionsBlocked()) return;
		const guardRetry = captureGitRetryGuard();
		markGitActivity(); // user git action → responsive poll cadence
		const prefs = gitActionPreferences.current;
		const strategy = opts.strategy ?? prefs.pullStrategy;
		const autoStash = opts.autoStash ?? prefs.autoStash;
		gitOperations.begin('Pull', []);
		try {
			const res = await git.pull(strategy, autoStash);
			git.refreshPrState(true).catch(() => {});
			gitOperations.clear();
			if (res.has_conflicts) {
				const files = res.conflicts.length ? res.conflicts.join(', ') : 'some files';
				notify(`Pull hit conflicts in ${files}. Resolve them, then commit to finish the merge.`, { severity: 'error' });
			} else {
				notify(res.message || 'Pulled latest changes', { severity: 'success' });
			}
		} catch (e) {
			gitOperations.clear();
			// Dirty working tree without auto-stash → 409. Offer a stash retry
			// (mirrors the branch-checkout 409 → "Stash & switch" pattern).
			const dirty = !autoStash && isApiError(e) && e.status === 409;
			const detail = errText(e);
			if (dirty) {
				notify(detail, { severity: 'error', action: { label: 'Stash & pull', onClick: () => guardRetry(() => { void handlePull({ strategy, autoStash: true }); }) } });
			} else {
				notify(detail, { severity: 'error' });
			}
		}
	}

	async function handleCreatePR(opts: CreatePROptions = {}) {
		if (gitActionsBlocked()) return;
		markGitActivity(); // user git action → responsive poll cadence
		const target = git.captureTarget();
		gitOperations.begin('Create PR', [
			{ id: 'push', label: 'Push to remote' },
			{ id: 'pr', label: 'Open pull request' }
		]);
		try {
			gitOperations.setStep('push', 'running');
			await git.pushCommits(undefined, true, false, target);
			gitOperations.setStep('push', 'done');

			gitOperations.setStep('pr', 'running');
			const prInfo = await runCreatePR(opts, target);
			gitOperations.setStep('pr', 'done', { detail: `#${prInfo.number}` });

			if (activeSessionId === (target.sessionId ?? null)) git.refreshPrState(true).catch(() => {});
			gitOperations.succeed();
			const url = prInfo.url;
			notify(`Opened pull request #${prInfo.number}`, { severity: 'success', action: { label: 'View PR', onClick: () => openPRUrl(url) } });
		} catch (e) {
			gitOperations.failCurrent(errText(e));
		}
	}

	async function handleShipIt(opts: ShipItOptions = {}) {
		if (gitActionsBlocked()) return;
		if (selectedWorkerTarget) {
			notify('Create a session on the selected worker before shipping changes', { severity: 'error' });
			return;
		}
		markGitActivity(); // user git action → responsive poll cadence
		const projectId = activeProject!;
		const origin: OperationOrigin = { sessionId: activeSessionId, projectId };
		const prefs = gitActionPreferences.current;
		const mergeMethod = opts.mergeMethod ?? prefs.mergeMethod;
		const cleanup = opts.cleanup ?? prefs.cleanupAfterMerge;
		const includeUnstaged = opts.includeUnstaged ?? prefs.includeUnstaged;
		const base = opts.base ?? $gitStatus?.default_branch ?? $githubRepoInfo?.default_branch ?? undefined;
		try {
			const op = await gitOperations.runServer(
				projectId,
				'Ship It',
				() =>
					startShipOperation(projectId, {
						worktree_id: activeWorktreeId ?? undefined,
						session_id: activeSessionId ?? undefined,
						merge_method: mergeMethod,
						cleanup,
						include_unstaged: includeUnstaged,
						base_branch: base,
						draft: opts.draft ?? false
					}),
				{ sessionId: origin.sessionId, kind: 'ship' }
			);
			onOperationDone(op, origin);
		} catch {
			// runServer already pinned the failure in the progress popover.
		}
	}

	/**
	 * Shared success handling for the server-driven Finish / Ship It operations.
	 * A terminal error is left pinned in the popover (no toast). On success:
	 * surface the PR link, refresh git/PR state, and — when the backend cleaned
	 * up the worktree — flag it deleted so git polling stops for the gone folder.
	 *
	 * Session-scoped effects (markWorktreeDeleted, git refresh) are gated on the
	 * user STILL viewing the originating session/project (`origin`, captured at
	 * start), so an op that resolves after a session switch can't mutate the
	 * wrong session's state. The success toast is always safe to show.
	 */
	function onOperationDone(op: GitOperation, origin: OperationOrigin) {
		const effects = computeOperationSideEffects(
			{
				status: op.status,
				worktreeDeleted: op.result?.worktree_deleted,
				prUrl: op.result?.pr_url,
				prNumber: op.result?.pr_number
			},
			origin,
			{ sessionId: activeSessionId, projectId: activeProject }
		);
		if (!effects) return;

		if (effects.markWorktreeDeleted) {
			// Flips activeWorktreeId → null so the header reloads project-mode git
			// state and the git store stops polling the removed worktree folder.
			activeSession.markWorktreeDeleted();
		}
		if (effects.refreshGit) {
			git.forceRefresh();
			markGitActivity();
		}
		if (effects.toast.prUrl) {
			const url = effects.toast.prUrl;
			notify(effects.toast.label, { severity: 'success', action: { label: 'View PR', onClick: () => openPRUrl(url) } });
		} else {
			notify('Operation complete', { severity: 'success' });
		}
	}

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
			notify(detail || 'Failed to download chat history', { severity: 'error' });
		} finally {
			downloadingJsonl = false;
		}
	}
</script>

<!--
	Header actions are declared once and rendered by whichever bar is on screen:
	the desktop StudioTopBar, or the shell's own `header` snippet (the mobile
	app's AppTopBar). The git cluster is the existing GitHeaderActions
	instance with its existing handlers.
-->
{#snippet downloadAction()}
	{#if $activeView?.type === 'session' && activeTab?.sessionId && activeTab.messages.length > 0}
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

{#snippet headerActions()}
	{#if $activeView?.type === 'session' && activeTab?.sessionId}
		{#key activeTab.sessionId}
			<!-- Dialog only: the git panel's "Private access" row opens it, and the
			     header button reads its pending count (chatAccessSummary). -->
			<ChatAccess sessionId={activeTab.sessionId} trigger="none" />
			<SwarmDropdown
				sessionId={activeTab.sessionId}
				standalone
				hydrateOnly={shouldShowHeaderGitActions({
					viewType: $activeView?.type, hasProject: !!activeProject,
					githubAvailable: activeGithubAvailable, worktreeDeleted: activeWorktreeDeleted,
					isRemote: activeIsRemote, sessionMode: activeTab.mode
				}) && $gitStatus?.is_git_repo !== false && !$git.error}
			/>
		{/key}
	{/if}
	{#if activeTab && shouldShowHeaderGitActions({
		viewType: $activeView?.type,
		hasProject: !!activeProject,
		githubAuthenticated: $githubAuthenticated,
		githubAvailable: activeGithubAvailable,
		worktreeDeleted: activeWorktreeDeleted,
		isRemote: activeIsRemote,
		sessionMode: activeTab.mode
	})}
		<div class="git-actions-host">
			<GitHeaderActions
				sessionId={activeTab.sessionId}
				isStreaming={$gitControlsStreaming}
				error={$git.error}
				operationInProgress={$gitOperationRunning}
				isGitRepo={$gitStatus?.is_git_repo !== false}
				projectName={headerProjectName}
				worktreeBranch={activeWorktreeBranch}
				openPR={openPrForBranch}
				githubAvailable={activeGithubAvailable}
				onClickCommit={handleCommit}
				onClickCreatePR={handleCreatePR}
				onClickPush={handlePush}
				onClickPull={handlePull}
				onClickInitRepo={() => { initRepoEverOpened = true; showInitRepoModal = true; }}
				onClickShipIt={handleShipIt}
				onOpenDiffReview={openDiffPanel}
				onDownloadChat={header ? undefined : handleDownloadJsonl}
				downloadingChat={downloadingJsonl}
			/>
		</div>
	{/if}
{/snippet}

<div class="main-panel">
	{#if header}
		<!-- The mobile shell owns the title bar and its separate left/right action row. -->
		{@render header({
			title: headerTitle,
			isSessionView: $activeView?.type === 'session',
			embedded: $activeView ? embeddedViews.has($activeView.type) : false,
			downloadAction,
			headerActions
		})}
	{:else}
		<!-- Desktop: one bar for the whole main panel — sidebar toggle,
		     breadcrumbs, and the action cluster. -->
		<StudioTopBar
			projectLabel={crumbProject}
			title={crumbTitle}
			sessionId={activeTab?.sessionId ?? null}
			{sidebarCollapsed}
			{onToggleSidebar}
		>
			{#snippet actions()}
				{@render downloadAction()}
				{@render headerActions()}
				{#if pageHeader.actions}
					<!-- Desktop app: the page on screen lends its header actions to
					     the strip (see StudioPageHeader). -->
					<div class="page-actions">{@render pageHeader.actions()}</div>
				{/if}
			{/snippet}
		</StudioTopBar>
	{/if}

	<div class="main-content">
		<div class="main-surface" inert={!!activeSidePanel.active && (activeSidePanel.expanded || panelOverlay)}>
		{#if $activeView}
			{#if showAssistantSurface && $isAdmin}
				<div class="assistant-surface">{#key `${$sessionOwnerBoundary}:${assistantAgentId ?? ''}`}<AssistantHome requestedAgentId={assistantAgentId} />{/key}</div>
			{/if}
			{#if showChatSurface}
				<div class="chat-row">
					<div class="chat-browser-underlay">
					<ChatView
						onBrowserToggle={$activeView?.type === 'session' && activeTab?.sessionId
							? () => browserPanel.toggle(activeTab?.sessionId ?? null)
							: undefined}
						onBrowserPreload={() => BrowserSidePanel.preload()}
						browserOpen={sessionBrowser.open}
					/>
					</div>
				</div>
			{/if}
			<!--
				Feature panels are lazily imported AND kept alive: the active one plus
				the previous one stay mounted, the inactive one hidden. Revisiting a
				panel is then a same-frame render with no refetch (R14).
			-->
			{#each mountedPanels as key (key)}
				<div class="feature-panel" class:is-inactive={key !== featureKey} inert={key !== featureKey}>
					<FeaturePanelHost active={key === featureKey}>
					{#if key === 'build'}
						<LazyPanel
							handle={panelForKey('build')}
							props={{ requestedSub: buildRequestedSub, requestedItemId: buildRequestedItemId }}
							label="Build"
						/>
					{:else if key === 'project'}
						<LazyPanel handle={panelForKey('project')} label="Projects" />
					{:else if key === 'settings'}
						<LazyPanel handle={panelForKey('settings')} label="Settings" />
					{:else if key === 'file-browser'}
						<LazyPanel handle={panelForKey('file-browser')} label="Files" />
					{:else if key === 'notes'}
						<LazyPanel handle={panelForKey('notes')} props={{ initialItemId: $activeView?.type === 'notes' ? $activeView.itemId : null }} label="Notes" />
					{:else if key === 'automations'}
						<LazyPanel handle={panelForKey('automations')} label="Automations" />
					{:else if key === 'stats'}
						<LazyPanel
							handle={panelForKey('stats')}
							props={{ onSessionClick: onOpenSession || (() => {}) }}
							label="Stats"
						/>
					{:else if key === 'docs'}
						<LazyPanel
							handle={panelForKey('docs')}
							props={{ mobile: isMobile, initialItemId: $activeView?.type === 'docs' ? $activeView.itemId : null }}
							label="Documentation"
						/>
					{/if}
					</FeaturePanelHost>
				</div>
			{/each}
		{:else}
			<!-- Default landing: a fresh chat (the Sessions board is retired) -->
			<div class="chat-row">
				<ChatView />
			</div>
		{/if}
		</div>
		{#if activeSidePanel.active}
			<SessionSidePanel active={activeSidePanel.active} expanded={activeSidePanel.expanded}
				width={browserPanel.current.width} mobile={panelOverlay}
				onClose={closeSidePanel}
				onResize={(width) => browserPanel.setWidth(width)}
				onExpand={() => sidePanel.setExpanded(panelScope, !activeSidePanel.expanded)}>
				{#snippet children(panelActions)}
				{#key `${panelScope}:${activeSidePanel.active}`}
					{#if activeSidePanel.active === 'browser' && activeTab?.sessionId}
						<LazyPanel handle={BrowserSidePanel} props={{ sessionId: activeTab.sessionId, embedded: true, panelActions }} label="the live browser" fallbackActions={panelActions} />
					{:else if activeSidePanel.active === 'changes' && activeProject}
						<LazyPanel handle={ChangedFilesPanelLazy} label="changed files" fallbackActions={panelActions}
							props={{ projectId: activeProject, open: true, reviewMode: true, embedded: true,
								onClose: closeSidePanel, onViewDiff: () => {}, panelActions }} />
					{/if}
				{/key}
				{/snippet}
			</SessionSidePanel>
		{/if}
	</div>
</div>

<!-- Git modals (rendered outside main-panel for proper z-index stacking) -->
<!--
	Local-git surfaces: gated on a project, NOT on GitHub auth. A transient
	`gh auth status` failure must not unmount an open diff review (G10c).
-->
{#if activeProject}
	{#if initRepoEverOpened}
		<LazyPanel
			handle={InitRepoModalLazy}
			label="the init-repo dialog"
			bare
			props={{
				open: showInitRepoModal,
				projectId: activeProject,
				projectName: headerProjectName ?? '',
				onClose: () => (showInitRepoModal = false),
				sessionId: activeSessionId ?? undefined,
				onInitialized: handleRepoInitialized
			}}
		/>
	{/if}
{/if}

<style>
	.assistant-surface { height:100%; min-height:0; overflow-y:auto; overscroll-behavior:contain; }
	.main-content { position: relative; display: flex; flex: 1; min-width: 0; min-height: 0; overflow: hidden; }
	/* Keep composer popovers below the sibling side-panel overlay. */
	.main-surface { isolation: isolate; display: flex; flex-direction: column; flex: 1; min-width: 0; min-height: 0; overflow: hidden; }
	.chat-browser-underlay { display: flex; flex: 1; min-width: 0; min-height: 0; }
	.chat-browser-underlay > :global(.chat-view) { flex: 1; min-width: 0; }
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

	/* Anchor for the git progress popover so it drops under the header actions. */
	.page-actions {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.git-actions-host {
		position: relative;
		display: flex;
		align-items: center;
	}

	/* Download chat history button — matches the mobile PageHeader's .header-btn styling */
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

	/* Anchor the live browser overlay below the page header. */
	.chat-row {
		position: relative;
		display: flex;
		flex: 1;
		min-height: 0;
		min-width: 0;
		overflow: hidden;
	}

	.chat-row > :global(.chat-view) {
		flex: 1 1 0;
		min-width: 0;
	}

	.feature-panel {
		flex: 1;
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
	}

	/* Kept-alive but off-screen panel: no layout, no paint, not focusable. */
	.feature-panel.is-inactive {
		display: none;
	}

	/* Feature panels rendered as cards need to fill the space */
	.feature-panel :global(> *) {
		flex: 1;
		height: 100%;
	}
</style>

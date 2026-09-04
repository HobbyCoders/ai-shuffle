<script lang="ts">
	/**
	 * Main Page - Sidebar + Main Panel AI Workspace
	 *
	 * Codex-style layout with:
	 * - Collapsible sidebar with session list and navigation
	 * - Single active view in the main panel (chat or feature page)
	 * - SpotlightSearch (Cmd+K) for quick navigation
	 * - Full keyboard shortcut support
	 *
	 * Session modes (code / chat / voice) are NOT threaded from here. AppLayout
	 * and AppTopBar pass Sidebar's props one by one, so rather than widen two
	 * intermediaries for a single store write, Sidebar sets `appMode` itself and
	 * then calls the `onNewSession` it already receives — which is exactly the
	 * "switching lanes lands you on that lane's compose screen" behaviour. What
	 * this page still owns is the reverse direction: any path that opens or
	 * creates a session has to put the sidepanel in the matching lane, which is
	 * what handleOpenSession and handleNewSessionForProject do below.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { isAuthenticated, isAdmin } from '$lib/stores/auth.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { appMode, normalizeSessionMode } from '$lib/stores/sessionMode.svelte';
	import { appData, profiles, projects, sessions, sessionsTagFilter } from '$lib/stores/app-data.svelte';

	import { layout, activeView, sidebarCollapsed, sidebarOverlay } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { registerShortcut, type ShortcutRegistration } from '$lib/services/keyboard';
	import { getTags, type Tag } from '$lib/api/client';

	// Layout components
	import AppLayout from '$lib/components/layout/AppLayout.svelte';
	import ActiveVoiceCallBar from '$lib/components/voice/ActiveVoiceCallBar.svelte';

	// Modals
	import KeyboardShortcutsModal from '$lib/components/KeyboardShortcutsModal.svelte';
	import ImportSessionModal from '$lib/components/ImportSessionModal.svelte';
	import TagManager from '$lib/components/TagManager.svelte';
	import KnowledgeManager from '$lib/components/KnowledgeManager.svelte';
	import SpotlightSearch from '$lib/components/SpotlightSearch.svelte';
	import { initGlobalStt } from '$lib/services/globalStt';

	// ============================================
	// State: Modals
	// ============================================
	let showKeyboardShortcuts = $state(false);
	let showImportModal = $state(false);
	let showTagManager = $state(false);
	let showKnowledgeModal = $state(false);
	let showSpotlight = $state(false);

	// Tag state
	let allTags: Tag[] = $state([]);

	// Shortcut registrations for cleanup
	let shortcutRegistrations: ShortcutRegistration[] = [];

	// Git repository loading is handled by MainPanel's $effect, which supports
	// both project mode and worktree mode. No duplicate load needed here.

	// ============================================
	// Navigation Handlers
	// ============================================

	/**
	 * Create a new chat session and navigate to it
	 */
	function handleNewSession() {
		activeSession.newChat();

		if ($sidebarOverlay && !$sidebarCollapsed) {
			layout.setSidebarCollapsed(true);
			requestAnimationFrame(() => layout.openChat(null));
		} else {
			layout.openChat(null);
		}
	}

	/**
	 * Create a new chat session with a specific project pre-selected.
	 *
	 * Only the projects panel calls this, and a project only means something in
	 * the code lane — chat and voice sessions are projectless. So the lane is
	 * forced first: picking a project out of a chat-lane sidepanel would
	 * otherwise create a session the list beside it cannot show.
	 */
	function handleNewSessionForProject(projectId: string) {
		appMode.set('code');
		activeSession.newChat();
		activeSession.setProject(projectId);

		if ($sidebarOverlay && !$sidebarCollapsed) {
			layout.setSidebarCollapsed(true);
			requestAnimationFrame(() => layout.openChat(null));
		} else {
			layout.openChat(null);
		}
	}

	/**
	 * Open an existing session by ID
	 */
	function handleOpenSession(sessionId: string) {
		// Look up the session's project and worktree state so switchSession can
		// set them immediately, avoiding state bleed from the leaving session.
		const session = $sessions.find(s => s.id === sessionId);
		const sessionProjectId = session?.project_id;
		const sessionProjectV2Id = session?.project_v2_id ?? null;

		// Build worktree info from the session list data so the switcher can
		// use server-authoritative values instead of stale in-memory state.
		// A v2 branch session carries only `branch` (no legacy worktree_id/
		// worktree_branch); fall back to it so branch mode restores on switch.
		const worktreeInfo = session?.worktree_id || session?.worktree_branch || session?.branch ? {
			worktreeId: session.worktree_id ?? null,
			worktreeBranch: session.worktree_branch ?? session.branch ?? '',
			worktreeDeleted: session.worktree_deleted ?? false,
			worktreeIntent: 'worktree' as const,
		} : null;

		const sessionTitle = session?.title || null;

		// Follow the session into its lane. A deep link (/?session=<id>), a
		// notification tap or a spotlight hit can name a session from a lane the
		// sidepanel is not showing; without this the session opens in the main
		// panel while the list beside it does not contain it. Unknown/absent
		// `mode` normalizes to 'code', which is what every pre-modes row is.
		appMode.set(normalizeSessionMode(session?.mode));

		const openChat = () => {
			activeSession.switchSession(sessionId, sessionProjectId, worktreeInfo, sessionTitle, sessionProjectV2Id);
			layout.openChat(sessionId);
		};

		if ($sidebarOverlay && !$sidebarCollapsed) {
			layout.setSidebarCollapsed(true);
			requestAnimationFrame(openChat);
		} else {
			openChat();
		}
	}

	/**
	 * Open a session in response to the in-app notification bell's
	 * `open-session` event. Skip the switch when the session is already on screen so
	 * the click doesn't trigger a needless reload/flash.
	 */
	function handleNotificationOpenSession(sessionId: string | null | undefined) {
		if (!sessionId) return;
		const alreadyOpen =
			$activeView?.type === 'session' &&
			$activeView.sessionId === sessionId &&
			$activeSession?.sessionId === sessionId;
		if (alreadyOpen) return;
		handleOpenSession(sessionId);
	}

	/**
	 * Open a feature page (settings, files, plugins, etc.)
	 */
	function handleOpenFeature(type: ViewType, itemId?: string | null) {
		if (type === 'session') {
			handleNewSession();
			return;
		}

		if ($sidebarOverlay && !$sidebarCollapsed) {
			layout.setSidebarCollapsed(true);
			requestAnimationFrame(() => {
				layout.openFeaturePage(type as Exclude<ViewType, 'session'>, itemId);
			});
		} else {
			layout.openFeaturePage(type as Exclude<ViewType, 'session'>, itemId);
		}
	}


	// ============================================
	// Lifecycle
	// ============================================

	let visibilityChangeHandler: (() => void) | null = null;
	let focusHandler: (() => void) | null = null;
	let cleanupGlobalStt: (() => void) | null = null;
	let openSessionEventHandler: ((event: Event) => void) | null = null;

	// Cross-install freshness: active-state changes (and other mutations) land in
	// the shared DB from OTHER installs, but our board only reloads on mount +
	// local mutations — so an open board goes stale. Refresh it when the board
	// becomes visible / the window regains focus. Debounced so rapid focus flaps
	// (alt-tabbing) don't stampede the API.
	let _lastBoardRefresh = 0;
	const BOARD_REFRESH_MIN_INTERVAL_MS = 4000;
	function _refreshBoard() {
		const now = Date.now();
		if (now - _lastBoardRefresh < BOARD_REFRESH_MIN_INTERVAL_MS) return;
		_lastBoardRefresh = now;
		// Respect the current tag filter (mirrors the other loadSessions callers).
		void appData.loadSessions($sessionsTagFilter);
	}
	function refreshBoardIfVisible() {
		// The sidebar session list is always on screen on desktop, so refresh on
		// every visibility/focus return (still debounced by _refreshBoard).
		_refreshBoard();
	}

	onMount(async () => {
		if (!$isAuthenticated) {
			await goto('/login');
			return;
		}

		// Initialize data
		await activeSession.init();
		await Promise.all([
			appData.loadProfiles(),
			appData.loadSessions(),
			appData.loadProjects(),
			appData.loadClusterProjects(),
			appData.loadApiUserSessions(),
			loadAllTags()
		]);
		// Sessions were just loaded — seed the board-refresh debounce so the first
		// board render / focus event doesn't immediately refetch the same list.
		_lastBoardRefresh = Date.now();

		// Persisted profile may point to an ID that no longer exists
		// (deleted on another device / tab). Reconcile against the freshly
		// loaded profiles list before any new session is created.
		activeSession.validateSelectedProfile(appData.current.profiles);

		// Same reconciliation for the restored session's LANE. init() adopts a
		// sessionId from a per-tab snapshot before any row exists, so this is the
		// first moment the row — the authority for which lane a session belongs
		// to — can be consulted.
		const restoredMode = activeSession.reconcileSessionMode(appData.current.sessions);

		// Sync layout.activeView with the restored session from activeSession.init().
		// These two stores persist independently (localStorage vs backend API) and can
		// diverge. Generally the backend-persisted session is authoritative, EXCEPT
		// when the layout explicitly says "new chat" (sessionId: null). The layout
		// uses synchronous localStorage, while the backend uses an async PUT that
		// may not complete before a page reload. When the user clicks "New Chat",
		// the layout immediately persists null, but the backend may still have the
		// old session. In that case, the layout's explicit "new chat" intent wins.
		const restoredSessionId = $activeSession?.sessionId ?? null;
		const currentViewSessionId = $activeView?.type === 'session' ? ($activeView.sessionId ?? null) : null;

		// Pull the sidepanel over to the restored session's lane, so a reload never
		// leaves the open session missing from the list beside it (the alignment
		// handleOpenSession does for a click). Only on the branches that actually
		// OPEN it: where the layout's explicit New Chat wins below, the lane the
		// user last chose is the one that compose belongs in.
		const followRestoredLane = () => { if (restoredMode) appMode.set(restoredMode); };

		if (!$activeView) {
			// No persisted layout view — open the restored session or a new chat
			if (restoredSessionId) {
				followRestoredLane();
				layout.openChat(restoredSessionId);
			} else {
				handleNewSession();
			}
		} else if ($activeView.type === 'session' && currentViewSessionId === null && restoredSessionId) {
			// Layout says "new chat" but backend has a session — the user explicitly
			// clicked New Chat (layout persists synchronously) but the async backend
			// persist may not have completed. Respect the layout's explicit intent.
			activeSession.newChat();
		} else if ($activeView.type === 'session' && currentViewSessionId !== restoredSessionId) {
			// Layout view and active session disagree — backend is authoritative
			followRestoredLane();
			layout.openChat(restoredSessionId);
		} else {
			// Layout and the restored session already agree (the ordinary reload).
			followRestoredLane();
		}

		// Deep-link: the Android shell opens `/?session=<id>` when a notification
		// is tapped. Open that session explicitly — it outranks the restored
		// session above (it's a fresh user intent) — then strip the param so a
		// refresh doesn't re-trigger it. Shared-DB cluster sessions appear in
		// $sessions on every install, so handleOpenSession resolves them and (when
		// the session is owned by another device) relays exactly like a click in
		// the Sessions board.
		const deepLinkSessionId = new URLSearchParams(window.location.search).get('session');
		if (deepLinkSessionId) {
			handleOpenSession(deepLinkSessionId);
			const url = new URL(window.location.href);
			url.searchParams.delete('session');
			history.replaceState(history.state, '', url.pathname + url.search + url.hash);
		}

		// The in-app notification bell opens the relevant session without a reload.
		openSessionEventHandler = (event: Event) => {
			handleNotificationOpenSession((event as CustomEvent).detail?.sessionId);
		};
		window.addEventListener('open-session', openSessionEventHandler);

		// Sync from server when window regains focus / the tab becomes visible.
		// Keeps the sessions board fresh with cross-install mutations (e.g. a
		// session marked active on another install) that our board otherwise never
		// picks up until a manual reload.
		visibilityChangeHandler = () => {
			if (document.visibilityState === 'visible') {
				refreshBoardIfVisible();
			}
		};
		document.addEventListener('visibilitychange', visibilityChangeHandler);
		focusHandler = () => refreshBoardIfVisible();
		window.addEventListener('focus', focusHandler);

		// Register keyboard shortcuts
		shortcutRegistrations = [
			registerShortcut({
				id: 'spotlight',
				description: 'Open spotlight search',
				key: 'k',
				cmdOrCtrl: true,
				category: 'navigation',
				action: () => { showSpotlight = !showSpotlight; }
			}),
			registerShortcut({
				id: 'new-chat',
				description: 'New chat session',
				key: 'n',
				cmdOrCtrl: true,
				category: 'chat',
				action: () => handleNewSession()
			}),
			registerShortcut({
				id: 'show-shortcuts',
				description: 'Show keyboard shortcuts',
				key: '/',
				cmdOrCtrl: true,
				category: 'general',
				action: () => { showKeyboardShortcuts = !showKeyboardShortcuts; }
			}),
			registerShortcut({
				id: 'show-shortcuts-question',
				description: 'Show keyboard shortcuts',
				key: '?',
				category: 'general',
				action: () => { showKeyboardShortcuts = true; }
			}),
			registerShortcut({
				id: 'close-modal',
				description: 'Close modal',
				key: 'Escape',
				category: 'general',
				allowInInput: true,
				action: () => {
					if (showKeyboardShortcuts) showKeyboardShortcuts = false;
					else if (showSpotlight) showSpotlight = false;
					else if (showImportModal) showImportModal = false;
					else if (showKnowledgeModal) showKnowledgeModal = false;
					else if (showTagManager) showTagManager = false;
				}
			}),
			registerShortcut({
				id: 'open-settings',
				description: 'Open settings',
				key: ',',
				cmdOrCtrl: true,
				category: 'navigation',
				action: () => handleOpenFeature('settings')
			}),
			registerShortcut({
				id: 'open-profiles',
				description: 'Open agents',
				key: 'p',
				cmdOrCtrl: true,
				shift: true,
				category: 'navigation',
				action: () => handleOpenFeature('profile')
			}),
			registerShortcut({
				id: 'advanced-search',
				description: 'Search everything',
				key: 'f',
				cmdOrCtrl: true,
				shift: true,
				category: 'navigation',
				action: () => { showSpotlight = !showSpotlight; }
			})
		];

		// Initialize global STT
		cleanupGlobalStt = initGlobalStt();
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			if (visibilityChangeHandler) {
				document.removeEventListener('visibilitychange', visibilityChangeHandler);
			}
			if (focusHandler) {
				window.removeEventListener('focus', focusHandler);
			}
			if (openSessionEventHandler) {
				window.removeEventListener('open-session', openSessionEventHandler);
			}
		}
		shortcutRegistrations.forEach((reg) => reg.unregister());
		shortcutRegistrations = [];
		activeSession.destroy();
		if (cleanupGlobalStt) cleanupGlobalStt();
	});

	// ============================================
	// Data Loading
	// ============================================
	async function loadAllTags() {
		try {
			allTags = await getTags();
		} catch (e) {
			console.error('Failed to load tags:', e);
		}
	}

	// ============================================
	// Spotlight Search Handlers
	// ============================================
	function handleSpotlightSelectSession(session: { id: string }) {
		handleOpenSession(session.id);
		showSpotlight = false;
	}

	function handleSpotlightSelectAgent(id: string) {
		handleOpenFeature('profile', id);
		showSpotlight = false;
	}

	function handleSpotlightNewChat() {
		handleNewSession();
		showSpotlight = false;
	}

	function handleSpotlightOpenSettings() {
		handleOpenFeature('settings');
		showSpotlight = false;
	}

	// ============================================
	// Import Session Handlers
	// ============================================
	async function handleImportSuccess(data: { sessionId: string; title: string; messageCount: number }) {
		const { sessionId } = data;
		await appData.loadSessions();
		handleOpenSession(sessionId);
		showImportModal = false;
	}

	// ============================================
	// Tag Manager Handlers
	// ============================================
	async function handleTagManagerClose() {
		showTagManager = false;
		await loadAllTags();
		await appData.loadSessions($sessionsTagFilter);
	}
</script>

<svelte:head>
	<title>AI Shuffle</title>
</svelte:head>

{#if !$isAuthenticated}
	<div class="flex items-center justify-center h-screen bg-background">
		<p class="text-muted-foreground">Redirecting to login...</p>
	</div>
{:else}
	<ActiveVoiceCallBar onReturn={handleOpenSession} />
	<!-- Main Layout: Sidebar + Main Panel -->
	<AppLayout
		isAdmin={$isAdmin}
		onNewSession={handleNewSession}
		onSettledOpenSession={handleNewSession}
		onNewSessionForProject={handleNewSessionForProject}
		onOpenSession={handleOpenSession}
		onOpenFeature={handleOpenFeature}
		onOpenSpotlight={() => showSpotlight = true}
		onOpenPlugins={() => handleOpenFeature('plugins')}
	/>

	<!-- ========================================== -->
	<!-- MODALS -->
	<!-- ========================================== -->

	<!-- Spotlight Search (Cmd+K) -->
	<SpotlightSearch
		visible={showSpotlight}
		sessions={$sessions}
		currentProjectId={$activeSession?.project}
		onClose={() => showSpotlight = false}
		onSelectSession={handleSpotlightSelectSession}
		onSelectAgent={handleSpotlightSelectAgent}
		onNewChat={handleSpotlightNewChat}
		onOpenSettings={handleSpotlightOpenSettings}
	/>

	<!-- Keyboard Shortcuts Modal -->
	<KeyboardShortcutsModal open={showKeyboardShortcuts} onClose={() => showKeyboardShortcuts = false} />

	<!-- Import Session Modal -->
	<ImportSessionModal
		bind:show={showImportModal}
		onclose={() => showImportModal = false}
		onimported={handleImportSuccess}
	/>

	<!-- Tag Manager Modal -->
	{#if showTagManager}
		<TagManager
			onclose={handleTagManagerClose}
			ontagsupdated={() => loadAllTags()}
		/>
	{/if}

	<!-- Knowledge Base Modal -->
	{#if showKnowledgeModal && $activeSession?.project}
		<KnowledgeManager
			projectId={$activeSession.project}
			onclose={() => showKnowledgeModal = false}
		/>
	{/if}

	<!-- Global STT Indicator -->
{/if}

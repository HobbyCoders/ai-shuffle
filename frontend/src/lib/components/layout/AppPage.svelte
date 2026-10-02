<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	import { resourceSync } from '$lib/services/resource-sync';
	/**
	 * AppPage - the authenticated workspace: data bootstrap, keyboard
	 * shortcuts, deep links, the global modals — and a `shell` snippet that
	 * decides which chrome wraps MainPanel. The worker web UI renders the
	 * desktop AppLayout there; the mobile app (mobile/) renders its own
	 * top-bar + drawer layout. Everything else on this page is shared.
	 *
	 * Codex-style layout with:
	 * - Collapsible sidebar with session list and navigation
	 * - Single active view in the main panel (chat or feature page)
	 * - SpotlightSearch (Cmd+K) for quick navigation
	 * - Full keyboard shortcut support
	 *
	 * Session modes still determine tool capabilities, not separate histories.
	 * Text and calls share existing sessions. Sidebar owns the new-conversation
	 * capability picker; ordinary navigation continues through handleOpenSession.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { isAuthenticated, isAdmin } from '$lib/stores/auth.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { appMode } from '$lib/stores/sessionMode.svelte';
	import { hasComposerDraft } from '$lib/stores/session/composer-suggestion';
	import { openSession as handleOpenSession } from '$lib/services/session-navigation';
	import { appData, profiles, projects, sessions, sessionsTagFilter } from '$lib/stores/app-data.svelte';

	import { layout, activeView, sidebarCollapsed, sidebarOverlay } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { registerShortcut, type ShortcutRegistration } from '$lib/services/keyboard';
	import { getTags, type Tag } from '$lib/api/client';

	// Layout components
	import type { Snippet } from 'svelte';
	import type { AppShellProps } from './app-shell';
	import ActiveVoiceCallBar from '$lib/components/voice/ActiveVoiceCallBar.svelte';
	import GlobalVoiceInput from '$lib/components/voice/GlobalVoiceInput.svelte';

	// Modals
	import KeyboardShortcutsModal from '$lib/components/KeyboardShortcutsModal.svelte';
	import ImportSessionModal from '$lib/components/ImportSessionModal.svelte';
	import TagManager from '$lib/components/TagManager.svelte';
	import SpotlightSearch from '$lib/components/SpotlightSearch.svelte';
	import { initGlobalStt } from '$lib/services/globalStt';

	interface Props {
		/** Renders the app shell (sidebar/top bar chrome) around MainPanel. */
		shell: Snippet<[AppShellProps]>;
	}
	let { shell }: Props = $props();

	// ============================================
	// State: Modals
	// ============================================
	let showKeyboardShortcuts = $state(false);
	let showImportModal = $state(false);
	let showTagManager = $state(false);
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
	let navigationClaimed = false;
	function handleNewSession() {
		navigationClaimed = true;
		activeSession.newChat();

		if ($sidebarOverlay && !$sidebarCollapsed) {
			layout.setSidebarCollapsed(true);
			requestAnimationFrame(() => layout.openChat(null));
		} else {
			layout.openChat(null);
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
		navigationClaimed = true;
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
		// Join the root visibility/reconnect queue instead of racing its load.
		resourceSync.invalidate(['sessions']);
	}
	function refreshBoardIfVisible() {
		// The sidebar session list is always on screen on desktop, so refresh on
		// every visibility/focus return (still debounced by _refreshBoard).
		_refreshBoard();
	}

	onMount(async () => {
		// The root layout owns auth routing and delays this mount until verified.
		if (!$isAuthenticated) return;

		// Initialize data
		await activeSession.init();
		// The layout's explicit New Chat wins over an older backend session
		// snapshot (see below). Settle that now, not after the loads: until then
		// the composer was bound to the old session, so a send went there and the
		// late newChat then wiped it from view (#1636).
		const layoutNewChatWon = !navigationClaimed && !!$activeSession?.sessionId &&
			$activeView?.type === 'session' && ($activeView.sessionId ?? null) === null;
		if (layoutNewChatWon) activeSession.newChat();
		// That New Chat's remembered project and branch resolve against the
		// project catalog, so apply them as soon as it is in.
		const catalogLoaded = Promise.all([appData.loadProjects(), appData.loadClusterProjects()])
			.then(() => { if (layoutNewChatWon) activeSession.applyComposerDefaults(); });
		await Promise.all([
			appData.loadProfiles(),
			appData.loadSessions(),
			catalogLoaded,
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
		const followRestoredLane = () => { if (restoredMode) appMode.set(restoredMode, false); };

		// A persisted empty composer is also an empty landing, not a selected work
		// session. Default to Assistant once at startup, never as a reactive redirect:
		// subsequent New conversation actions, drafts and restored sessions win.
		// A deep link (see below) is an explicit destination; read it first so the
		// landing never mounts Assistant only to be replaced a moment later.
		const deepLinkSessionId = new URLSearchParams(window.location.search).get('session');
		const hasDraft = hasComposerDraft($activeSession.draft);
		const emptyOwnerLanding = $isAdmin && !navigationClaimed && !layoutNewChatWon && !restoredSessionId && !hasDraft && !deepLinkSessionId &&
			(!$activeView || ($activeView.type === 'session' && currentViewSessionId === null));
		if (emptyOwnerLanding) {
			layout.openFeaturePage('assistant');
		} else if (!$activeView) {
			if (restoredSessionId) {
				followRestoredLane();
				layout.openChat(restoredSessionId);
			} else if (hasDraft) {
				// Do not clear a surviving draft just because the layout snapshot is absent.
				layout.openChat(null);
			} else {
				handleNewSession();
			}
		} else if ($activeView.type === 'session' && currentViewSessionId === null && (restoredSessionId || layoutNewChatWon)) {
			// Layout says "new chat" but backend has a session — the user explicitly
			// clicked New Chat (layout persists synchronously) but the async backend
			// persist may not have completed. Respect the layout's explicit intent.
			// When that was settled right after init, a session id here is one a
			// send made during the loads, and it stays.
			if (!layoutNewChatWon) activeSession.newChat();
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
	watchResources(["tags"], () => loadAllTags());
</script>

<svelte:head>
	<title>AI Shuffle</title>
</svelte:head>

{#if !$isAuthenticated}
	<div class="flex items-center justify-center h-screen bg-background">
		<p class="text-muted-foreground">Checking connection...</p>
	</div>
{:else}
	<ActiveVoiceCallBar onReturn={handleOpenSession} />
	<!-- App shell (desktop AppLayout or the mobile app's layout) around MainPanel -->
	{@render shell({
		isAdmin: $isAdmin,
		onNewSession: handleNewSession,
		onSettledOpenSession: handleNewSession,
		onOpenSession: handleOpenSession,
		onOpenFeature: handleOpenFeature,
		onOpenSpotlight: () => { showSpotlight = true; }
	})}

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
		onOpenFeature={handleOpenFeature}
		onNewChat={handleSpotlightNewChat}
		onOpenSettings={handleSpotlightOpenSettings}
		onImportSession={() => { showSpotlight = false; showImportModal = true; }}
		onManageTags={() => { showSpotlight = false; showTagManager = true; }}
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

	<!-- Global STT Indicator -->
	<GlobalVoiceInput />
{/if}

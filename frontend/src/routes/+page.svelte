<script lang="ts">
	/**
	 * Main Page - Sidebar + Main Panel AI Workspace
	 *
	 * Codex-style layout with:
	 * - Collapsible sidebar with session list and navigation
	 * - Single active view in the main panel (chat or feature page)
	 * - SpotlightSearch (Cmd+K) for quick navigation
	 * - Full keyboard shortcut support
	 */
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { isAuthenticated, isAdmin } from '$lib/stores/auth.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { appData, profiles, projects, sessions, sessionsTagFilter } from '$lib/stores/app-data.svelte';

	import { layout, activeView, sidebarCollapsed, sidebarOverlay } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { registerShortcut, type ShortcutRegistration } from '$lib/services/keyboard';
	import { getTags, type Tag } from '$lib/api/client';
	import type { Command } from '$lib/api/commands';

	// Layout components
	import AppLayout from '$lib/components/layout/AppLayout.svelte';

	// Modals
	import KeyboardShortcutsModal from '$lib/components/KeyboardShortcutsModal.svelte';
	import ImportSessionModal from '$lib/components/ImportSessionModal.svelte';
	import TagManager from '$lib/components/TagManager.svelte';
	import KnowledgeManager from '$lib/components/KnowledgeManager.svelte';
	import SpotlightSearch from '$lib/components/SpotlightSearch.svelte';
	import AdvancedSearch from '$lib/components/AdvancedSearch.svelte';
	import GlobalSttIndicator from '$lib/components/GlobalSttIndicator.svelte';
	import { initGlobalStt } from '$lib/services/globalStt';
	import { requestPermissionIfNeeded } from '$lib/services/notifications';

	// ============================================
	// State: Modals
	// ============================================
	let showKeyboardShortcuts = $state(false);
	let showImportModal = $state(false);
	let showTagManager = $state(false);
	let showKnowledgeModal = $state(false);
	let showSpotlight = $state(false);
	let showAdvancedSearch = $state(false);

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
	 * Create a new chat session with a specific project pre-selected
	 */
	function handleNewSessionForProject(projectId: string) {
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

		// Build worktree info from the session list data so the switcher can
		// use server-authoritative values instead of stale in-memory state.
		const worktreeInfo = session?.worktree_id || session?.worktree_branch ? {
			worktreeId: session.worktree_id ?? null,
			worktreeBranch: session.worktree_branch ?? '',
			worktreeDeleted: session.worktree_deleted ?? false,
			executionMode: 'worktree' as const,
		} : null;

		const sessionTitle = session?.title || null;

		const openChat = () => {
			activeSession.switchSession(sessionId, sessionProjectId, worktreeInfo, sessionTitle);
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
	 * Open a feature page (settings, files, plugins, etc.)
	 */
	function handleOpenFeature(type: ViewType) {
		if (type === 'chat') {
			handleNewSession();
			return;
		}

		if ($sidebarOverlay && !$sidebarCollapsed) {
			layout.setSidebarCollapsed(true);
			requestAnimationFrame(() => {
				layout.openFeaturePage(type as Exclude<ViewType, 'chat'>);
			});
		} else {
			layout.openFeaturePage(type as Exclude<ViewType, 'chat'>);
		}
	}

	// ============================================
	// Lifecycle
	// ============================================

	let visibilityChangeHandler: (() => void) | null = null;
	let cleanupGlobalStt: (() => void) | null = null;

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
			appData.loadApiUserSessions(),
			loadAllTags()
		]);

		// Persisted profile may point to an ID that no longer exists
		// (deleted on another device / tab). Reconcile against the freshly
		// loaded profiles list before any new session is created.
		activeSession.validateSelectedProfile(appData.current.profiles);

		// Sync layout.activeView with the restored session from activeSession.init().
		// These two stores persist independently (localStorage vs backend API) and can
		// diverge. Generally the backend-persisted session is authoritative, EXCEPT
		// when the layout explicitly says "new chat" (sessionId: null). The layout
		// uses synchronous localStorage, while the backend uses an async PUT that
		// may not complete before a page reload. When the user clicks "New Chat",
		// the layout immediately persists null, but the backend may still have the
		// old session. In that case, the layout's explicit "new chat" intent wins.
		const restoredSessionId = $activeSession?.sessionId ?? null;
		const currentViewSessionId = $activeView?.type === 'chat' ? ($activeView.sessionId ?? null) : null;

		if (!$activeView) {
			// No persisted layout view — open the restored session or a new chat
			if (restoredSessionId) {
				layout.openChat(restoredSessionId);
			} else {
				handleNewSession();
			}
		} else if ($activeView.type === 'chat' && currentViewSessionId === null && restoredSessionId) {
			// Layout says "new chat" but backend has a session — the user explicitly
			// clicked New Chat (layout persists synchronously) but the async backend
			// persist may not have completed. Respect the layout's explicit intent.
			activeSession.newChat();
		} else if ($activeView.type === 'chat' && currentViewSessionId !== restoredSessionId) {
			// Layout view and active session disagree — backend is authoritative
			layout.openChat(restoredSessionId);
		}

		// Sync from server when window regains focus
		visibilityChangeHandler = () => {
			if (document.visibilityState === 'visible') {
				// Could sync layout state from server here
			}
		};
		document.addEventListener('visibilitychange', visibilityChangeHandler);

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
					else if (showAdvancedSearch) showAdvancedSearch = false;
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
				description: 'Open profiles',
				key: 'p',
				cmdOrCtrl: true,
				shift: true,
				category: 'navigation',
				action: () => handleOpenFeature('profile')
			}),
			registerShortcut({
				id: 'advanced-search',
				description: 'Advanced search',
				key: 'f',
				cmdOrCtrl: true,
				shift: true,
				category: 'navigation',
				action: () => { showAdvancedSearch = !showAdvancedSearch; }
			})
		];

		// Request notification permission if not yet determined (works on both desktop and mobile)
		// Fire-and-forget: non-critical, errors handled internally
		void requestPermissionIfNeeded();

		// Initialize global STT
		cleanupGlobalStt = initGlobalStt();
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			if (visibilityChangeHandler) {
				document.removeEventListener('visibilitychange', visibilityChangeHandler);
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

	function handleSpotlightSelectCommand(_command: Command) {
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
	<!-- Main Layout: Sidebar + Main Panel -->
	<AppLayout
		isAdmin={$isAdmin}
		onNewSession={handleNewSession}
		onNewSessionForProject={handleNewSessionForProject}
		onOpenSession={handleOpenSession}
		onOpenFeature={handleOpenFeature}
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
		onSelectCommand={handleSpotlightSelectCommand}
		onNewChat={handleSpotlightNewChat}
		onOpenSettings={handleSpotlightOpenSettings}
	/>

	<!-- Keyboard Shortcuts Modal -->
	<KeyboardShortcutsModal open={showKeyboardShortcuts} onClose={() => showKeyboardShortcuts = false} />

	<!-- Advanced Search Modal (Cmd+Shift+F) -->
	<AdvancedSearch
		visible={showAdvancedSearch}
		onClose={() => showAdvancedSearch = false}
		onSelectSession={(sessionId) => {
			handleOpenSession(sessionId);
			showAdvancedSearch = false;
		}}
	/>

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
	<GlobalSttIndicator />
{/if}

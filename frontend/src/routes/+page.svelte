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
	import { isAuthenticated, isAdmin } from '$lib/stores/auth';
	import { activeSession } from '$lib/stores/active-session';
	import { appData, profiles, projects, sessions, sessionsTagFilter } from '$lib/stores/app-data';

	import { layout, activeView, sidebarCollapsed, sidebarOverlay } from '$lib/stores/layout';
	import type { ViewType } from '$lib/stores/layout';
	import { registerShortcut, type ShortcutRegistration } from '$lib/services/keyboard';
	import { getTags, type Tag } from '$lib/api/client';
	import type { Command } from '$lib/api/commands';

	// Layout components
	import { AppLayout } from '$lib/components/layout';

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
	 * Open an existing session by ID
	 */
	function handleOpenSession(sessionId: string) {
		// Look up the session's project so switchSession can set it immediately,
		// avoiding a window where the old project's streaming/git state leaks.
		const sessionProjectId = $sessions.find(s => s.id === sessionId)?.project_id;

		const openChat = () => {
			activeSession.switchSession(sessionId, sessionProjectId);
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

		// If no active view, open a new chat
		if (!$activeView) {
			handleNewSession();
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

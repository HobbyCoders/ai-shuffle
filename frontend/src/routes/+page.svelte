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
	import { isAuthenticated, isAdmin, apiUser } from '$lib/stores/auth';
	import { credentials } from '$lib/stores/credentials';
	import {
		tabs,
		allTabs,
		activeTab,
		activeTabId,
		profiles,
		projects,
		sessions,
		sessionsTagFilter
	} from '$lib/stores/tabs';
	import { git } from '$lib/stores/git';
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

	// Track last loaded git project to avoid unnecessary reloads
	let lastGitProjectId: string | null = null;

	// Sync git repository data when active tab's project changes
	$effect(() => {
		const tab = $activeTab;
		const currentProjectId = tab?.project || null;

		if (currentProjectId !== lastGitProjectId) {
			lastGitProjectId = currentProjectId;
			if (currentProjectId) {
				git.loadRepository(currentProjectId);
			} else {
				git.clearRepository();
			}
		}
	});

	// ============================================
	// Navigation Handlers
	// ============================================

	/**
	 * Create a new chat session and navigate to it
	 */
	function handleNewSession() {
		const tabId = tabs.createTab();
		if ($sidebarOverlay && !$sidebarCollapsed) {
			layout.setSidebarCollapsed(true);
			requestAnimationFrame(() => layout.openChat(null, tabId));
		} else {
			layout.openChat(null, tabId);
		}
	}

	/**
	 * Open an existing session by ID
	 */
	function handleOpenSession(sessionId: string) {
		const existingTabId = tabs.findTabBySessionId(sessionId);
		const openChat = () => {
			if (existingTabId) {
				tabs.setActiveTab(existingTabId);
				layout.openChat(sessionId, existingTabId);
			} else {
				const tabId = tabs.openSession(sessionId);
				layout.openChat(sessionId, tabId);
			}
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
		await tabs.init();
		await Promise.all([
			tabs.loadProfiles(),
			tabs.loadSessions(),
			tabs.loadProjects(),
			loadAllTags()
		]);

		if ($isAdmin) {
			tabs.loadApiUsers();
			tabs.loadAdminSessions();
		}

		if ($apiUser) {
			credentials.load();
		}

		// If no active view, open a new chat
		if (!$activeView) {
			handleNewSession();
		} else if ($activeView.type === 'chat' && $activeView.tabId) {
			// Restore the tab if it exists
			const existingTab = $allTabs.find((t) => t.id === $activeView!.tabId);
			if (!existingTab) {
				// Tab no longer exists, create new
				handleNewSession();
			} else {
				tabs.setActiveTab($activeView.tabId);
			}
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
		tabs.destroy();
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
	async function handleImportSuccess(event: CustomEvent<{ sessionId: string; title: string; messageCount: number }>) {
		const { sessionId } = event.detail;
		await tabs.loadSessions();
		handleOpenSession(sessionId);
		showImportModal = false;
	}

	// ============================================
	// Tag Manager Handlers
	// ============================================
	async function handleTagManagerClose() {
		showTagManager = false;
		await loadAllTags();
		await tabs.loadSessions($sessionsTagFilter);
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
		currentProjectId={$activeTab?.project}
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
		show={showImportModal}
		on:close={() => showImportModal = false}
		on:imported={handleImportSuccess}
	/>

	<!-- Tag Manager Modal -->
	{#if showTagManager}
		<TagManager
			on:close={handleTagManagerClose}
			on:tagsUpdated={() => loadAllTags()}
		/>
	{/if}

	<!-- Knowledge Base Modal -->
	{#if showKnowledgeModal && $activeTab?.project}
		<KnowledgeManager
			projectId={$activeTab.project}
			on:close={() => showKnowledgeModal = false}
		/>
	{/if}

	<!-- Global STT Indicator -->
	<GlobalSttIndicator />
{/if}

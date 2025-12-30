<script lang="ts">
	/**
	 * Main Page - Deck-based AI Workspace
	 *
	 * A card-based desktop workspace combining:
	 * - DeckLayout with draggable, resizable cards (chat, agent, studio, terminal)
	 * - All modals from the original page (settings, profile, analytics, etc.)
	 * - SpotlightSearch (Cmd+K) for quick navigation
	 * - Full keyboard shortcut support
	 */
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { isAuthenticated, isAdmin } from '$lib/stores/auth';
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
	import {
		deck,
		allCards,
		focusedCardId,
		mobileActiveCardIndex,
		layoutMode,
		isShuffling,
		shufflingCards
	} from '$lib/stores/deck';
	import type { DeckCard, DeckCardType, LayoutMode } from '$lib/stores/deck';
	import { groups } from '$lib/stores/groups';
	import { git, branches as gitBranches, currentBranch, gitLoading } from '$lib/stores/git';
	import { registerShortcut, type ShortcutRegistration } from '$lib/services/keyboard';
	import { getTags, type Tag } from '$lib/api/client';
	import type { Command } from '$lib/api/commands';

	// Deck components
	import { DeckLayout } from '$lib/components/deck';
	import {
		Workspace,
		ChatCard,
		TerminalCard,
		MobileWorkspace,
		ProfileCard,
		ProjectCard,
		SubagentCard,
		SettingsCard,
		UserSettingsCard,
		CardShuffle,
		ImageStudioCard,
		ModelStudioCard,
		AudioStudioCard,
		FileBrowserCard,
		PluginManagerCard,
		StackedCardPreview
	} from '$lib/components/deck/cards';
	import type { CardType, DeckCard as CardsDeckCard } from '$lib/components/deck/cards/types';

	// Modals
	import KeyboardShortcutsModal from '$lib/components/KeyboardShortcutsModal.svelte';
	import AnalyticsModal from '$lib/components/AnalyticsModal.svelte';
	import TerminalModal from '$lib/components/TerminalModal.svelte';
	import ImportSessionModal from '$lib/components/ImportSessionModal.svelte';
	import GitModal from '$lib/components/git/GitModal.svelte';
	import TagManager from '$lib/components/TagManager.svelte';
	import KnowledgeManager from '$lib/components/KnowledgeManager.svelte';
	import Canvas from '$lib/components/canvas/Canvas.svelte';
	import SpotlightSearch from '$lib/components/SpotlightSearch.svelte';
	import AdvancedSearch from '$lib/components/AdvancedSearch.svelte';
	import CardDeckNavigator from '$lib/components/deck/CardDeckNavigator.svelte';

	// ============================================
	// State: Deck Layout
	// ============================================
	let isMobile = $state(false);
	let workspaceRef: Workspace | undefined = $state();

	// Track last loaded git project to avoid unnecessary reloads
	let lastGitProjectId: string | null = null;

	// Sync git repository data when active tab's project changes
	$effect(() => {
		const tab = $activeTab;
		const currentProjectId = tab?.project || null;

		// Only reload if project actually changed
		if (currentProjectId !== lastGitProjectId) {
			lastGitProjectId = currentProjectId;

			if (currentProjectId) {
				// Load git data for the new project
				git.loadRepository(currentProjectId);
			} else {
				// Clear git data when no project selected
				git.clearRepository();
			}
		}
	});

	// ============================================
	// State: Modals
	// ============================================
	let showKeyboardShortcuts = $state(false);
	let showAnalyticsModal = $state(false);
	let showTerminalModal = $state(false);
	let showImportModal = $state(false);
	let showGitModal = $state(false);
	let showTagManager = $state(false);
	let showKnowledgeModal = $state(false);
	let showCanvas = $state(false);
	let showSpotlight = $state(false);
	let showAdvancedSearch = $state(false);
	let showCardNavigator = $state(false);
	let cardNavigatorInitialView = $state<'main' | 'recent'>('main');

	// Reorder drag state (for sidebyside/tile layouts)
	let reorderDragCardId: string | null = $state(null);
	let reorderDragPosition: { x: number; y: number } | null = $state(null);
	let isReorderTransitioning = $state(false);
	let lastReorderTargetIndex = $state(-1); // Track last target to avoid redundant reorders
	let isProcessingReorder = $state(false); // Prevent re-entrant reorder calls

	// Terminal modal state
	let terminalCommand = $state('/resume');
	let terminalSessionId = $state('');

	// Tag state
	let allTags: Tag[] = $state([]);

	// Shortcut registrations for cleanup
	let shortcutRegistrations: ShortcutRegistration[] = [];

	// ============================================
	// Derived State: Cards for Workspace
	// ============================================
	const workspaceCards = $derived<CardsDeckCard[]>(
		$allCards.map((c) => ({
			id: c.id,
			type: mapDeckCardType(c.type),
			title: c.title,
			x: c.position.x,
			y: c.position.y,
			width: c.size.width,
			height: c.size.height,
			zIndex: c.zIndex,
			maximized: c.maximized,
			focused: c.id === $focusedCardId,
			snappedTo: mapSnapZone(c.snappedTo),
			restoreGeometry: c.savedPosition && c.savedSize
				? {
						x: c.savedPosition.x,
						y: c.savedPosition.y,
						width: c.savedSize.width,
						height: c.savedSize.height
					}
				: undefined,
			data: { dataId: c.dataId, meta: c.meta },
			createdAt: c.createdAt,
			updatedAt: c.createdAt
		}))
	);

	// ============================================
	// Helper Functions
	// ============================================
	function mapDeckCardType(type: DeckCardType): CardType {
		switch (type) {
			case 'chat':
				return 'chat';
			case 'terminal':
				return 'terminal';
			case 'settings':
				return 'settings';
			case 'user-settings':
				return 'user-settings';
			case 'profile':
				return 'profile';
			case 'subagent':
				return 'subagent';
			case 'project':
				return 'project';
			case 'image-studio':
				return 'image-studio';
			case 'model-studio':
				return 'model-studio';
			case 'audio-studio':
				return 'audio-studio';
			case 'file-browser':
				return 'file-browser';
			case 'plugins':
				return 'plugins';
			default:
				return 'chat';
		}
	}

	function mapSnapZone(_zone: DeckCard['snappedTo']): CardsDeckCard['snappedTo'] {
		// Snapping has been removed - always return undefined
		return undefined;
	}

	function getTabIdForCard(card: CardsDeckCard): string | null {
		const tabIdFromMeta = card.data?.meta?.tabId as string | undefined;
		if (tabIdFromMeta) {
			return tabIdFromMeta;
		}
		if (card.data?.dataId) {
			return tabs.findTabBySessionId(card.data.dataId as string);
		}
		return null;
	}

	// ============================================
	// Deck-Tabs Sync
	// ============================================
	/**
	 * Synchronize deck cards with tabs after initialization.
	 * For chat cards with stale/missing tab references, recreate the tabs.
	 */
	function syncDeckCardsWithTabs() {
		const currentTabs = $allTabs;

		// Use all cards from deck store
		const allDeckCards = $allCards;

		for (const card of allDeckCards) {
			if (card.type !== 'chat') continue;

			const tabId = card.meta?.tabId as string | undefined;
			if (!tabId) {
				// Card has no tabId - create a fresh tab
				const newTabId = tabs.createTab();
				deck.setCardMeta(card.id, { tabId: newTabId });
				continue;
			}

			// Check if the tab exists
			const existingTab = currentTabs.find((t) => t.id === tabId);
			if (!existingTab) {
				// Tab doesn't exist - we need to create it
				// If the card has a sessionId (dataId), load that session
				if (card.dataId) {
					const newTabId = tabs.openSession(card.dataId);
					// Update the card's meta with the new tabId
					deck.setCardMeta(card.id, { tabId: newTabId });
				} else {
					// New chat with no session - create a fresh tab
					const newTabId = tabs.createTab();
					deck.setCardMeta(card.id, { tabId: newTabId });
				}
			}
		}
	}

	// ============================================
	// Lifecycle
	// ============================================

	// Visibility change handler for cross-device sync
	let visibilityChangeHandler: (() => void) | null = null;

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

		// Sync deck state from server for cross-device sync
		await deck.syncFromServer();

		// Sync deck cards with tabs after initialization
		// This ensures chat cards have valid tab references
		syncDeckCardsWithTabs();

		// Setup responsive handling
		checkMobile();
		window.addEventListener('resize', checkMobile);
		window.addEventListener('resize', updateWorkspaceBounds);

		// Sync from server when window regains focus (user switching devices)
		visibilityChangeHandler = () => {
			if (document.visibilityState === 'visible') {
				deck.syncFromServer();
			}
		};
		document.addEventListener('visibilitychange', visibilityChangeHandler);

		// Initial workspace bounds
		updateWorkspaceBounds();

		// Register keyboard shortcuts
		shortcutRegistrations = [
			// Spotlight search (Cmd+K)
			registerShortcut({
				id: 'spotlight',
				description: 'Open spotlight search',
				key: 'k',
				cmdOrCtrl: true,
				category: 'navigation',
				action: () => { showSpotlight = !showSpotlight; }
			}),

			// New chat card (Cmd+N)
			registerShortcut({
				id: 'new-chat',
				description: 'New chat card',
				key: 'n',
				cmdOrCtrl: true,
				category: 'chat',
				action: () => { handleCreateCard('chat'); }
			}),

			// Terminal (Cmd+T)
			registerShortcut({
				id: 'new-terminal',
				description: 'New terminal',
				key: 't',
				cmdOrCtrl: true,
				category: 'general',
				action: () => { handleCreateCard('terminal'); }
			}),

			// Show keyboard shortcuts (Cmd+/)
			registerShortcut({
				id: 'show-shortcuts',
				description: 'Show keyboard shortcuts',
				key: '/',
				cmdOrCtrl: true,
				category: 'general',
				action: () => { showKeyboardShortcuts = !showKeyboardShortcuts; }
			}),

			// Show keyboard shortcuts (? key)
			registerShortcut({
				id: 'show-shortcuts-question',
				description: 'Show keyboard shortcuts',
				key: '?',
				category: 'general',
				action: () => { showKeyboardShortcuts = true; }
			}),

			// Close modals (Escape)
			registerShortcut({
				id: 'close-modal',
				description: 'Close modal',
				key: 'Escape',
				category: 'general',
				allowInInput: true,
				action: () => {
					if (showKeyboardShortcuts) {
						showKeyboardShortcuts = false;
					} else if (showAdvancedSearch) {
						showAdvancedSearch = false;
					} else if (showSpotlight) {
						showSpotlight = false;
					} else if (showImportModal) {
						showImportModal = false;
					} else if (showTerminalModal) {
						showTerminalModal = false;
					} else if (showGitModal) {
						showGitModal = false;
					} else if (showKnowledgeModal) {
						showKnowledgeModal = false;
					} else if (showCanvas) {
						showCanvas = false;
					} else if (showAnalyticsModal) {
						showAnalyticsModal = false;
					} else if (showTagManager) {
						showTagManager = false;
					}
				}
			}),

			// Open settings (Cmd+,)
			registerShortcut({
				id: 'open-settings',
				description: 'Open settings',
				key: ',',
				cmdOrCtrl: true,
				category: 'navigation',
				action: () => { handleCreateCard('settings'); }
			}),

			// Open profiles (Cmd+Shift+P)
			registerShortcut({
				id: 'open-profiles',
				description: 'Open profiles',
				key: 'p',
				cmdOrCtrl: true,
				shift: true,
				category: 'navigation',
				action: () => { handleCreateCard('profile'); }
			}),

			// Open subagents (Cmd+Shift+A)
			registerShortcut({
				id: 'open-subagents',
				description: 'Open subagents',
				key: 'a',
				cmdOrCtrl: true,
				shift: true,
				category: 'navigation',
				action: () => { handleCreateCard('subagent'); }
			}),

			// Open projects (Cmd+Shift+J)
			registerShortcut({
				id: 'open-projects',
				description: 'Open projects',
				key: 'j',
				cmdOrCtrl: true,
				shift: true,
				category: 'navigation',
				action: () => { handleCreateCard('project'); }
			}),

			// Advanced search (Cmd+Shift+F)
			registerShortcut({
				id: 'advanced-search',
				description: 'Advanced search',
				key: 'f',
				cmdOrCtrl: true,
				shift: true,
				category: 'navigation',
				action: () => { showAdvancedSearch = !showAdvancedSearch; }
			}),

			// Git modal (Cmd+Shift+G)
			registerShortcut({
				id: 'git-modal',
				description: 'Open Git & GitHub',
				key: 'g',
				cmdOrCtrl: true,
				shift: true,
				category: 'navigation',
				action: () => {
					if ($activeTab?.project) {
						showGitModal = !showGitModal;
					}
				}
			}),

			// Toggle Canvas (Cmd+Shift+C)
			registerShortcut({
				id: 'toggle-canvas',
				description: 'Toggle Canvas view',
				key: 'c',
				cmdOrCtrl: true,
				shift: true,
				category: 'navigation',
				action: () => { showCanvas = !showCanvas; }
			}),

			// Close focused card (Cmd+W)
			registerShortcut({
				id: 'close-card',
				description: 'Close focused card',
				key: 'w',
				cmdOrCtrl: true,
				category: 'general',
				action: () => {
					if ($focusedCardId) {
						handleCardClose($focusedCardId);
					}
				}
			}),

			// Focus mode: previous card (Left Arrow)
			registerShortcut({
				id: 'focus-prev',
				description: 'Previous card (focus mode)',
				key: 'ArrowLeft',
				category: 'navigation',
				action: () => {
					if ($layoutMode === 'focus') {
						handleFocusNavigate('prev');
					}
				}
			}),

			// Focus mode: next card (Right Arrow)
			registerShortcut({
				id: 'focus-next',
				description: 'Next card (focus mode)',
				key: 'ArrowRight',
				category: 'navigation',
				action: () => {
					if ($layoutMode === 'focus') {
						handleFocusNavigate('next');
					}
				}
			})
		];
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('resize', checkMobile);
			window.removeEventListener('resize', updateWorkspaceBounds);
			if (visibilityChangeHandler) {
				document.removeEventListener('visibilitychange', visibilityChangeHandler);
			}
		}
		shortcutRegistrations.forEach((reg) => reg.unregister());
		shortcutRegistrations = [];
		tabs.destroy();
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
	// Responsive Handling
	// ============================================
	function checkMobile() {
		isMobile = window.innerWidth < 640;
		deck.setIsMobile(isMobile);
	}

	function updateWorkspaceBounds() {
		const mainArea = document.querySelector('.workspace-container');
		if (mainArea) {
			const rect = mainArea.getBoundingClientRect();
			deck.setWorkspaceBounds(rect.width, rect.height);
		}
	}

	// ============================================
	// Mode & Navigation Handlers
	// ============================================
	/**
	 * Open a thread from the CardDeckNavigator
	 */
	function handleNavigatorOpenThread(sessionId: string) {
		// Check if this session is already open as a card
		const existingCard = deck.findCardByDataId(sessionId);
		if (existingCard) {
			// Focus the existing card
			deck.focusCard(existingCard.id);
		} else {
			// Open the session as a new card
			const tabId = tabs.openSession(sessionId);
			const sessionData = $sessions.find(s => s.id === sessionId);
			deck.addCard('chat', {
				title: sessionData?.title || 'Chat',
				dataId: sessionId,
				meta: { tabId }
			});
		}
	}

	// ============================================
	// Card Lifecycle Handlers
	// ============================================
	function handleCreateCard(type: CardType) {
		let deckCardType: DeckCardType;
		let title: string;
		let dataId: string | null = null;
		let meta: Record<string, unknown> = {};

		switch (type) {
			case 'chat': {
				deckCardType = 'chat';
				title = 'New Chat';
				const tabId = tabs.createTab();
				dataId = null;
				meta = { tabId };
				break;
			}
			case 'terminal':
				deckCardType = 'terminal';
				title = 'Terminal';
				break;
			case 'recent-sessions':
				// Open the card navigator directly to the recent sessions view
				cardNavigatorInitialView = 'recent';
				showCardNavigator = true;
				return; // Don't create a card
			case 'settings': {
				// Singleton - check if already open
				const existingSettings = $allCards.find(c => c.type === 'settings');
				if (existingSettings) {
					deck.focusCard(existingSettings.id);
					return;
				}
				deckCardType = 'settings';
				title = 'Settings';
				break;
			}
			case 'profile': {
				deckCardType = 'profile';
				title = 'Profiles';
				break;
			}
			case 'subagent': {
				// Singleton
				const existingSubagent = $allCards.find(c => c.type === 'subagent');
				if (existingSubagent) {
					deck.focusCard(existingSubagent.id);
					return;
				}
				deckCardType = 'subagent';
				title = 'Subagents';
				break;
			}
			case 'project': {
				// Singleton
				const existingProject = $allCards.find(c => c.type === 'project');
				if (existingProject) {
					deck.focusCard(existingProject.id);
					return;
				}
				deckCardType = 'project';
				title = 'Projects';
				break;
			}
			case 'user-settings': {
				// Singleton - only one user settings card at a time
				const existingUserSettings = $allCards.find(c => c.type === 'user-settings');
				if (existingUserSettings) {
					deck.focusCard(existingUserSettings.id);
					return;
				}
				deckCardType = 'user-settings';
				title = 'My Settings';
				break;
			}
			case 'image-studio': {
				deckCardType = 'image-studio';
				title = 'Image Studio';
				break;
			}
			case 'model-studio': {
				deckCardType = 'model-studio';
				title = '3D Models';
				break;
			}
			case 'audio-studio': {
				deckCardType = 'audio-studio';
				title = 'Audio Studio';
				break;
			}
			case 'file-browser': {
				deckCardType = 'file-browser';
				title = 'Files';
				break;
			}
			case 'plugins': {
				// Singleton - only one plugins card at a time
				const existingPlugins = $allCards.find(c => c.type === 'plugins');
				if (existingPlugins) {
					deck.focusCard(existingPlugins.id);
					return;
				}
				deckCardType = 'plugins';
				title = 'Plugins';
				break;
			}
			default:
				deckCardType = 'chat';
				title = 'New Card';
		}

		deck.addCard(deckCardType, { title, dataId, meta });
	}

	function handleCardFocus(id: string) {
		deck.focusCard(id);

		// If this is a chat card, sync the active tab
		// Note: deck.getCard returns the store's DeckCard type (meta at top level)
		// which is different from CardsDeckCard (meta under data)
		const storeCard = deck.getCard(id);
		if (storeCard?.type === 'chat') {
			// Get tabId from store card's meta (not data.meta)
			const tabId = (storeCard.meta?.tabId as string) ||
				(storeCard.dataId ? tabs.findTabBySessionId(storeCard.dataId) : null);
			if (tabId) {
				tabs.setActiveTab(tabId);
			}
		}
	}

	function handleCardMove(id: string, x: number, y: number) {
		const card = deck.getCard(id);
		if (!card || !workspaceRef) {
			deck.moveCard(id, x, y);
			return;
		}

		const { width, height } = card.size;

		// In managed grid layouts (sidebyside/tile/stack), use reorder behavior
		if ($layoutMode === 'sidebyside' || $layoutMode === 'tile' || $layoutMode === 'stack') {
			// In stack mode, don't allow reordering the main (focused) card
			if ($layoutMode === 'stack' && id === $focusedCardId) {
				return;
			}

			// Track drag position for the floating overlay (this is cheap, just local state)
			reorderDragCardId = id;
			reorderDragPosition = { x, y };

			// Prevent re-entrant calls during reorder processing
			if (isProcessingReorder) return;

			// Calculate target slot - only reorder if slot actually changed
			// This prevents calling reorderCard+applyLayout 60 times per second
			const targetIndex = deck.calculateSlotIndex(x, y);
			if (targetIndex !== lastReorderTargetIndex) {
				lastReorderTargetIndex = targetIndex;
				// Enable transitions for other cards to animate
				isReorderTransitioning = true;

				// Mark as processing and schedule reorder
				isProcessingReorder = true;
				try {
					deck.reorderCard(id, targetIndex);
				} finally {
					isProcessingReorder = false;
				}
			}
			return;
		}

		// Freeflow mode: Apply bounds clamping to prevent cards from going off-screen
		const clampedPos = workspaceRef.clampToBounds(x, y, width, height);

		// Update the card position
		deck.moveCard(id, clampedPos.x, clampedPos.y);
	}

	function handleCardResize(id: string, width: number, height: number) {
		deck.resizeCard(id, width, height);
	}

	function handleCardDragEnd(_id: string) {
		// In managed layouts, commit the reorder and clear drag state
		if ($layoutMode === 'sidebyside' || $layoutMode === 'tile' || $layoutMode === 'stack') {
			deck.commitReorder();
			reorderDragCardId = null;
			reorderDragPosition = null;
			lastReorderTargetIndex = -1; // Reset for next drag
			isProcessingReorder = false; // Ensure flag is cleared

			// Keep transitions briefly for snap animation, then disable
			setTimeout(() => {
				isReorderTransitioning = false;
			}, 250);
			return;
		}
	}

	function handleCardResizeEnd(_id: string) {
		// No-op: snapping has been removed
	}

	function handleCardMaximize(id: string) {
		deck.toggleMaximize(id);
	}

	function handleCardClose(id: string) {
		const card = deck.getCard(id);
		if (card?.meta?.tabId) {
			tabs.closeTab(card.meta.tabId as string);
		}
		deck.removeCard(id);
	}

	function handleCardTitleChange(id: string, title: string) {
		deck.setCardTitle(id, title);
	}

	function handleFork(_cardId: string, _sessionId: string, _messageIndex: number, _messageId: string) {
		// Fork functionality to be implemented
	}

	// ============================================
	// Layout Mode Handler
	// ============================================
	function handleLayoutModeChange(mode: LayoutMode) {
		deck.setLayoutMode(mode);
	}

	function handleFocusNavigate(direction: 'prev' | 'next') {
		deck.navigateFocusMode(direction);
	}

	/**
	 * Handle clicking a stacked card to shuffle it to the main position
	 * Triggers shuffle animation and swaps the cards
	 */
	function handleStackCardClick(cardId: string) {
		if ($layoutMode !== 'stack') return;
		if (cardId === $focusedCardId) return; // Already the main card

		const outgoingId = $focusedCardId;

		// Start shuffle animation
		deck.startShuffle(outgoingId, cardId);

		// Focus the new card (triggers layout recalculation)
		deck.focusCard(cardId);

		// End shuffle animation after it completes
		setTimeout(() => {
			deck.endShuffle();
		}, 350); // Match animation duration
	}

	// ============================================

	// ============================================
	// Spotlight Search Handlers
	// ============================================
	function handleSpotlightSelectSession(session: { id: string }) {
		// Create a card for this session
		const existingCard = deck.findCardByDataId(session.id);
		if (existingCard) {
			deck.focusCard(existingCard.id);
		} else {
			const tabId = tabs.openSession(session.id);
			const sessionData = $sessions.find((s) => s.id === session.id);
			deck.addCard('chat', {
				title: sessionData?.title || 'Chat',
				dataId: session.id,
				meta: { tabId }
			});
		}
		showSpotlight = false;
	}

	function handleSpotlightSelectCommand(command: Command) {
		// For interactive commands, open terminal modal
		if (command.type === 'interactive') {
			// Need an active session for terminal
			const activeCard = $allCards.find((c) => c.id === $focusedCardId);
			if (activeCard?.dataId) {
				terminalSessionId = activeCard.dataId;
				terminalCommand = `/${command.name}`;
				showTerminalModal = true;
			}
		}
		showSpotlight = false;
	}

	function handleSpotlightNewChat() {
		handleCreateCard('chat');
		showSpotlight = false;
	}

	function handleSpotlightOpenSettings() {
		handleCreateCard('settings');
		showSpotlight = false;
	}

	// ============================================
	// Terminal Modal Handlers
	// ============================================
	function closeTerminalModal() {
		showTerminalModal = false;
		terminalSessionId = '';
	}

	// ============================================
	// Import Session Handlers
	// ============================================
	async function handleImportSuccess(event: CustomEvent<{ sessionId: string; title: string; messageCount: number }>) {
		const { sessionId, title } = event.detail;
		await tabs.loadSessions();
		// Open the imported session as a card
		const tabId = tabs.openSession(sessionId);
		deck.addCard('chat', {
			title: title || 'Imported Chat',
			dataId: sessionId,
			meta: { tabId }
		});
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

{#if !$isAuthenticated}
	<div class="flex items-center justify-center h-screen bg-background">
		<p class="text-muted-foreground">Redirecting to login...</p>
	</div>
{:else}
	<!-- Main Deck Layout -->
	<DeckLayout
		onLogoClick={() => showCardNavigator = !showCardNavigator}
		hasOpenCards={workspaceCards.length > 0}
		layoutMode={$layoutMode}
		onLayoutModeChange={handleLayoutModeChange}
	>
		{#if isMobile}
				<!-- Mobile Workspace -->
				<MobileWorkspace
					cards={workspaceCards}
					activeCardIndex={$mobileActiveCardIndex}
					onCardChange={(index) => deck.setMobileActiveCardIndex(index)}
					onCloseCard={handleCardClose}
					onCreateCard={handleCreateCard}
					onOpenNavigator={() => showCardNavigator = true}
				>
					{#snippet children(card)}
						{@const tabId = getTabIdForCard(card)}
						{#if card.type === 'chat' && tabId}
							<ChatCard
								{card}
								{tabId}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
								onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
								onFork={(sessionId, messageIndex, messageId) =>
									handleFork(card.id, sessionId, messageIndex, messageId)
								}
							/>
						{:else if card.type === 'terminal'}
							<TerminalCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
								onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
							/>
						{:else if card.type === 'settings'}
							<SettingsCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
																onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
							/>
						{:else if card.type === 'user-settings'}
							<UserSettingsCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
								onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
							/>
						{:else if card.type === 'profile'}
							<ProfileCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
																onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
								onOpenPlugins={() => handleCreateCard('plugins')}
							/>
						{:else if card.type === 'subagent'}
							<SubagentCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
																onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
							/>
						{:else if card.type === 'project'}
							<ProjectCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
								onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
							/>
						{:else if card.type === 'image-studio'}
							<ImageStudioCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
								onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
							/>
						{:else if card.type === 'model-studio'}
							<ModelStudioCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
								onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
							/>
						{:else if card.type === 'audio-studio'}
							<AudioStudioCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
								onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
							/>
						{:else if card.type === 'file-browser'}
							<FileBrowserCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
								onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
							/>
						{:else if card.type === 'plugins'}
							<PluginManagerCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
								onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
							/>
						{:else}
							<div class="p-4">
								<p class="text-muted-foreground">Unknown card type</p>
							</div>
						{/if}
					{/snippet}
				</MobileWorkspace>
			{:else}
				<!-- Desktop Workspace with draggable cards -->
				<Workspace
					bind:this={workspaceRef}
					cards={workspaceCards}
					onCardFocus={handleCardFocus}
					onCardMove={handleCardMove}
					onCardResize={handleCardResize}
					onCreateCard={handleCreateCard}
					layoutMode={$layoutMode}
					onLayoutModeChange={handleLayoutModeChange}
					onFocusNavigate={handleFocusNavigate}
					focusedCardId={$focusedCardId}
				>
					{#snippet children()}
						<!-- Don't sort cards - z-index CSS handles visual stacking order -->
						<!-- Sorting would cause DOM reordering which resets scroll positions -->
						{#each workspaceCards as card (card.id)}
							{@const tabId = getTabIdForCard(card)}
							{@const isStackedCard = $layoutMode === 'stack' && card.id !== $focusedCardId}
							{@const isShufflingCard = $isShuffling && ($shufflingCards.outgoingId === card.id || $shufflingCards.incomingId === card.id)}
							<div
									class="card-wrapper"
									class:maximized={card.maximized}
									class:reorder-transitioning={isReorderTransitioning}
									class:is-reorder-dragging={reorderDragCardId === card.id}
									class:stacked-card={isStackedCard}
									class:main-card={$layoutMode === 'stack' && card.id === $focusedCardId}
									class:shuffling={isShufflingCard}
									style:position="absolute"
									style:left={card.maximized ? '0' : `${card.x}px`}
									style:top={card.maximized ? '0' : `${card.y}px`}
									style:width={card.maximized ? '100%' : `${card.width}px`}
									style:height={card.maximized ? '100%' : `${card.height}px`}
									style:z-index={card.zIndex}
								>
									<!-- In stack layout, show compact preview for stacked cards -->
									{#if isStackedCard}
										<StackedCardPreview
											{card}
											{tabId}
											onClick={() => handleStackCardClick(card.id)}
											onDragStart={(e) => {
												reorderDragCardId = card.id;
												reorderDragPosition = { x: e.clientX, y: e.clientY };
											}}
											onDragMove={(e) => handleCardMove(card.id, e.clientX, e.clientY)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											isDragging={reorderDragCardId === card.id}
										/>
									{:else if card.type === 'chat'}
										{#if tabId}
											<ChatCard
												{card}
												{tabId}
												onClose={() => handleCardClose(card.id)}
												onMaximize={() => handleCardMaximize(card.id)}
												onFocus={() => handleCardFocus(card.id)}
												onMove={(x, y) => handleCardMove(card.id, x, y)}
												onResize={(w, h) => handleCardResize(card.id, w, h)}
												onDragEnd={() => handleCardDragEnd(card.id)}
												onResizeEnd={() => handleCardResizeEnd(card.id)}
												onFork={(sessionId, messageIndex, messageId) =>
													handleFork(card.id, sessionId, messageIndex, messageId)
												}
											/>
										{:else}
											<div class="card-loading">
												<p>Initializing chat...</p>
											</div>
										{/if}
									{:else if card.type === 'terminal'}
										<TerminalCard
											{card}
											onClose={() => handleCardClose(card.id)}
																						onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else if card.type === 'settings'}
										<SettingsCard
											{card}
											onClose={() => handleCardClose(card.id)}
																						onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else if card.type === 'user-settings'}
										<UserSettingsCard
											{card}
											onClose={() => handleCardClose(card.id)}
											onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else if card.type === 'profile'}
										<ProfileCard
											{card}
											onClose={() => handleCardClose(card.id)}
																						onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
											onOpenPlugins={() => handleCreateCard('plugins')}
										/>
									{:else if card.type === 'subagent'}
										<SubagentCard
											{card}
											onClose={() => handleCardClose(card.id)}
																						onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else if card.type === 'project'}
										<ProjectCard
											{card}
											onClose={() => handleCardClose(card.id)}
											onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else if card.type === 'image-studio'}
										<ImageStudioCard
											{card}
											onClose={() => handleCardClose(card.id)}
											onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else if card.type === 'model-studio'}
										<ModelStudioCard
											{card}
											onClose={() => handleCardClose(card.id)}
											onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else if card.type === 'audio-studio'}
										<AudioStudioCard
											{card}
											onClose={() => handleCardClose(card.id)}
											onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else if card.type === 'file-browser'}
										<FileBrowserCard
											{card}
											onClose={() => handleCardClose(card.id)}
											onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else if card.type === 'plugins'}
										<PluginManagerCard
											{card}
											onClose={() => handleCardClose(card.id)}
											onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else}
										<!-- Other card types -->
										<div class="card-placeholder">
											<p class="text-muted-foreground">
												{card.type} card (coming soon)
											</p>
										</div>
									{/if}
								</div>
						{/each}

						<!-- Floating overlay for dragged card during reorder -->
						{#if reorderDragCardId && reorderDragPosition}
							{@const draggedCard = workspaceCards.find(c => c.id === reorderDragCardId)}
							{#if draggedCard}
								<div
									class="reorder-drag-overlay"
									style:left="{reorderDragPosition.x}px"
									style:top="{reorderDragPosition.y}px"
									style:width="{draggedCard.width}px"
									style:height="{draggedCard.height}px"
								>
									<!-- Simplified preview of the card -->
									<div class="h-full w-full flex flex-col rounded-xl overflow-hidden border border-primary/30 bg-card/95 backdrop-blur-md">
										<div class="h-11 flex items-center gap-2 px-3 bg-secondary/80 border-b border-border">
											<span class="text-sm font-medium text-foreground truncate">{draggedCard.title}</span>
										</div>
										<div class="flex-1 bg-card/50"></div>
									</div>
								</div>
							{/if}
						{/if}
					{/snippet}
				</Workspace>
		{/if}
	</DeckLayout>

	<!-- ========================================== -->
	<!-- MODALS -->
	<!-- ========================================== -->

	<!-- Card Deck Navigator (AI Shuffle themed) -->
	<CardDeckNavigator
		open={showCardNavigator}
		onClose={() => {
			showCardNavigator = false;
			cardNavigatorInitialView = 'main'; // Reset for next open
		}}
		onCreateChat={() => handleCreateCard('chat')}
		onCreateTerminal={() => handleCreateCard('terminal')}
		onOpenThread={handleNavigatorOpenThread}
		onOpenImageStudio={() => handleCreateCard('image-studio')}
		onOpenModelStudio={() => handleCreateCard('model-studio')}
		onOpenAudioStudio={() => handleCreateCard('audio-studio')}
		onOpenFileBrowser={() => handleCreateCard('file-browser')}
		onOpenProjects={() => handleCreateCard('project')}
		onOpenProfiles={() => handleCreateCard('profile')}
		onOpenSubagents={() => handleCreateCard('subagent')}
		onOpenSettings={() => handleCreateCard('settings')}
		onOpenPlugins={() => handleCreateCard('plugins')}
		isAdmin={$isAdmin}
		{isMobile}
		initialView={cardNavigatorInitialView}
	/>


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

	<!-- Analytics Modal -->
	<AnalyticsModal
		open={showAnalyticsModal}
		onClose={() => showAnalyticsModal = false}
		onSessionClick={(sessionId) => {
			const tabId = tabs.openSession(sessionId);
			const sessionData = $sessions.find((s) => s.id === sessionId);
			deck.addCard('chat', {
				title: sessionData?.title || 'Chat',
				dataId: sessionId,
				meta: { tabId }
			});
			showAnalyticsModal = false;
		}}
	/>

	<!-- Advanced Search Modal (Cmd+Shift+F) -->
	<AdvancedSearch
		visible={showAdvancedSearch}
		onClose={() => showAdvancedSearch = false}
		onSelectSession={(sessionId) => {
			const tabId = tabs.openSession(sessionId);
			const sessionData = $sessions.find((s) => s.id === sessionId);
			deck.addCard('chat', {
				title: sessionData?.title || 'Chat',
				dataId: sessionId,
				meta: { tabId }
			});
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

	<!-- Git Modal -->
	{#if showGitModal && $activeTab?.project}
		<GitModal
			open={showGitModal}
			projectId={$activeTab.project}
			onClose={() => showGitModal = false}
			onOpenSession={(sessionId) => {
				const tabId = tabs.openSession(sessionId);
				const sessionData = $sessions.find((s) => s.id === sessionId);
				deck.addCard('chat', {
					title: sessionData?.title || 'Chat',
					dataId: sessionId,
					meta: { tabId }
				});
				showGitModal = false;
			}}
		/>
	{/if}

	<!-- Canvas Modal (full-screen overlay) -->
	{#if showCanvas}
		<div class="fixed inset-0 z-50 bg-background">
			<Canvas on:close={() => showCanvas = false} />
		</div>
	{/if}

	<!-- Terminal Modal for interactive CLI commands -->
	{#if showTerminalModal && terminalSessionId}
		<TerminalModal
			sessionId={terminalSessionId}
			command={terminalCommand}
			onClose={closeTerminalModal}
		/>
	{/if}

{/if}

<style>
	.card-wrapper {
		pointer-events: auto;
	}

	.card-wrapper.maximized {
		position: absolute !important;
		inset: 0 !important;
		width: 100% !important;
		height: 100% !important;
	}

	/* Smooth transitions during reorder drag for non-dragged cards */
	.card-wrapper.reorder-transitioning:not(.is-reorder-dragging) {
		transition: left 200ms cubic-bezier(0.4, 0, 0.2, 1),
					top 200ms cubic-bezier(0.4, 0, 0.2, 1);
	}

	/* Hide the original card during reorder drag - the overlay shows it */
	.card-wrapper.is-reorder-dragging {
		opacity: 0;
		pointer-events: none;
	}

	/* Floating drag overlay for reorder */
	.reorder-drag-overlay {
		position: absolute;
		pointer-events: none;
		z-index: 10000;
		opacity: 0.9;
		transform: scale(1.02);
		box-shadow: 0 20px 40px rgba(0, 0, 0, 0.3),
					0 0 0 2px hsl(var(--primary) / 0.5);
		border-radius: 12px;
		overflow: hidden;
	}

	/* ========================================
	   Stack Layout: Shuffle Animation
	   ======================================== */

	/* Stacked cards in stack layout */
	.card-wrapper.stacked-card {
		cursor: pointer;
	}

	/* Main card in stack layout */
	.card-wrapper.main-card {
		/* Main card has normal appearance */
	}

	/* Shuffle animation - smooth position/size transition */
	.card-wrapper.shuffling {
		transition:
			left 350ms cubic-bezier(0.34, 1.56, 0.64, 1),
			top 350ms cubic-bezier(0.34, 1.56, 0.64, 1),
			width 350ms cubic-bezier(0.34, 1.56, 0.64, 1),
			height 350ms cubic-bezier(0.34, 1.56, 0.64, 1),
			transform 350ms cubic-bezier(0.34, 1.56, 0.64, 1);
	}

	/* Main card shrinking during shuffle (going to stack) */
	.card-wrapper.main-card.shuffling {
		transform: scale(0.98);
	}

	/* Stacked card growing during shuffle (becoming main) */
	.card-wrapper.stacked-card.shuffling {
		transform: scale(1.02);
		z-index: 9999 !important; /* Ensure it's on top during animation */
	}

	/* Respect reduced motion preference */
	@media (prefers-reduced-motion: reduce) {
		.card-wrapper.shuffling {
			transition: none;
		}
	}

	.card-loading,
	.card-placeholder {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 100%;
		background: hsl(var(--card) / 0.8);
		backdrop-filter: blur(24px);
		-webkit-backdrop-filter: blur(24px);
		border-radius: 12px;
		border: 1px solid hsl(var(--border) / 0.5);
	}

	.card-loading p,
	.card-placeholder p {
		color: hsl(var(--muted-foreground));
		font-size: 0.875rem;
	}
</style>

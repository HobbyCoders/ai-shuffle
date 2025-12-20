<script lang="ts">
	/**
	 * Main Page - Deck-based AI Workspace
	 *
	 * A card-based desktop workspace combining:
	 * - DeckLayout with draggable, resizable cards (chat, agent, canvas, terminal)
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
		visibleCards,
		minimizedCards as minimizedCardsStore,
		focusedCardId,
		mobileActiveCardIndex,
		gridSnapEnabled
	} from '$lib/stores/deck';
	import type { DeckCard, DeckCardType } from '$lib/stores/deck';
	import { groups } from '$lib/stores/groups';
	import { registerShortcut, type ShortcutRegistration } from '$lib/services/keyboard';
	import { getTags, type Tag, type SessionTag, exportAgent, importAgent, parseAgentExportFile } from '$lib/api/client';
	import type { Command } from '$lib/api/commands';

	// Deck components
	import { DeckLayout } from '$lib/components/deck';
	import { AgentsView } from '$lib/components/deck/agents';
	import { StudioView } from '$lib/components/deck/studio';
	import {
		Workspace,
		ChatCard,
		AgentCard,
		CanvasCard,
		TerminalCard,
		MobileWorkspace
	} from '$lib/components/deck/cards';
	import type { CardType, DeckCard as CardsDeckCard } from '$lib/components/deck/cards/types';

	// Modals
	import SettingsModal from '$lib/components/SettingsModal.svelte';
	import ProfileModal from '$lib/components/ProfileModal.svelte';
	import KeyboardShortcutsModal from '$lib/components/KeyboardShortcutsModal.svelte';
	import AnalyticsModal from '$lib/components/AnalyticsModal.svelte';
	import TerminalModal from '$lib/components/TerminalModal.svelte';
	import ImportSessionModal from '$lib/components/ImportSessionModal.svelte';
	import AgentImportModal from '$lib/components/AgentImportModal.svelte';
	import GitModal from '$lib/components/git/GitModal.svelte';
	import TagManager from '$lib/components/TagManager.svelte';
	import KnowledgeManager from '$lib/components/KnowledgeManager.svelte';
	import Canvas from '$lib/components/canvas/Canvas.svelte';
	import SpotlightSearch from '$lib/components/SpotlightSearch.svelte';
	import SubagentManager from '$lib/components/SubagentManager.svelte';
	import AdvancedSearch from '$lib/components/AdvancedSearch.svelte';
	import { api } from '$lib/api/client';

	// Types from deck/types
	import type {
		ActivityMode,
		DeckSession,
		DeckAgent,
		DeckGeneration,
		MinimizedCard,
		RunningProcess
	} from '$lib/components/deck/types';

	// Subagent interface for profile config
	interface Subagent {
		id: string;
		name: string;
		description: string;
		model?: string;
		is_builtin?: boolean;
	}

	// Tool interfaces for profile config
	interface ToolInfo {
		name: string;
		category: string;
		description: string;
		mcp_server?: string;
	}

	interface ToolCategory {
		id: string;
		name: string;
		description: string;
		tools: ToolInfo[];
	}

	interface ToolsResponse {
		categories: ToolCategory[];
		all_tools: ToolInfo[];
	}

	// ============================================
	// State: Deck Layout
	// ============================================
	let activeMode = $state<ActivityMode>('workspace');
	let contextCollapsed = $state(false);
	let isMobile = $state(false);
	let workspaceRef: Workspace | undefined = $state();

	// ============================================
	// State: Modals
	// ============================================
	let showSettingsModal = $state(false);
	let showProfileModal = $state(false);
	let showKeyboardShortcuts = $state(false);
	let showAnalyticsModal = $state(false);
	let showTerminalModal = $state(false);
	let showImportModal = $state(false);
	let showAgentImportModal = $state(false);
	let showGitModal = $state(false);
	let showTagManager = $state(false);
	let showKnowledgeModal = $state(false);
	let showCanvas = $state(false);
	let showSpotlight = $state(false);
	let showSubagentManager = $state(false);
	let showAdvancedSearch = $state(false);
	let showProjectModal = $state(false);
	let showNewProjectForm = $state(false);

	// Terminal modal state
	let terminalCommand = $state('/resume');
	let terminalSessionId = $state('');

	// Profile modal state
	let editingProfile: any = $state(null);
	let allSubagents: Subagent[] = $state([]);
	let availableTools: ToolsResponse = $state({ categories: [], all_tools: [] });

	// Tag state
	let allTags: Tag[] = $state([]);

	// Project form state
	let newProjectId = $state('');
	let newProjectName = $state('');
	let newProjectDescription = $state('');

	// Shortcut registrations for cleanup
	let shortcutRegistrations: ShortcutRegistration[] = [];
	let deckUnsubscribe: (() => void) | null = null;

	// ============================================
	// Derived State: Cards for Workspace
	// ============================================
	const workspaceCards = $derived<CardsDeckCard[]>(
		$visibleCards.map((c) => ({
			id: c.id,
			type: mapDeckCardType(c.type),
			title: c.title,
			x: c.position.x,
			y: c.position.y,
			width: c.size.width,
			height: c.size.height,
			zIndex: c.zIndex,
			minimized: c.minimized,
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

	const minimizedCardsForDock = $derived<MinimizedCard[]>(
		$minimizedCardsStore.map((c) => ({
			id: c.id,
			type: mapDeckCardType(c.type) as 'chat' | 'agent' | 'generation' | 'file' | 'settings',
			title: c.title
		}))
	);

	// Active Threads: Show all open cards (visible + minimized) in the sidebar
	const deckSessions = $derived<DeckSession[]>(
		[...$visibleCards, ...$minimizedCardsStore].map((c) => ({
			id: c.id,
			title: c.title || 'Untitled',
			lastMessage: c.type === 'chat' ? 'Chat' : c.type === 'terminal' ? 'Terminal' : c.type,
			timestamp: new Date(c.createdAt),
			isActive: c.id === $focusedCardId && !c.minimized
		}))
	);

	// Recent Sessions: Session history for loading old chats
	// Shows sessions that are NOT currently open as cards
	const openSessionIds = $derived(
		new Set([...$visibleCards, ...$minimizedCardsStore]
			.filter(c => c.dataId)
			.map(c => c.dataId))
	);

	const recentSessions = $derived<import('$lib/components/deck/ContextPanel.svelte').HistorySession[]>(
		$sessions.slice(0, 20).map((s) => ({
			id: s.id,
			title: s.title || 'Untitled',
			timestamp: new Date(s.updated_at),
			isOpen: openSessionIds.has(s.id)
		}))
	);

	const badges = $derived<import('$lib/components/deck/types').ActivityBadges>({
		workspace: $visibleCards.length > 0 ? $visibleCards.length : undefined,
		agents: undefined,
		studio: undefined,
		files: undefined
	});

	// Empty arrays for now (agents/studio integration pending)
	const deckAgents: DeckAgent[] = [];
	const deckGenerations: DeckGeneration[] = [];
	const runningProcesses = $derived<RunningProcess[]>([]);

	// ============================================
	// Helper Functions
	// ============================================
	function mapDeckCardType(type: DeckCardType): CardType {
		switch (type) {
			case 'chat':
				return 'chat';
			case 'agent-monitor':
			case 'agent-launcher':
				return 'agent';
			case 'studio-image':
			case 'studio-video':
				return 'canvas';
			case 'terminal':
				return 'terminal';
			default:
				return 'chat';
		}
	}

	function mapSnapZone(zone: DeckCard['snappedTo']): CardsDeckCard['snappedTo'] {
		if (!zone) return undefined;
		const mapping: Record<string, CardsDeckCard['snappedTo']> = {
			left: 'left',
			right: 'right',
			top: 'top',
			bottom: 'bottom',
			'top-left': 'topleft',
			'top-right': 'topright',
			'bottom-left': 'bottomleft',
			'bottom-right': 'bottomright'
		};
		return mapping[zone] || undefined;
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

		// Combine visible and minimized cards from deck store
		const allDeckCards = [...$visibleCards, ...$minimizedCardsStore];

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
			loadSubagents(),
			loadTools(),
			loadAllTags()
		]);

		if ($isAdmin) {
			tabs.loadApiUsers();
			tabs.loadAdminSessions();
		}

		// Sync deck cards with tabs after initialization
		// This ensures chat cards have valid tab references
		syncDeckCardsWithTabs();

		// Setup responsive handling
		checkMobile();
		window.addEventListener('resize', checkMobile);
		window.addEventListener('resize', updateWorkspaceBounds);

		// Initial workspace bounds
		updateWorkspaceBounds();

		// Sync activeMode from deck store
		deckUnsubscribe = deck.subscribe((state) => {
			activeMode = state.activeMode as ActivityMode;
			contextCollapsed = state.contextPanelCollapsed;
		});

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
				description: 'Close modal / minimize card',
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
					} else if (showSettingsModal) {
						showSettingsModal = false;
					} else if (showImportModal) {
						showImportModal = false;
					} else if (showProfileModal) {
						showProfileModal = false;
					} else if (showSubagentManager) {
						showSubagentManager = false;
					} else if (showProjectModal) {
						showProjectModal = false;
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
					} else if (showAgentImportModal) {
						showAgentImportModal = false;
					} else if (showTagManager) {
						showTagManager = false;
					} else if ($focusedCardId) {
						deck.minimizeCard($focusedCardId);
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
				action: () => { showSettingsModal = true; }
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
			})
		];
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('resize', checkMobile);
			window.removeEventListener('resize', updateWorkspaceBounds);
		}
		if (deckUnsubscribe) {
			deckUnsubscribe();
		}
		shortcutRegistrations.forEach((reg) => reg.unregister());
		shortcutRegistrations = [];
		tabs.destroy();
	});

	// ============================================
	// Data Loading
	// ============================================
	async function loadSubagents() {
		try {
			allSubagents = await api.get<Subagent[]>('/subagents');
		} catch (e) {
			console.error('Failed to load subagents:', e);
			allSubagents = [];
		}
	}

	async function loadTools() {
		try {
			availableTools = await api.get<ToolsResponse>('/tools');
		} catch (e) {
			console.error('Failed to load tools:', e);
			availableTools = { categories: [], all_tools: [] };
		}
	}

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
	function handleModeChange(mode: ActivityMode) {
		activeMode = mode;
		deck.setMode(mode as 'workspace' | 'agents' | 'studio' | 'files');
	}

	function handleSessionClick(session: DeckSession) {
		// Since we now pass card.id as session.id, find the card directly
		const allCards = [...$visibleCards, ...$minimizedCardsStore];
		const existingCard = allCards.find((c) => c.id === session.id);
		if (existingCard) {
			if (existingCard.minimized) {
				deck.restoreCard(existingCard.id);
			}
			deck.focusCard(existingCard.id);
		}
	}

	function handleHistorySessionClick(historySession: import('$lib/components/deck/ContextPanel.svelte').HistorySession) {
		// Check if this session is already open as a card
		const existingCard = deck.findCardByDataId(historySession.id);
		if (existingCard) {
			// Focus the existing card
			if (existingCard.minimized) {
				deck.restoreCard(existingCard.id);
			}
			deck.focusCard(existingCard.id);
		} else {
			// Open the session as a new card
			const tabId = tabs.openSession(historySession.id);
			deck.addCard('chat', {
				title: historySession.title || 'Chat',
				dataId: historySession.id,
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
			case 'agent':
				deckCardType = 'agent-monitor';
				title = 'Agent Monitor';
				break;
			case 'canvas':
				deckCardType = 'studio-image';
				title = 'Image Studio';
				break;
			case 'terminal':
				deckCardType = 'terminal';
				title = 'Terminal';
				break;
			default:
				deckCardType = 'chat';
				title = 'New Card';
		}

		deck.addCard(deckCardType, { title, dataId, meta });
	}

	function handleCardFocus(id: string) {
		deck.focusCard(id);
	}

	function handleCardMove(id: string, x: number, y: number) {
		const card = deck.getCard(id);
		if (!card || !workspaceRef) {
			deck.moveCard(id, x, y);
			return;
		}

		const { width, height } = card.size;

		// Apply bounds clamping to prevent cards from going off-screen
		let clampedPos = workspaceRef.clampToBounds(x, y, width, height);

		// Apply grid snapping if enabled
		clampedPos = workspaceRef.snapToGrid(clampedPos.x, clampedPos.y);

		// Check for card-to-card snapping
		const snapResult = workspaceRef.checkCardSnapping(id, clampedPos.x, clampedPos.y, width, height);

		// Update the card position with snapped coordinates
		deck.moveCard(id, snapResult.x, snapResult.y);

		// Show snap guides during drag
		workspaceRef.updateSnapGuides(snapResult.guides);

		// Also show edge snap preview if applicable
		workspaceRef.showSnapPreview(snapResult.x, snapResult.y, width, height);
	}

	function handleCardResize(id: string, width: number, height: number) {
		deck.resizeCard(id, width, height);
	}

	function handleCardDragEnd(id: string) {
		if (workspaceRef) {
			// Clear snap guides when drag ends
			workspaceRef.clearSnapGuides();
			workspaceRef.hideSnapPreview();

			// Finalize edge snapping if card is near workspace edge
			const card = deck.getCard(id);
			if (card) {
				workspaceRef.finalizeSnap(id, card.position.x, card.position.y, card.size.width, card.size.height);
			}
		}
	}

	function handleCardResizeEnd(id: string) {
		if (workspaceRef) {
			// Clear snap guides when resize ends
			workspaceRef.clearSnapGuides();
		}
	}

	function handleCardSnap(id: string, snapTo: CardsDeckCard['snappedTo']) {
		if (!snapTo) return;
		const mapping: Record<string, DeckCard['snappedTo']> = {
			left: 'left',
			right: 'right',
			top: 'top',
			bottom: 'bottom',
			topleft: 'top-left',
			topright: 'top-right',
			bottomleft: 'bottom-left',
			bottomright: 'bottom-right'
		};
		const zone = mapping[snapTo] || null;
		if (zone) {
			deck.snapCard(id, zone);
		}
	}

	function handleCardMinimize(id: string) {
		deck.minimizeCard(id);
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

	function handleMinimizedCardClick(card: MinimizedCard) {
		deck.restoreCard(card.id);
	}

	function handleCardTitleChange(id: string, title: string) {
		deck.setCardTitle(id, title);
	}

	function handleFork(cardId: string, sessionId: string, messageIndex: number, messageId: string) {
		console.log('Fork requested:', { cardId, sessionId, messageIndex, messageId });
	}

	// ============================================
	// Spotlight Search Handlers
	// ============================================
	function handleSpotlightSelectSession(session: { id: string }) {
		// Create a card for this session
		const existingCard = deck.findCardByDataId(session.id);
		if (existingCard) {
			if (existingCard.minimized) {
				deck.restoreCard(existingCard.id);
			} else {
				deck.focusCard(existingCard.id);
			}
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
			const activeCard = $visibleCards.find((c) => c.id === $focusedCardId);
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
		showProfileModal = true;
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
	// Profile Modal Handlers
	// ============================================
	async function handleExportProfile(profileId: string) {
		try {
			await exportAgent(profileId);
		} catch (e: any) {
			alert('Export failed: ' + (e.detail || 'Unknown error'));
		}
	}

	async function handleAgentImported() {
		await tabs.loadProfiles();
		showAgentImportModal = false;
		showProfileModal = false;
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

	// ============================================
	// Project Modal Handlers
	// ============================================
	async function createProject() {
		if (!newProjectId || !newProjectName) return;
		await tabs.createProject({
			id: newProjectId.toLowerCase().replace(/[^a-z0-9-]/g, '-'),
			name: newProjectName,
			description: newProjectDescription || undefined
		});
		newProjectId = '';
		newProjectName = '';
		newProjectDescription = '';
		showNewProjectForm = false;
	}

	async function deleteProject(projectId: string) {
		if (confirm('Delete this project?')) {
			await tabs.deleteProject(projectId);
		}
	}
</script>

{#if !$isAuthenticated}
	<div class="flex items-center justify-center h-screen bg-background">
		<p class="text-muted-foreground">Redirecting to login...</p>
	</div>
{:else}
	<!-- Main Deck Layout -->
	<DeckLayout
		{activeMode}
		{badges}
		contextCollapsed={contextCollapsed}
		sessions={deckSessions}
		{recentSessions}
		agents={deckAgents}
		generations={deckGenerations}
		currentSession={null}
		minimizedCards={minimizedCardsForDock}
		{runningProcesses}
		onModeChange={handleModeChange}
		onLogoClick={() => handleCreateCard('chat')}
		onSettingsClick={() => (showSettingsModal = true)}
		onContextToggle={() => deck.toggleContextPanel()}
		onSessionClick={handleSessionClick}
		onHistorySessionClick={handleHistorySessionClick}
		onAgentClick={() => {}}
		onGenerationClick={() => {}}
		onMinimizedCardClick={handleMinimizedCardClick}
		onProcessClick={() => {}}
	>
		{#if activeMode === 'workspace'}
			{#if isMobile}
				<!-- Mobile Workspace -->
				<MobileWorkspace
					cards={workspaceCards}
					activeCardIndex={$mobileActiveCardIndex}
					minimizedCount={$minimizedCardsStore.length}
					onCardChange={(index) => deck.setMobileActiveCardIndex(index)}
					onCloseCard={handleCardClose}
					onCreateCard={handleCreateCard}
					onSettingsClick={() => (showSettingsModal = true)}
					onMinimizedClick={() => {
						// Restore the first minimized card
						if ($minimizedCardsStore.length > 0) {
							const firstMinimized = $minimizedCardsStore[0];
							handleMinimizedCardClick({ id: firstMinimized.id, type: firstMinimized.type, title: firstMinimized.title });
						}
					}}
				>
					{#snippet children(card)}
						{@const tabId = getTabIdForCard(card)}
						{#if card.type === 'chat' && tabId}
							<ChatCard
								{card}
								{tabId}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
								onMinimize={() => handleCardMinimize(card.id)}
								onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
								onFork={(sessionId, messageIndex, messageId) =>
									handleFork(card.id, sessionId, messageIndex, messageId)
								}
							/>
						{:else if card.type === 'agent'}
							<div class="p-4">
								<p class="text-muted-foreground">Agent view (mobile)</p>
							</div>
						{:else if card.type === 'canvas'}
							<div class="p-4">
								<p class="text-muted-foreground">Canvas view (mobile)</p>
							</div>
						{:else if card.type === 'terminal'}
							<div class="terminal-mobile-content">
								<div class="terminal-line">Terminal integration coming soon...</div>
								<div class="terminal-line"></div>
								<div class="terminal-prompt">
									<span class="prompt-symbol">$</span>
									<span class="cursor"></span>
								</div>
							</div>
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
					onCardSnap={handleCardSnap}
					onCreateCard={handleCreateCard}
					gridSnapEnabled={$gridSnapEnabled}
					cardSnapEnabled={true}
				>
					{#snippet children()}
						{#each workspaceCards.sort((a, b) => a.zIndex - b.zIndex) as card (card.id)}
							{@const tabId = getTabIdForCard(card)}
							{#if !card.minimized}
								<div
									class="card-wrapper"
									class:maximized={card.maximized}
									style:position="absolute"
									style:left={card.maximized ? '0' : `${card.x}px`}
									style:top={card.maximized ? '0' : `${card.y}px`}
									style:width={card.maximized ? '100%' : `${card.width}px`}
									style:height={card.maximized ? '100%' : `${card.height}px`}
									style:z-index={card.zIndex}
								>
									{#if card.type === 'chat'}
										{#if tabId}
											<ChatCard
												{card}
												{tabId}
												onClose={() => handleCardClose(card.id)}
												onMinimize={() => handleCardMinimize(card.id)}
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
									{:else if card.type === 'agent'}
										<AgentCard
											{card}
											agentId={card.data?.dataId as string || card.id}
											onClose={() => handleCardClose(card.id)}
											onMinimize={() => handleCardMinimize(card.id)}
											onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else if card.type === 'canvas'}
										<CanvasCard
											{card}
											canvasId={card.data?.dataId as string || card.id}
											onClose={() => handleCardClose(card.id)}
											onMinimize={() => handleCardMinimize(card.id)}
											onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else if card.type === 'terminal'}
										<TerminalCard
											{card}
											onClose={() => handleCardClose(card.id)}
											onMinimize={() => handleCardMinimize(card.id)}
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
							{/if}
						{/each}
					{/snippet}
				</Workspace>
			{/if}
		{:else if activeMode === 'agents'}
			<AgentsView />
		{:else if activeMode === 'studio'}
			<StudioView />
		{:else}
			<!-- Files mode placeholder -->
			<div class="flex items-center justify-center h-full">
				<div class="text-center">
					<div class="w-16 h-16 mx-auto mb-4 rounded-2xl bg-muted/50 flex items-center justify-center">
						<svg class="w-8 h-8 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
						</svg>
					</div>
					<p class="text-lg text-muted-foreground mb-2">File Browser</p>
					<p class="text-sm text-muted-foreground/70">Coming soon</p>
				</div>
			</div>
		{/if}
	</DeckLayout>

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

	<!-- Settings Modal -->
	<SettingsModal open={showSettingsModal} onClose={() => showSettingsModal = false} />

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

	<!-- Profile Modal -->
	<ProfileModal
		show={showProfileModal}
		{editingProfile}
		profiles={$profiles}
		{allSubagents}
		{availableTools}
		groups={$groups}
		on:close={() => {
			showProfileModal = false;
			editingProfile = null;
		}}
		on:save={async (e) => {
			const { isNew, data } = e.detail;
			if (isNew) {
				await tabs.createProfile(data);
			} else {
				await tabs.updateProfile(data.id, {
					name: data.name,
					description: data.description,
					config: data.config
				});
			}
		}}
		on:delete={async (e) => {
			const profileId = e.detail;
			if (confirm('Delete this profile?')) {
				await tabs.deleteProfile(profileId);
			}
		}}
		on:export={(e) => handleExportProfile(e.detail)}
		on:import={async (e) => {
			const file = e.detail;
			try {
				const content = await file.text();
				const parsed = parseAgentExportFile(content);
				const profile = await importAgent(parsed);
				await tabs.loadProfiles();
				alert(`Successfully imported agent: ${profile.name}`);
			} catch (err: any) {
				alert('Import failed: ' + (err.detail || err.message || 'Invalid file format'));
			}
		}}
		on:assignGroup={(e) => groups.assignToGroup('profiles', e.detail.profileId, e.detail.groupName)}
		on:removeGroup={(e) => groups.removeFromGroup('profiles', e.detail)}
		on:createGroup={(e) => {
			groups.createGroup('profiles', e.detail.groupName);
			groups.assignToGroup('profiles', e.detail.profileId, e.detail.groupName);
		}}
	/>

	<!-- Agent Import Modal -->
	<AgentImportModal
		show={showAgentImportModal}
		on:close={() => showAgentImportModal = false}
		on:imported={handleAgentImported}
	/>

	<!-- Subagent Manager Panel -->
	{#if showSubagentManager}
		<SubagentManager onClose={() => showSubagentManager = false} onUpdate={() => loadSubagents()} />
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

	<!-- Project Modal -->
	{#if showProjectModal}
		<div class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" onclick={() => (showProjectModal = false)}>
			<!-- svelte-ignore a11y_click_events_have_key_events -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="bg-card rounded-xl w-full max-w-lg max-h-[80vh] overflow-y-auto" onclick={(e) => e.stopPropagation()}>
				<div class="p-4 border-b border-border flex items-center justify-between">
					<h2 class="text-lg font-semibold text-foreground">Projects</h2>
					<button
						class="text-muted-foreground hover:text-foreground"
						onclick={() => {
							showProjectModal = false;
							showNewProjectForm = false;
						}}
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>

				<div class="p-4">
					{#if showNewProjectForm}
						<div class="space-y-4">
							<div>
								<label class="block text-xs text-muted-foreground mb-1">ID</label>
								<input bind:value={newProjectId} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground" placeholder="my-project" />
							</div>
							<div>
								<label class="block text-xs text-muted-foreground mb-1">Name</label>
								<input bind:value={newProjectName} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground" placeholder="My Project" />
							</div>
							<div>
								<label class="block text-xs text-muted-foreground mb-1">Description</label>
								<textarea bind:value={newProjectDescription} class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground resize-none" rows="2" placeholder="Optional"></textarea>
							</div>
							<div class="flex gap-2">
								<button onclick={() => (showNewProjectForm = false)} class="flex-1 px-4 py-2 bg-muted text-foreground rounded-lg hover:bg-accent">Cancel</button>
								<button onclick={createProject} class="flex-1 px-4 py-2 bg-primary text-foreground rounded-lg hover:opacity-90">Create</button>
							</div>
						</div>
					{:else}
						<div class="space-y-2 mb-4">
							{#each $projects as project}
								<div class="flex items-center justify-between p-3 bg-accent rounded-lg">
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2">
											<p class="text-sm text-foreground font-medium">{project.name}</p>
											{#if $groups.projects.assignments[project.id]}
												<span class="text-[10px] px-1.5 py-0.5 bg-primary/10 text-primary rounded">{$groups.projects.assignments[project.id]}</span>
											{/if}
										</div>
										<p class="text-xs text-muted-foreground mt-0.5">/workspace/{project.path}/</p>
									</div>
									<div class="flex gap-1">
										<button onclick={() => deleteProject(project.id)} class="p-1.5 text-muted-foreground hover:text-destructive" title="Delete project">
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
											</svg>
										</button>
									</div>
								</div>
							{/each}
						</div>
						<button onclick={() => (showNewProjectForm = true)} class="w-full py-2 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-gray-500">
							+ New Project
						</button>
					{/if}
				</div>
			</div>
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

	/* Mobile terminal styling */
	.terminal-mobile-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		background: hsl(var(--background));
		color: #22c55e;
		font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
		font-size: 0.875rem;
		padding: 16px;
		overflow: auto;
	}

	.terminal-mobile-content .terminal-line {
		line-height: 1.5;
		min-height: 1.5em;
	}

	.terminal-mobile-content .terminal-prompt {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-top: 8px;
	}

	.terminal-mobile-content .prompt-symbol {
		color: #00ff00;
	}

	.terminal-mobile-content .cursor {
		width: 8px;
		height: 16px;
		background: #00ff00;
		animation: blink 1s step-end infinite;
	}

	@keyframes blink {
		0%, 100% { opacity: 1; }
		50% { opacity: 0; }
	}
</style>

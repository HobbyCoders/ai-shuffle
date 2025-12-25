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
	import { runningAgents } from '$lib/stores/agents';
	import {
		deck,
		visibleCards,
		focusedCardId,
		mobileActiveCardIndex,
		gridSnapEnabled,
		layoutMode
	} from '$lib/stores/deck';
	import type { DeckCard, DeckCardType, LayoutMode } from '$lib/stores/deck';
	import { groups } from '$lib/stores/groups';
	import { git, branches as gitBranches, currentBranch, gitLoading } from '$lib/stores/git';
	import { registerShortcut, type ShortcutRegistration } from '$lib/services/keyboard';
	import { getTags, type Tag } from '$lib/api/client';
	import type { Command } from '$lib/api/commands';

	// Deck components
	import { DeckLayout } from '$lib/components/deck';
	import { StudioView } from '$lib/components/deck/studio';
	import {
		Workspace,
		ChatCard,
		AgentCard,
		StudioCard,
		TerminalCard,
		MobileWorkspace,
		ProfileCard,
		ProjectCard,
		SubagentCard,
		SettingsCard,
		CardShuffle
	} from '$lib/components/deck/cards';
	import type { CardType, DeckCard as CardsDeckCard } from '$lib/components/deck/cards/types';

	// Modals
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
	import AdvancedSearch from '$lib/components/AdvancedSearch.svelte';
	import CreateMenu from '$lib/components/deck/CreateMenu.svelte';

	// Types from deck/types
	import type {
		ActivityMode,
		DeckSession,
		DeckAgent,
		DeckGeneration,
		RunningProcess
	} from '$lib/components/deck/types';
	import type { ActiveSession, HistorySession, ActivityTabType, OverlayType } from '$lib/components/deck';

	// ============================================
	// State: Deck Layout
	// ============================================
	let activeMode = $state<ActivityMode>('workspace');
	let contextCollapsed = $state(false);
	let isMobile = $state(false);
	let workspaceRef: Workspace | undefined = $state();

	// Activity Panel State
	let activityPanelTab = $state<ActivityTabType>('threads');
	let overlayType = $state<OverlayType>(null);

	// Background Mode State (per-tab)
	interface BackgroundConfig {
		taskName: string;
		branch: string;
		createNewBranch: boolean;
		autoPR: boolean;
		autoMerge: boolean;
		maxDurationMinutes: number;
	}
	let backgroundModeEnabled = $state(false);
	let backgroundConfig = $state<BackgroundConfig>({
		taskName: '',
		branch: 'main',
		createNewBranch: false,
		autoPR: false,
		autoMerge: false,
		maxDurationMinutes: 30
	});

	// Stable callbacks for chat settings (defined once, reused)
	function handleChatSettingsProfileChange(profileId: string) {
		const tab = $activeTab;
		if (tab) tabs.setTabProfile(tab.id, profileId);
	}

	function handleChatSettingsProjectChange(projectId: string) {
		const tab = $activeTab;
		if (tab) tabs.setTabProject(tab.id, projectId);
		git.loadRepository(projectId);
	}

	function handleChatSettingsModelChange(model: string | null) {
		const tab = $activeTab;
		if (tab) tabs.setTabModelOverride(tab.id, model);
	}

	function handleChatSettingsModeChange(mode: string | null) {
		const tab = $activeTab;
		if (tab) tabs.setTabPermissionModeOverride(tab.id, mode);
	}

	function handleChatSettingsBackgroundModeChange(enabled: boolean) {
		backgroundModeEnabled = enabled;
		const tab = $activeTab;
		if (enabled && tab?.project) {
			git.loadRepository(tab.project);
		}
	}

	function handleChatSettingsBackgroundConfigChange(config: Partial<BackgroundConfig>) {
		backgroundConfig = { ...backgroundConfig, ...config };
	}

	// Derived overlay data - automatically updates when dependencies change
	const chatSettingsOverlayData = $derived.by(() => {
		if (overlayType !== 'chat-settings') return {};

		const tab = $activeTab;
		if (!tab) return {};

		// Get profile settings for effective values
		const currentProfile = $profiles.find(p => p.id === tab.profile);
		const profileModel = currentProfile?.config?.model || 'sonnet';
		const profileMode = currentProfile?.config?.permission_mode || 'default';

		// Calculate context usage
		const autocompactBuffer = 45000;
		const contextUsed = (tab.contextUsed ?? (tab.totalTokensIn + tab.totalCacheCreationTokens + tab.totalCacheReadTokens)) + autocompactBuffer;
		const contextMax = tab.contextMax || 200000;
		const contextPercent = Math.min((contextUsed / contextMax) * 100, 100);

		return {
			// Current values
			selectedProfile: tab.profile,
			selectedProject: tab.project,
			selectedModel: tab.modelOverride,
			selectedMode: tab.permissionModeOverride,
			effectiveModel: tab.modelOverride || profileModel,
			effectiveMode: tab.permissionModeOverride || profileMode,
			contextUsage: {
				used: contextUsed,
				total: contextMax,
				percentage: contextPercent
			},

			// Locked states
			isProfileLocked: false,
			isProjectLocked: !!tab.sessionId,

			// Options
			profiles: $profiles,
			projects: $projects,
			models: [
				{ value: 'sonnet', label: 'Sonnet' },
				{ value: 'sonnet-1m', label: 'Sonnet 1M' },
				{ value: 'opus', label: 'Opus' },
				{ value: 'haiku', label: 'Haiku' }
			],
			modes: [
				{ value: 'default', label: 'Ask' },
				{ value: 'acceptEdits', label: 'Auto-Accept' },
				{ value: 'plan', label: 'Plan' },
				{ value: 'bypassPermissions', label: 'Bypass' }
			],

			// Background mode state
			isBackgroundMode: backgroundModeEnabled,
			backgroundConfig: backgroundConfig,

			// Branch data
			branches: $gitBranches.filter(b => !b.remote).map(b => b.name),
			currentBranch: $currentBranch?.name || 'main',
			defaultBranch: 'main',
			loadingBranches: $gitLoading,

			// Stable callbacks (don't change on re-render)
			onProfileChange: handleChatSettingsProfileChange,
			onProjectChange: handleChatSettingsProjectChange,
			onModelChange: handleChatSettingsModelChange,
			onModeChange: handleChatSettingsModeChange,
			onBackgroundModeChange: handleChatSettingsBackgroundModeChange,
			onBackgroundConfigChange: handleChatSettingsBackgroundConfigChange
		};
	});

	// ============================================
	// State: Modals
	// ============================================
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
	let showAdvancedSearch = $state(false);
	let showCreateMenu = $state(false);

	// Terminal modal state
	let terminalCommand = $state('/resume');
	let terminalSessionId = $state('');

	// Tag state
	let allTags: Tag[] = $state([]);

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

	// Active Threads: Show all open cards in the sidebar (legacy)
	const deckSessions = $derived<DeckSession[]>(
		$visibleCards.map((c) => ({
			id: c.id,
			title: c.title || 'Untitled',
			lastMessage: c.type === 'chat' ? 'Chat' : c.type === 'terminal' ? 'Terminal' : c.type,
			timestamp: new Date(c.createdAt),
			isActive: c.id === $focusedCardId
		}))
	);

	// Active Sessions for Activity Panel: Combines open cards and running agents
	const activeSessions = $derived<ActiveSession[]>([
		// Open cards
		...$visibleCards.map((c) => ({
			id: c.id,
			type: 'chat' as const,
			title: c.title || 'Untitled',
			status: c.id === $focusedCardId ? 'active' as const : 'idle' as const,
			isSelected: c.id === $focusedCardId,
			unread: false
		})),
		// Running agents
		...$runningAgents.map(agent => ({
			id: agent.id,
			type: 'agent' as const,
			title: agent.name,
			status: agent.status === 'running' ? 'running' as const :
			        agent.status === 'failed' ? 'error' as const : 'idle' as const,
			progress: agent.progress,
			isSelected: false,
			unread: false
		}))
	]);

	// Recent Sessions: Session history for loading old chats
	// Shows sessions that are NOT currently open as cards
	const openSessionIds = $derived(
		new Set($visibleCards
			.filter(c => c.dataId)
			.map(c => c.dataId))
	);

	const recentSessions = $derived<HistorySession[]>(
		$sessions.slice(0, 20).map((s) => ({
			id: s.id,
			title: s.title || 'Untitled',
			timestamp: new Date(s.updated_at),
			isOpen: openSessionIds.has(s.id)
		}))
	);

	const badges = $derived<import('$lib/components/deck/types').ActivityBadges>({
		workspace: $visibleCards.length > 0 ? $visibleCards.length : undefined,
		studio: undefined,
		files: undefined
	});

	// Map running agents to DeckAgent format for ContextPanel
	const deckAgents = $derived<DeckAgent[]>(
		$runningAgents.map(agent => ({
			id: agent.id,
			name: agent.name,
			status: agent.status === 'running' ? 'running' :
			        agent.status === 'paused' ? 'paused' :
			        agent.status === 'failed' ? 'error' : 'idle',
			task: agent.prompt?.slice(0, 50),
			progress: agent.progress
		}))
	);

	// Split agents for Activity Panel tabs
	const runningAgentsForPanel = $derived<DeckAgent[]>(
		deckAgents.filter(a => a.status === 'running' || a.status === 'paused')
	);
	const completedAgentsForPanel = $derived<DeckAgent[]>(
		deckAgents.filter(a => a.status === 'idle' || a.status === 'error')
	);

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
				return 'studio';
			case 'terminal':
				return 'terminal';
			case 'settings':
				return 'settings';
			case 'profile':
				return 'profile';
			case 'subagent':
				return 'subagent';
			case 'project':
				return 'project';
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

		// Use visible cards from deck store
		const allDeckCards = $visibleCards;

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

			// Background agent (Cmd+Shift+B)
			registerShortcut({
				id: 'new-agent',
				description: 'New background agent',
				key: 'b',
				cmdOrCtrl: true,
				shift: true,
				category: 'agents',
				action: () => { handleCreateCard('agent'); }
			}),

			// Studio (Cmd+Shift+S)
			registerShortcut({
				id: 'open-studio',
				description: 'Open studio',
				key: 's',
				cmdOrCtrl: true,
				shift: true,
				category: 'studio',
				action: () => { handleCreateCard('studio'); }
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
					} else if (showAgentImportModal) {
						showAgentImportModal = false;
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
		deck.setMode(mode as 'workspace' | 'studio' | 'files');
	}

	function handleSessionClick(session: ActiveSession) {
		if (session.type === 'chat') {
			// It's a card - focus it
			const existingCard = $visibleCards.find((c) => c.id === session.id);
			if (existingCard) {
				deck.focusCard(existingCard.id);
			}
		} else if (session.type === 'agent') {
			// It's an agent - open/focus the agent monitor card
			deck.setMode('workspace');
			deck.openOrFocusCard('agent-monitor', session.id, {
				title: session.title,
				meta: { agentId: session.id }
			});
		}
	}

	function handleHistorySessionClick(historySession: HistorySession) {
		// Check if this session is already open as a card
		const existingCard = deck.findCardByDataId(historySession.id);
		if (existingCard) {
			// Focus the existing card
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
			case 'studio':
				deckCardType = 'studio-image';
				title = 'Studio';
				break;
			case 'terminal':
				deckCardType = 'terminal';
				title = 'Terminal';
				break;
			case 'settings': {
				// Singleton - check if already open
				const existingSettings = $visibleCards.find(c => c.type === 'settings');
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
				const existingSubagent = $visibleCards.find(c => c.type === 'subagent');
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
				const existingProject = $visibleCards.find(c => c.type === 'project');
				if (existingProject) {
					deck.focusCard(existingProject.id);
					return;
				}
				deckCardType = 'project';
				title = 'Projects';
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

	function handleFork(cardId: string, sessionId: string, messageIndex: number, messageId: string) {
		console.log('Fork requested:', { cardId, sessionId, messageIndex, messageId });
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

	// ============================================
	// Profile/Project Card Handlers (from ChatHeader context menu)
	// ============================================
	function handleOpenProfileCard(editId?: string) {
		// Find or create profile card
		const existingProfile = $visibleCards.find(c => c.type === 'profile');
		if (existingProfile) {
			deck.focusCard(existingProfile.id);
			// If editing a specific profile, set metadata to open in edit mode
			if (editId) {
				deck.setCardMeta(existingProfile.id, { editProfileId: editId });
			}
		} else {
			deck.addCard('profile', {
				title: 'Profiles',
				meta: editId ? { editProfileId: editId } : undefined
			});
		}
	}

	function handleOpenChatSettings() {
		// Toggle overlay if already open
		if (overlayType === 'chat-settings') {
			overlayType = null;
			// Also collapse the side panel when closing settings
			deck.toggleContextPanel();
			return;
		}

		// Expand the context panel if collapsed
		deck.expandContextPanel();

		// Just set the overlay type - data is derived reactively
		overlayType = 'chat-settings';
	}

	function handleOpenProjectCard(editId?: string) {
		// Find or create project card
		const existingProject = $visibleCards.find(c => c.type === 'project');
		if (existingProject) {
			deck.focusCard(existingProject.id);
			// If editing a specific project, set metadata to open in edit mode
			if (editId) {
				deck.setCardMeta(existingProject.id, { editProjectId: editId });
			}
		} else {
			deck.addCard('project', {
				title: 'Projects',
				meta: editId ? { editProjectId: editId } : undefined
			});
		}
	}

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
		{activeMode}
		{badges}
		contextCollapsed={contextCollapsed}
		activeTab={activityPanelTab}
		{activeSessions}
		{recentSessions}
		runningAgents={runningAgentsForPanel}
		completedAgents={completedAgentsForPanel}
		generations={deckGenerations}
		currentSession={null}
		{overlayType}
		overlayData={chatSettingsOverlayData}
		sessions={deckSessions}
		agents={deckAgents}
		{runningProcesses}
		onModeChange={handleModeChange}
		onLogoClick={() => showCreateMenu = !showCreateMenu}
		onSettingsClick={() => handleCreateCard('settings')}
		onContextToggle={() => deck.toggleContextPanel()}
		onTabChange={(tab) => activityPanelTab = tab}
		onSessionClick={handleSessionClick}
		onHistorySessionClick={handleHistorySessionClick}
		onAgentClick={(agent) => {
			// Open agent card in workspace
			deck.setMode('workspace');
			deck.openOrFocusCard('agent-monitor', agent.id, {
				title: agent.name,
				meta: { agentId: agent.id }
			});
		}}
		onGenerationClick={() => {}}
		onOverlayClose={() => { overlayType = null; }}
		onProcessClick={() => {}}
	>
		{#if activeMode === 'workspace'}
			{#if isMobile}
				<!-- Mobile Workspace -->
				<MobileWorkspace
					cards={workspaceCards}
					activeCardIndex={$mobileActiveCardIndex}
					onCardChange={(index) => deck.setMobileActiveCardIndex(index)}
					onCloseCard={handleCardClose}
					onCreateCard={handleCreateCard}
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
								onOpenProfileCard={handleOpenProfileCard}
								onOpenProjectCard={handleOpenProjectCard}
								onOpenSettings={handleOpenChatSettings}
							/>
						{:else if card.type === 'agent'}
							<div class="p-4">
								<p class="text-muted-foreground">Agent view (mobile)</p>
							</div>
						{:else if card.type === 'studio'}
							<StudioCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
																onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
							/>
						{:else if card.type === 'terminal'}
							<div class="terminal-mobile-content">
								<div class="terminal-line">Terminal integration coming soon...</div>
								<div class="terminal-line"></div>
								<div class="terminal-prompt">
									<span class="prompt-symbol">$</span>
									<span class="cursor"></span>
								</div>
							</div>
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
						{:else if card.type === 'profile'}
							<ProfileCard
								{card}
								mobile={true}
								onClose={() => handleCardClose(card.id)}
																onMaximize={() => handleCardMaximize(card.id)}
								onFocus={() => handleCardFocus(card.id)}
								onMove={(x, y) => handleCardMove(card.id, x, y)}
								onResize={(w, h) => handleCardResize(card.id, w, h)}
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
																								onMaximize={() => handleCardMaximize(card.id)}
												onFocus={() => handleCardFocus(card.id)}
												onMove={(x, y) => handleCardMove(card.id, x, y)}
												onResize={(w, h) => handleCardResize(card.id, w, h)}
												onDragEnd={() => handleCardDragEnd(card.id)}
												onResizeEnd={() => handleCardResizeEnd(card.id)}
												onFork={(sessionId, messageIndex, messageId) =>
													handleFork(card.id, sessionId, messageIndex, messageId)
												}
												onOpenProfileCard={handleOpenProfileCard}
												onOpenProjectCard={handleOpenProjectCard}
												onOpenSettings={handleOpenChatSettings}
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
																						onMaximize={() => handleCardMaximize(card.id)}
											onFocus={() => handleCardFocus(card.id)}
											onMove={(x, y) => handleCardMove(card.id, x, y)}
											onResize={(w, h) => handleCardResize(card.id, w, h)}
											onDragEnd={() => handleCardDragEnd(card.id)}
											onResizeEnd={() => handleCardResizeEnd(card.id)}
										/>
									{:else if card.type === 'studio'}
										<StudioCard
											{card}
											onClose={() => handleCardClose(card.id)}
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

	<!-- Create Menu -->
	<CreateMenu
		open={showCreateMenu}
		onClose={() => showCreateMenu = false}
		onCreateChat={() => handleCreateCard('chat')}
		onCreateStudio={() => handleCreateCard('studio')}
		onCreateTerminal={() => handleCreateCard('terminal')}
		onOpenProfiles={() => handleCreateCard('profile')}
		onOpenProjects={() => handleCreateCard('project')}
		onOpenSettings={() => handleCreateCard('settings')}
		{isMobile}
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

	<!-- Agent Import Modal -->
	<AgentImportModal
		show={showAgentImportModal}
		on:close={() => showAgentImportModal = false}
		on:imported={async () => {
			await tabs.loadProfiles();
			showAgentImportModal = false;
		}}
	/>

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

<script lang="ts">
	/**
	 * AI Hub Main Page - The Deck Layout
	 *
	 * This is the main entry point using "The Deck" card-based layout system.
	 * It manages authentication, session loading, WebSocket connections, and
	 * routes between different activity modes (chat, agents, studio, files).
	 */
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';

	// Auth stores
	import {
		auth,
		isAuthenticated,
		isAdmin,
		claudeAuthenticated,
		username,
		apiUser
	} from '$lib/stores/auth';

	// Tab store (manages sessions, WebSocket, etc.)
	import {
		tabs,
		allTabs,
		activeTabId,
		activeTab,
		profiles,
		projects,
		sessions
	} from '$lib/stores/tabs';

	// Deck stores
	import {
		deck,
		activeMode,
		visibleCards,
		minimizedCards,
		contextPanelCollapsed,
		runningAgentsCount,
		activeGenerationsCount,
		groups
	} from '$lib/stores';
	import { agents, runningAgents, allAgents } from '$lib/stores/agents';
	import { studio, recentGenerations } from '$lib/stores/studio';

	// Deck components
	import { DeckLayout } from '$lib/components/deck';
	import { CardContainer } from '$lib/components/deck/cards';
	import ChatCard from '$lib/components/deck/cards/ChatCard.svelte';
	import AgentCard from '$lib/components/deck/cards/AgentCard.svelte';
	import CanvasCard from '$lib/components/deck/cards/CanvasCard.svelte';
	import TerminalCard from '$lib/components/deck/cards/TerminalCard.svelte';

	// Essential modals
	import SettingsModal from '$lib/components/SettingsModal.svelte';
	import SpotlightSearch from '$lib/components/SpotlightSearch.svelte';
	import KeyboardShortcutsModal from '$lib/components/KeyboardShortcutsModal.svelte';
	import ProfileModal from '$lib/components/ProfileModal.svelte';
	import TerminalModal from '$lib/components/TerminalModal.svelte';
	import AnalyticsModal from '$lib/components/AnalyticsModal.svelte';
	import ImportSessionModal from '$lib/components/ImportSessionModal.svelte';
	import KnowledgeManager from '$lib/components/KnowledgeManager.svelte';
	import GitModal from '$lib/components/git/GitModal.svelte';
	import SubagentManager from '$lib/components/SubagentManager.svelte';

	// Permission & question handling
	import PermissionQueue from '$lib/components/PermissionQueue.svelte';
	import UserQuestion from '$lib/components/UserQuestion.svelte';

	// Keyboard shortcuts
	import { registerShortcut, type ShortcutRegistration } from '$lib/services/keyboard';

	// Types
	import type { ActivityMode, DeckSession, DeckAgent, DeckGeneration, ActivityBadges, SessionInfo } from '$lib/components/deck/types';
	import type { DeckCard, LayoutMode } from '$lib/components/deck/cards/types';
	import type { Command } from '$lib/api/commands';
	import type { Session } from '$lib/api/client';

	// ==========================================================================
	// State
	// ==========================================================================

	// Modal states
	let showSettingsModal = $state(false);
	let showSpotlight = $state(false);
	let showKeyboardShortcuts = $state(false);
	let showProfileModal = $state(false);
	let showTerminalModal = $state(false);
	let showAnalyticsModal = $state(false);
	let showImportModal = $state(false);
	let showKnowledgeModal = $state(false);
	let showGitModal = $state(false);
	let showSubagentManager = $state(false);

	// Terminal modal state
	let terminalCommand = $state('/resume');
	let terminalSessionId = $state('');

	// Keyboard shortcut registrations
	let shortcutRegistrations: ShortcutRegistration[] = [];

	// Track auth state for reactive initialization
	let wasAuthenticated = $state(false);

	// Current card layout mode
	let cardLayout: LayoutMode = $state('stack');

	// ==========================================================================
	// Derived State
	// ==========================================================================

	// Convert tabs to deck cards for display
	let deckCards = $derived.by(() => {
		return $allTabs.map((tab): DeckCard => ({
			id: tab.id,
			type: 'chat',
			title: tab.title || 'New Chat',
			pinned: false,
			minimized: false,
			createdAt: new Date(),
			updatedAt: new Date()
		}));
	});

	// Active card ID from active tab
	let activeCardId = $derived($activeTabId);

	// Convert sessions to DeckSession format for context panel
	let deckSessions = $derived.by(() => {
		return $sessions.slice(0, 10).map((s): DeckSession => ({
			id: s.id,
			title: s.title || 'Untitled',
			preview: undefined,
			updatedAt: new Date(s.updated_at),
			tokenCount: s.total_tokens_in + s.total_tokens_out,
			cost: s.total_cost_usd,
			active: $allTabs.some(t => t.sessionId === s.id)
		}));
	});

	// Convert running agents to DeckAgent format
	let deckAgents = $derived.by(() => {
		return $runningAgents.map((a): DeckAgent => ({
			id: a.id,
			name: a.name,
			status: a.status as 'running' | 'paused' | 'completed' | 'failed',
			progress: a.progress,
			branch: a.branch,
			startedAt: a.startedAt,
			currentTask: a.tasks.find(t => t.status === 'in_progress')?.name
		}));
	});

	// Convert generations to DeckGeneration format
	let deckGenerations = $derived.by(() => {
		return $recentGenerations.slice(0, 5).map((g): DeckGeneration => ({
			id: g.id,
			type: g.type,
			prompt: g.prompt,
			status: g.status,
			progress: g.progress,
			thumbnailUrl: g.resultUrl,
			resultUrl: g.resultUrl
		}));
	});

	// Current session info for context panel
	let currentSessionInfo = $derived.by((): SessionInfo | null => {
		if (!$activeTab) return null;
		return {
			inputTokens: $activeTab.totalTokensIn || 0,
			outputTokens: $activeTab.totalTokensOut || 0,
			totalCost: 0, // Cost is tracked at session level, not in tab
			modelName: $activeTab.modelOverride || 'Claude'
		};
	});

	// Activity badges for the rail
	let activityBadges = $derived.by((): ActivityBadges => {
		const badges: ActivityBadges = {};

		// Chat badge: number of open tabs
		if ($allTabs.length > 0) {
			badges.chat = { count: $allTabs.length };
		}

		// Agents badge: running agents
		if ($runningAgentsCount > 0) {
			badges.agents = { count: $runningAgentsCount, variant: 'success' };
		}

		// Studio badge: active generations
		if ($activeGenerationsCount > 0) {
			badges.studio = { count: $activeGenerationsCount, variant: 'warning' };
		}

		return badges;
	});

	// ==========================================================================
	// Lifecycle
	// ==========================================================================

	// Reactive initialization when authenticated
	$effect(() => {
		if ($isAuthenticated && !wasAuthenticated) {
			wasAuthenticated = true;
			initializeStores();
		} else if (!$isAuthenticated && wasAuthenticated) {
			wasAuthenticated = false;
		}
	});

	onMount(() => {
		if ($isAuthenticated) {
			wasAuthenticated = true;
			initializeStores();
		}

		// Handle page restored from bfcache
		const handlePageShow = (event: PageTransitionEvent) => {
			if (event.persisted && $isAuthenticated) {
				tabs.loadSessions();
				if ($isAdmin) {
					tabs.loadAdminSessions();
				}
			}
		};
		window.addEventListener('pageshow', handlePageShow);

		// Register keyboard shortcuts
		registerKeyboardShortcuts();

		return () => {
			window.removeEventListener('pageshow', handlePageShow);
			shortcutRegistrations.forEach(reg => reg.unregister());
		};
	});

	onDestroy(() => {
		tabs.destroy();
		agents.destroy();
		studio.destroy();
		shortcutRegistrations.forEach(reg => reg.unregister());
	});

	// ==========================================================================
	// Initialization
	// ==========================================================================

	async function initializeStores() {
		tabs.init();
		agents.init();

		const promises = [
			tabs.loadProfiles(),
			tabs.loadSessions(),
			tabs.loadProjects()
		];

		if ($isAdmin) {
			promises.push(tabs.loadApiUsers());
			promises.push(tabs.loadAdminSessions());
		}

		await Promise.all(promises);
	}

	function registerKeyboardShortcuts() {
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

			// New chat (Cmd+N)
			registerShortcut({
				id: 'new-chat',
				description: 'Start new chat',
				key: 'n',
				cmdOrCtrl: true,
				category: 'chat',
				action: handleNewChat
			}),

			// Send message (Cmd+Enter) - handled by individual cards

			// Stop generation (Cmd+Shift+S)
			registerShortcut({
				id: 'stop-generation',
				description: 'Stop generation',
				key: 's',
				cmdOrCtrl: true,
				shift: true,
				category: 'chat',
				action: () => {
					if ($activeTabId && $activeTab?.isStreaming) {
						tabs.stopGeneration($activeTabId);
					}
				}
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
				description: 'Close modal / cancel',
				key: 'Escape',
				category: 'general',
				allowInInput: true,
				action: handleEscapeKey
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
			})
		];
	}

	function handleEscapeKey() {
		if (showKeyboardShortcuts) {
			showKeyboardShortcuts = false;
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
		} else if (showTerminalModal) {
			showTerminalModal = false;
		} else if (showGitModal) {
			showGitModal = false;
		} else if (showKnowledgeModal) {
			showKnowledgeModal = false;
		} else if (showAnalyticsModal) {
			showAnalyticsModal = false;
		}
	}

	// ==========================================================================
	// Event Handlers - Activity Rail
	// ==========================================================================

	function handleModeChange(mode: ActivityMode) {
		deck.setMode(mode);
	}

	function handleSettingsClick() {
		showSettingsModal = true;
	}

	// ==========================================================================
	// Event Handlers - Context Panel
	// ==========================================================================

	function handleContextCollapsedChange(collapsed: boolean) {
		deck.toggleContextPanel();
	}

	function handleSessionClick(session: DeckSession) {
		// Load session in current tab or create new tab
		if ($activeTabId) {
			tabs.loadSessionInTab($activeTabId, session.id);
		} else {
			tabs.createTab(session.id);
		}
	}

	function handleAgentClick(agent: DeckAgent) {
		// Switch to agents mode and show agent details
		deck.setMode('agents');
		// Could create an agent card or show in context panel
	}

	function handleGenerationClick(generation: DeckGeneration) {
		// Switch to studio mode
		deck.setMode('studio');
		studio.setActiveGeneration(generation.id);
	}

	// ==========================================================================
	// Event Handlers - Dock
	// ==========================================================================

	function handleNewChat() {
		tabs.createTab();
		deck.setMode('chat');
	}

	function handleNewAgent() {
		deck.setMode('agents');
		showSubagentManager = true;
	}

	function handleNewCreate() {
		deck.setMode('studio');
		// Could open a create prompt modal
	}

	// ==========================================================================
	// Event Handlers - Cards
	// ==========================================================================

	function handleCardActivate(id: string) {
		tabs.setActiveTab(id);
	}

	function handleCardClose(id: string) {
		tabs.closeTab(id);
	}

	function handleCardMinimize(id: string) {
		deck.minimizeCard(id);
	}

	function handleLayoutChange(layout: LayoutMode) {
		cardLayout = layout;
	}

	function handleCardReorder(cards: DeckCard[]) {
		// Cards reordered - could persist this if needed
	}

	// ==========================================================================
	// Event Handlers - Chat Cards
	// ==========================================================================

	function handleSendMessage(tabId: string, message: string) {
		tabs.sendMessage(tabId, message);
	}

	function handleStopStreaming(tabId: string) {
		tabs.stopGeneration(tabId);
	}

	// ==========================================================================
	// Event Handlers - Spotlight
	// ==========================================================================

	function handleSpotlightSelectSession(session: Session) {
		if ($activeTabId) {
			tabs.loadSessionInTab($activeTabId, session.id);
		}
		showSpotlight = false;
	}

	function handleSpotlightSelectCommand(command: Command) {
		if (!$activeTabId) return;

		if (command.type === 'interactive') {
			terminalCommand = `/${command.name}`;
			const tab = $allTabs.find(t => t.id === $activeTabId);
			if (tab?.sessionId) {
				terminalSessionId = tab.sessionId;
				showTerminalModal = true;
			}
		}
		showSpotlight = false;
	}

	function handleSpotlightNewChat() {
		tabs.createTab();
		showSpotlight = false;
	}

	function handleSpotlightOpenSettings() {
		showProfileModal = true;
		showSpotlight = false;
	}

	// ==========================================================================
	// Event Handlers - Permission & Questions
	// ==========================================================================

	function handlePermissionRespond(tabId: string, event: CustomEvent<{
		request_id: string;
		decision: 'allow' | 'deny';
		remember?: 'none' | 'session' | 'profile';
		pattern?: string;
	}>) {
		const { request_id, decision, remember, pattern } = event.detail;
		tabs.sendPermissionResponse(tabId, request_id, decision, remember, pattern);
	}

	function handleUserQuestionRespond(tabId: string, event: CustomEvent<{
		request_id: string;
		tool_use_id: string;
		answers: Record<string, string | string[]>;
	}>) {
		const { request_id, tool_use_id, answers } = event.detail;
		tabs.sendUserQuestionResponse(tabId, request_id, tool_use_id, answers);
	}

	// ==========================================================================
	// Utility
	// ==========================================================================

	async function handleLogout() {
		await auth.logout();
		goto('/login');
	}
</script>

<!-- Authentication check -->
{#if !$isAuthenticated}
	<div class="h-screen flex items-center justify-center bg-background">
		<div class="text-center">
			<div class="w-16 h-16 rounded-2xl bg-primary/10 flex items-center justify-center mx-auto mb-4">
				<svg class="w-8 h-8 text-primary animate-pulse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
				</svg>
			</div>
			<p class="text-muted-foreground">Loading...</p>
		</div>
	</div>
{:else}
	<!-- Main Deck Layout -->
	<DeckLayout
		activeMode={$activeMode}
		badges={activityBadges}
		contextCollapsed={$contextPanelCollapsed}
		sessions={deckSessions}
		agents={deckAgents}
		generations={deckGenerations}
		currentSession={currentSessionInfo}
		onModeChange={handleModeChange}
		onSettingsClick={handleSettingsClick}
		onContextCollapsedChange={handleContextCollapsedChange}
		onSessionClick={handleSessionClick}
		onAgentClick={handleAgentClick}
		onGenerationClick={handleGenerationClick}
		onNewChat={handleNewChat}
		onNewAgent={handleNewAgent}
		onNewCreate={handleNewCreate}
	>
		<!-- Main Content Area -->
		{#if $activeMode === 'chat'}
			<!-- Chat Mode: Show chat cards -->
			{#if deckCards.length === 0}
				<!-- Empty state -->
				<div class="h-full flex items-center justify-center">
					<div class="text-center max-w-md px-4">
						<div class="w-20 h-20 rounded-2xl bg-primary/10 flex items-center justify-center mx-auto mb-6">
							<svg class="w-10 h-10 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
							</svg>
						</div>
						<h2 class="text-xl font-semibold text-foreground mb-2">Welcome to AI Hub</h2>
						<p class="text-muted-foreground mb-6">Start a conversation with Claude to begin exploring ideas, writing code, or getting help with any task.</p>
						<button
							onclick={handleNewChat}
							class="inline-flex items-center gap-2 px-6 py-3 bg-primary text-primary-foreground rounded-xl font-medium hover:bg-primary/90 transition-colors"
						>
							<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
							</svg>
							New Chat
						</button>
					</div>
				</div>
			{:else}
				<CardContainer
					cards={deckCards}
					layout={cardLayout}
					{activeCardId}
					oncardactivate={handleCardActivate}
					oncardclose={handleCardClose}
					oncardminimize={handleCardMinimize}
					onlayoutchange={handleLayoutChange}
					onreorder={handleCardReorder}
				>
					{#snippet children({ card, index, isActive })}
						{@const tab = $allTabs.find(t => t.id === card.id)}
						{#if tab}
							<ChatCard
								id={card.id}
								title={tab.title || 'New Chat'}
								sessionId={tab.sessionId || ''}
								messages={tab.messages}
								isStreaming={tab.isStreaming}
								profile={$profiles.find(p => p.id === tab.profile)?.name}
								project={$projects.find(p => p.id === tab.project)?.name}
								pinned={card.pinned}
								minimized={card.minimized}
								active={isActive}
								onpin={() => deck.pinCard(card.id)}
								onminimize={() => handleCardMinimize(card.id)}
								onclose={() => handleCardClose(card.id)}
								onactivate={() => handleCardActivate(card.id)}
								onsendmessage={(msg) => handleSendMessage(card.id, msg)}
								onstopstreaming={() => handleStopStreaming(card.id)}
							/>

							<!-- Permission Queue for this tab -->
							{#if tab.pendingPermissions.length > 0}
								<div class="absolute bottom-20 left-4 right-4 z-50">
									<PermissionQueue
										requests={tab.pendingPermissions}
										on:respond={(e) => handlePermissionRespond(tab.id, e)}
									/>
								</div>
							{/if}

							<!-- User Question for this tab -->
							{#if tab.pendingQuestions.length > 0}
								<div class="absolute bottom-20 left-4 right-4 z-50">
									<UserQuestion
										data={tab.pendingQuestions[0]}
										on:respond={(e) => handleUserQuestionRespond(tab.id, e)}
									/>
								</div>
							{/if}
						{/if}
					{/snippet}
				</CardContainer>
			{/if}

		{:else if $activeMode === 'agents'}
			<!-- Agents Mode -->
			<div class="h-full flex items-center justify-center">
				<div class="text-center max-w-md px-4">
					<div class="w-20 h-20 rounded-2xl bg-success/10 flex items-center justify-center mx-auto mb-6">
						<svg class="w-10 h-10 text-success" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
						</svg>
					</div>
					<h2 class="text-xl font-semibold text-foreground mb-2">Background Agents</h2>
					<p class="text-muted-foreground mb-6">Launch autonomous agents to work on tasks in the background. Perfect for long-running operations.</p>
					{#if $allAgents.length > 0}
						<div class="space-y-2 mb-6">
							{#each $allAgents as agent}
								<div class="p-3 bg-card border border-border rounded-lg text-left">
									<div class="flex items-center gap-2">
										<span class="w-2 h-2 rounded-full {agent.status === 'running' ? 'bg-success animate-pulse' : agent.status === 'completed' ? 'bg-primary' : 'bg-warning'}"></span>
										<span class="font-medium text-foreground">{agent.name}</span>
										<span class="text-xs text-muted-foreground ml-auto capitalize">{agent.status}</span>
									</div>
									{#if agent.progress}
										<div class="mt-2 h-1 bg-muted rounded-full overflow-hidden">
											<div class="h-full bg-primary transition-all" style="width: {agent.progress}%"></div>
										</div>
									{/if}
								</div>
							{/each}
						</div>
					{/if}
					<button
						onclick={handleNewAgent}
						class="inline-flex items-center gap-2 px-6 py-3 bg-success text-success-foreground rounded-xl font-medium hover:bg-success/90 transition-colors"
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
						</svg>
						Launch Agent
					</button>
				</div>
			</div>

		{:else if $activeMode === 'studio'}
			<!-- Studio Mode -->
			<div class="h-full flex items-center justify-center">
				<div class="text-center max-w-md px-4">
					<div class="w-20 h-20 rounded-2xl bg-purple-500/10 flex items-center justify-center mx-auto mb-6">
						<svg class="w-10 h-10 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4.098 19.902a3.75 3.75 0 005.304 0l6.401-6.402M6.75 21A3.75 3.75 0 013 17.25V4.125C3 3.504 3.504 3 4.125 3h5.25c.621 0 1.125.504 1.125 1.125v4.072M6.75 21a3.75 3.75 0 003.75-3.75V8.197M6.75 21h13.125c.621 0 1.125-.504 1.125-1.125v-5.25c0-.621-.504-1.125-1.125-1.125h-4.072M10.5 8.197l2.88-2.88c.438-.439 1.15-.439 1.59 0l3.712 3.713c.44.44.44 1.152 0 1.59l-2.879 2.88M6.75 17.25h.008v.008H6.75v-.008z" />
						</svg>
					</div>
					<h2 class="text-xl font-semibold text-foreground mb-2">Creative Studio</h2>
					<p class="text-muted-foreground mb-6">Generate images and videos with AI. Create stunning visuals from text descriptions.</p>
					<button
						onclick={handleNewCreate}
						class="inline-flex items-center gap-2 px-6 py-3 bg-purple-500 text-white rounded-xl font-medium hover:bg-purple-600 transition-colors"
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
						</svg>
						Create
					</button>
				</div>
			</div>

		{:else if $activeMode === 'files'}
			<!-- Files Mode -->
			<div class="h-full flex items-center justify-center">
				<div class="text-center max-w-md px-4">
					<div class="w-20 h-20 rounded-2xl bg-blue-500/10 flex items-center justify-center mx-auto mb-6">
						<svg class="w-10 h-10 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M2.25 12.75V12A2.25 2.25 0 014.5 9.75h15A2.25 2.25 0 0121.75 12v.75m-8.69-6.44l-2.12-2.12a1.5 1.5 0 00-1.061-.44H4.5A2.25 2.25 0 002.25 6v12a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9a2.25 2.25 0 00-2.25-2.25h-5.379a1.5 1.5 0 01-1.06-.44z" />
						</svg>
					</div>
					<h2 class="text-xl font-semibold text-foreground mb-2">File Browser</h2>
					<p class="text-muted-foreground mb-6">Browse and manage project files. Coming soon.</p>
				</div>
			</div>
		{/if}
	</DeckLayout>

	<!-- Modals -->
	<!-- Settings Modal (Svelte 5 - callback props) -->
	<SettingsModal
		open={showSettingsModal}
		onClose={() => showSettingsModal = false}
	/>

	<!-- Spotlight Search (Svelte 5 - callback props) -->
	<SpotlightSearch
		visible={showSpotlight}
		sessions={$sessions}
		currentProjectId={$activeTab?.project}
		onClose={() => showSpotlight = false}
		onSelectSession={(session) => handleSpotlightSelectSession(session)}
		onSelectCommand={(command) => handleSpotlightSelectCommand(command)}
		onNewChat={handleSpotlightNewChat}
		onOpenSettings={handleSpotlightOpenSettings}
	/>

	<!-- Keyboard Shortcuts Modal (Svelte 5 - callback props) -->
	<KeyboardShortcutsModal
		open={showKeyboardShortcuts}
		onClose={() => showKeyboardShortcuts = false}
	/>

	<!-- Profile Modal (Svelte 4 - uses export and events) -->
	{#if showProfileModal}
		<ProfileModal
			show={showProfileModal}
			profiles={$profiles}
			groups={$groups}
			on:close={() => showProfileModal = false}
		/>
	{/if}

	<!-- Terminal Modal (Svelte 5 - callback props) -->
	{#if showTerminalModal && terminalSessionId}
		<TerminalModal
			sessionId={terminalSessionId}
			command={terminalCommand}
			onClose={() => showTerminalModal = false}
		/>
	{/if}

	<!-- Analytics Modal (Svelte 4 - uses export and callback props) -->
	{#if showAnalyticsModal}
		<AnalyticsModal
			open={showAnalyticsModal}
			onClose={() => showAnalyticsModal = false}
		/>
	{/if}

	<!-- Import Session Modal (Svelte 4 - uses export and events) -->
	{#if showImportModal}
		<ImportSessionModal
			show={showImportModal}
			on:close={() => showImportModal = false}
		/>
	{/if}

	<!-- Knowledge Manager (Svelte 4 - uses export and events) -->
	{#if showKnowledgeModal && $activeTab?.project}
		<KnowledgeManager
			projectId={$activeTab.project}
			on:close={() => showKnowledgeModal = false}
		/>
	{/if}

	<!-- Git Modal (Svelte 5 - callback props) -->
	{#if showGitModal && $activeTab?.project}
		<GitModal
			open={showGitModal}
			projectId={$activeTab.project}
			onClose={() => showGitModal = false}
		/>
	{/if}

	<!-- Subagent Manager (Svelte 5 - callback props) -->
	{#if showSubagentManager}
		<SubagentManager onClose={() => showSubagentManager = false} />
	{/if}
{/if}

<style>
	/* Global styles for the page */
	:global(body) {
		overflow: hidden;
	}
</style>

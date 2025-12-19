<script lang="ts">
	/**
	 * The Deck - Desktop-like AI Workspace
	 *
	 * A fully functional card-based workspace with:
	 * - Draggable, resizable cards (chat, agent, canvas, terminal)
	 * - Card lifecycle (create, focus, move, resize, minimize, maximize, close)
	 * - Integration with tabs store for chat sessions
	 * - Context panel showing real sessions
	 * - Settings modal integration
	 * - Keyboard shortcuts (Cmd+K spotlight, Cmd+N new chat, Escape)
	 */
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { isAuthenticated } from '$lib/stores/auth';
	import { tabs, allTabs, sessions } from '$lib/stores/tabs';
	import {
		deck,
		visibleCards,
		minimizedCards as minimizedCardsStore,
		focusedCardId,
		mobileActiveCardIndex
	} from '$lib/stores/deck';
	import type { DeckCard, DeckCardType } from '$lib/stores/deck';

	// Deck components
	import { DeckLayout } from '$lib/components/deck';
	import { AgentsView } from '$lib/components/deck/agents';
	import { StudioView } from '$lib/components/deck/studio';
	import {
		Workspace,
		ChatCard,
		AgentCard,
		CanvasCard,
		MobileWorkspace
	} from '$lib/components/deck/cards';
	import type { CardType, DeckCard as CardsDeckCard } from '$lib/components/deck/cards/types';

	// Settings Modal
	import SettingsModal from '$lib/components/SettingsModal.svelte';

	// Types from deck/types
	import type {
		ActivityMode,
		DeckSession,
		DeckAgent,
		DeckGeneration,
		MinimizedCard,
		RunningProcess
	} from '$lib/components/deck/types';

	// Local state
	let activeMode = $state<ActivityMode>('workspace');
	let contextCollapsed = $state(false);
	let isMobile = $state(false);
	let showSettingsModal = $state(false);
	let workspaceRef: Workspace | undefined = $state();

	// Map deck store cards to the card format expected by Workspace
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

	// Helper to map deck store card types to workspace card types
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

	// Helper to map snap zones
	function mapSnapZone(
		zone: DeckCard['snappedTo']
	): CardsDeckCard['snappedTo'] {
		if (!zone) return undefined;
		// Map kebab-case to flat names
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

	// Map minimized cards for the dock
	const minimizedCardsForDock = $derived<MinimizedCard[]>(
		$minimizedCardsStore.map((c) => ({
			id: c.id,
			type: mapDeckCardType(c.type) as 'chat' | 'agent' | 'generation' | 'file' | 'settings',
			title: c.title
		}))
	);

	// Running processes (agents that are running)
	const runningProcesses = $derived<RunningProcess[]>([]);

	// Store unsubscribe function for cleanup
	let deckUnsubscribe: (() => void) | null = null;

	// Auth check on mount
	onMount(async () => {
		if (!$isAuthenticated) {
			await goto('/login');
			return;
		}

		// Initialize tabs store
		await tabs.init();

		// Check mobile
		checkMobile();
		window.addEventListener('resize', checkMobile);
		window.addEventListener('resize', updateWorkspaceBounds);
		window.addEventListener('keydown', handleGlobalKeydown);

		// Initial workspace bounds update
		updateWorkspaceBounds();

		// Sync activeMode from deck store
		deckUnsubscribe = deck.subscribe((state) => {
			activeMode = state.activeMode as ActivityMode;
			contextCollapsed = state.contextPanelCollapsed;
		});
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('resize', checkMobile);
			window.removeEventListener('resize', updateWorkspaceBounds);
			window.removeEventListener('keydown', handleGlobalKeydown);
		}
		if (deckUnsubscribe) {
			deckUnsubscribe();
		}
	});

	function checkMobile() {
		isMobile = window.innerWidth < 640;
		deck.setIsMobile(isMobile);
	}

	function updateWorkspaceBounds() {
		// Get workspace dimensions from the main content area
		const mainArea = document.querySelector('.workspace-container');
		if (mainArea) {
			const rect = mainArea.getBoundingClientRect();
			deck.setWorkspaceBounds(rect.width, rect.height);
		}
	}

	// Handle global keyboard shortcuts
	function handleGlobalKeydown(e: KeyboardEvent) {
		// Cmd/Ctrl+K - Open spotlight/main chat
		if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
			e.preventDefault();
			goto('/');
		}

		// Cmd/Ctrl+N - New chat card
		if ((e.metaKey || e.ctrlKey) && e.key === 'n') {
			e.preventDefault();
			handleCreateCard('chat');
		}

		// Cmd/Ctrl+, - Open settings
		if ((e.metaKey || e.ctrlKey) && e.key === ',') {
			e.preventDefault();
			showSettingsModal = true;
		}

		// Escape - Close settings modal or minimize focused card
		if (e.key === 'Escape') {
			if (showSettingsModal) {
				showSettingsModal = false;
			} else if ($focusedCardId) {
				deck.minimizeCard($focusedCardId);
			}
		}

		// Cmd/Ctrl+W - Close focused card
		if ((e.metaKey || e.ctrlKey) && e.key === 'w' && $focusedCardId) {
			e.preventDefault();
			handleCardClose($focusedCardId);
		}
	}

	// Handle mode change
	function handleModeChange(mode: ActivityMode) {
		activeMode = mode;
		deck.setMode(mode as 'workspace' | 'agents' | 'studio' | 'files');
	}

	// Derive sessions for context panel
	const deckSessions = $derived<DeckSession[]>(
		$sessions.slice(0, 20).map((s) => ({
			id: s.id,
			title: s.title || 'Untitled',
			lastMessage: s.title || undefined,
			timestamp: new Date(s.updated_at),
			isActive: $allTabs.some((t) => t.sessionId === s.id)
		}))
	);

	// Empty arrays for now (agents/studio integration pending)
	const deckAgents: DeckAgent[] = [];
	const deckGenerations: DeckGeneration[] = [];

	// Badges
	const badges = $derived<import('$lib/components/deck/types').ActivityBadges>({
		workspace: $visibleCards.length > 0 ? $visibleCards.length : undefined,
		agents: undefined,
		studio: undefined,
		files: undefined
	});

	// Handle session click - open or focus card for that session
	function handleSessionClick(session: DeckSession) {
		// Check if there's already a card for this session
		const existingCard = deck.findCardByDataId(session.id);
		if (existingCard) {
			if (existingCard.minimized) {
				deck.restoreCard(existingCard.id);
			} else {
				deck.focusCard(existingCard.id);
			}
		} else {
			// Create a new chat card for this session
			const tabId = tabs.openSession(session.id);
			deck.addCard('chat', {
				title: session.title,
				dataId: session.id,
				meta: { tabId }
			});
		}
	}

	// Handle card creation
	function handleCreateCard(type: CardType) {
		let deckCardType: DeckCardType;
		let title: string;
		let dataId: string | null = null;
		let meta: Record<string, unknown> = {};

		switch (type) {
			case 'chat': {
				deckCardType = 'chat';
				title = 'New Chat';
				// Create a new tab and link it to the card
				const tabId = tabs.createTab();
				dataId = null; // No session yet
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

	// Handle card focus
	function handleCardFocus(id: string) {
		deck.focusCard(id);
	}

	// Handle card move
	function handleCardMove(id: string, x: number, y: number) {
		deck.moveCard(id, x, y);

		// Show snap preview if workspace ref is available
		const card = deck.getCard(id);
		if (card && workspaceRef) {
			workspaceRef.showSnapPreview(x, y, card.size.width, card.size.height);
		}
	}

	// Handle card resize
	function handleCardResize(id: string, width: number, height: number) {
		deck.resizeCard(id, width, height);
	}

	// Handle card snap
	function handleCardSnap(id: string, snapTo: CardsDeckCard['snappedTo']) {
		if (!snapTo) return;

		// Map from workspace format to deck store format
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

	// Handle card minimize
	function handleCardMinimize(id: string) {
		deck.minimizeCard(id);
	}

	// Handle card maximize/unmaximize
	function handleCardMaximize(id: string) {
		deck.toggleMaximize(id);
	}

	// Handle card close
	function handleCardClose(id: string) {
		const card = deck.getCard(id);
		if (card?.meta?.tabId) {
			// Close the associated tab
			tabs.closeTab(card.meta.tabId as string);
		}
		deck.removeCard(id);
	}

	// Handle minimized card restore
	function handleMinimizedCardClick(card: MinimizedCard) {
		deck.restoreCard(card.id);
	}

	// Handle card title change
	function handleCardTitleChange(id: string, title: string) {
		deck.setCardTitle(id, title);
	}

	// Get tab ID for a card
	function getTabIdForCard(card: CardsDeckCard): string | null {
		if (card.data?.meta?.tabId) {
			return card.data.meta.tabId as string;
		}
		// Fallback: find tab by session ID
		if (card.data?.dataId) {
			const tabId = tabs.findTabBySessionId(card.data.dataId as string);
			return tabId;
		}
		return null;
	}

	// Handle fork in chat
	function handleFork(cardId: string, sessionId: string, messageIndex: number, messageId: string) {
		// For now, just log - fork functionality to be implemented
		console.log('Fork requested:', { cardId, sessionId, messageIndex, messageId });
	}
</script>

{#if !$isAuthenticated}
	<div class="flex items-center justify-center h-screen bg-background">
		<p class="text-muted-foreground">Redirecting to login...</p>
	</div>
{:else}
	<DeckLayout
		{activeMode}
		{badges}
		contextCollapsed={contextCollapsed}
		sessions={deckSessions}
		agents={deckAgents}
		generations={deckGenerations}
		currentSession={null}
		minimizedCards={minimizedCardsForDock}
		{runningProcesses}
		onModeChange={handleModeChange}
		onLogoClick={() => goto('/')}
		onSettingsClick={() => (showSettingsModal = true)}
		onContextToggle={() => deck.toggleContextPanel()}
		onSessionClick={handleSessionClick}
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
				>
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
									/>
								{:else}
									<!-- Terminal or other card types -->
									<div class="card-placeholder">
										<p class="text-muted-foreground">
											{card.type} card (coming soon)
										</p>
									</div>
								{/if}
							</div>
						{/if}
					{/each}
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
					<div
						class="w-16 h-16 mx-auto mb-4 rounded-2xl bg-muted/50 flex items-center justify-center"
					>
						<svg
							class="w-8 h-8 text-muted-foreground"
							fill="none"
							stroke="currentColor"
							viewBox="0 0 24 24"
						>
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="1.5"
								d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"
							/>
						</svg>
					</div>
					<p class="text-lg text-muted-foreground mb-2">File Browser</p>
					<p class="text-sm text-muted-foreground/70">Coming soon</p>
				</div>
			</div>
		{/if}
	</DeckLayout>

	<!-- Settings Modal -->
	<SettingsModal open={showSettingsModal} onClose={() => (showSettingsModal = false)} />
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
</style>

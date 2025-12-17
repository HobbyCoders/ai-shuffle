/**
 * Deck Store - Main state management for "The Deck" UI
 *
 * Manages cards, layout modes, context panel, and coordinates
 * with background agents and media generations.
 */

import { writable, derived, get } from 'svelte/store';

// =============================================================================
// Types
// =============================================================================

export type ActivityMode = 'chat' | 'agents' | 'studio' | 'files';

export type CardType = 'chat' | 'agent' | 'file' | 'generation' | 'settings' | 'terminal';

export type CardLayout = 'stack' | 'split' | 'focus' | 'grid';

export type AgentStatus = 'queued' | 'running' | 'paused' | 'completed' | 'failed';

export type GenerationStatus = 'pending' | 'generating' | 'completed' | 'failed';

export type GenerationType = 'image' | 'video';

export interface DeckCard {
	id: string;
	type: CardType;
	title: string;
	subtitle?: string;
	icon?: string;
	// Card state
	minimized: boolean;
	pinned: boolean;
	// Card-specific data
	sessionId?: string; // For chat cards
	agentId?: string; // For agent cards
	filePath?: string; // For file cards
	generationId?: string; // For generation cards
	// Timestamps
	createdAt: Date;
	lastActiveAt: Date;
}

export interface DeckAgent {
	id: string;
	name: string;
	status: AgentStatus;
	progress: number;
	branch?: string;
	startedAt: Date;
	completedAt?: Date;
}

export interface DeckGeneration {
	id: string;
	type: GenerationType;
	prompt: string;
	status: GenerationStatus;
	progress: number;
	provider: string;
	resultUrl?: string;
	error?: string;
	createdAt: Date;
	completedAt?: Date;
}

export interface DeckState {
	// Current mode
	activeMode: ActivityMode;

	// Cards
	cards: DeckCard[];
	activeCardId: string | null;
	cardLayout: CardLayout;

	// Context panel
	contextPanelCollapsed: boolean;

	// Background agents
	runningAgents: DeckAgent[];

	// Media generations
	activeGenerations: DeckGeneration[];
}

// =============================================================================
// Persistence
// =============================================================================

const STORAGE_KEY = 'aihub_deck_state';
const SAVE_DEBOUNCE_MS = 500;

interface PersistedDeckState {
	activeMode: ActivityMode;
	cards: Array<{
		id: string;
		type: CardType;
		title: string;
		subtitle?: string;
		icon?: string;
		minimized: boolean;
		pinned: boolean;
		sessionId?: string;
		agentId?: string;
		filePath?: string;
		generationId?: string;
		createdAt: string;
		lastActiveAt: string;
	}>;
	activeCardId: string | null;
	cardLayout: CardLayout;
	contextPanelCollapsed: boolean;
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;
let pendingState: DeckState | null = null;

/**
 * Load deck state from localStorage
 */
function loadPersistedState(): Partial<DeckState> | null {
	if (typeof window === 'undefined') return null;

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (!stored) return null;

		const persisted: PersistedDeckState = JSON.parse(stored);

		return {
			activeMode: persisted.activeMode,
			cards: persisted.cards.map((card) => ({
				...card,
				createdAt: new Date(card.createdAt),
				lastActiveAt: new Date(card.lastActiveAt)
			})),
			activeCardId: persisted.activeCardId,
			cardLayout: persisted.cardLayout,
			contextPanelCollapsed: persisted.contextPanelCollapsed
		};
	} catch (e) {
		console.error('[Deck] Failed to load persisted state:', e);
		return null;
	}
}

/**
 * Save deck state to localStorage (debounced)
 */
function savePersistedState(state: DeckState) {
	if (typeof window === 'undefined') return;

	pendingState = state;

	if (saveTimer) {
		clearTimeout(saveTimer);
	}

	saveTimer = setTimeout(() => {
		if (!pendingState) return;

		try {
			const persisted: PersistedDeckState = {
				activeMode: pendingState.activeMode,
				cards: pendingState.cards.map((card) => ({
					...card,
					createdAt: card.createdAt.toISOString(),
					lastActiveAt: card.lastActiveAt.toISOString()
				})),
				activeCardId: pendingState.activeCardId,
				cardLayout: pendingState.cardLayout,
				contextPanelCollapsed: pendingState.contextPanelCollapsed
			};

			localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
			console.log('[Deck] Saved state to localStorage');
		} catch (e) {
			console.error('[Deck] Failed to save state:', e);
		}

		pendingState = null;
		saveTimer = null;
	}, SAVE_DEBOUNCE_MS);
}

/**
 * Flush pending save immediately (for beforeunload)
 */
function flushPendingSave() {
	if (!pendingState) return;

	if (saveTimer) {
		clearTimeout(saveTimer);
		saveTimer = null;
	}

	try {
		const persisted: PersistedDeckState = {
			activeMode: pendingState.activeMode,
			cards: pendingState.cards.map((card) => ({
				...card,
				createdAt: card.createdAt.toISOString(),
				lastActiveAt: card.lastActiveAt.toISOString()
			})),
			activeCardId: pendingState.activeCardId,
			cardLayout: pendingState.cardLayout,
			contextPanelCollapsed: pendingState.contextPanelCollapsed
		};

		localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
	} catch (e) {
		console.error('[Deck] Failed to flush state:', e);
	}

	pendingState = null;
}

// Register beforeunload handler
if (typeof window !== 'undefined') {
	window.addEventListener('beforeunload', flushPendingSave);
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'hidden') {
			flushPendingSave();
		}
	});
}

// =============================================================================
// Store Creation
// =============================================================================

function getDefaultState(): DeckState {
	return {
		activeMode: 'chat',
		cards: [],
		activeCardId: null,
		cardLayout: 'stack',
		contextPanelCollapsed: false,
		runningAgents: [],
		activeGenerations: []
	};
}

function createDeckStore() {
	// Load persisted state or use defaults
	const persisted = loadPersistedState();
	const initialState: DeckState = {
		...getDefaultState(),
		...persisted,
		// Never persist running agents or generations - they should be reloaded from backend
		runningAgents: [],
		activeGenerations: []
	};

	const { subscribe, set, update } = writable<DeckState>(initialState);

	// Helper to update and persist
	function updateAndPersist(updater: (state: DeckState) => DeckState) {
		update((state) => {
			const newState = updater(state);
			savePersistedState(newState);
			return newState;
		});
	}

	return {
		subscribe,

		// ==========================================================================
		// Mode Actions
		// ==========================================================================

		/**
		 * Switch activity mode (chat, agents, studio, files)
		 */
		setMode(mode: ActivityMode) {
			updateAndPersist((state) => ({
				...state,
				activeMode: mode
			}));
		},

		// ==========================================================================
		// Card Actions
		// ==========================================================================

		/**
		 * Add a new card to the deck
		 */
		addCard(card: Omit<DeckCard, 'id' | 'createdAt' | 'lastActiveAt' | 'minimized' | 'pinned'>) {
			const id = `card-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
			const now = new Date();

			const newCard: DeckCard = {
				...card,
				id,
				minimized: false,
				pinned: false,
				createdAt: now,
				lastActiveAt: now
			};

			updateAndPersist((state) => ({
				...state,
				cards: [...state.cards, newCard],
				activeCardId: id
			}));

			return id;
		},

		/**
		 * Remove a card from the deck
		 */
		removeCard(id: string) {
			updateAndPersist((state) => {
				const cards = state.cards.filter((c) => c.id !== id);
				let activeCardId = state.activeCardId;

				// If we removed the active card, activate the most recent one
				if (activeCardId === id) {
					const visibleCards = cards.filter((c) => !c.minimized);
					if (visibleCards.length > 0) {
						// Sort by lastActiveAt descending
						visibleCards.sort(
							(a, b) => b.lastActiveAt.getTime() - a.lastActiveAt.getTime()
						);
						activeCardId = visibleCards[0].id;
					} else {
						activeCardId = null;
					}
				}

				return {
					...state,
					cards,
					activeCardId
				};
			});
		},

		/**
		 * Bring a card to focus (activate it)
		 */
		activateCard(id: string) {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((card) =>
					card.id === id
						? { ...card, minimized: false, lastActiveAt: new Date() }
						: card
				),
				activeCardId: id
			}));
		},

		/**
		 * Minimize a card to the dock
		 */
		minimizeCard(id: string) {
			updateAndPersist((state) => {
				const cards = state.cards.map((card) =>
					card.id === id ? { ...card, minimized: true } : card
				);

				let activeCardId = state.activeCardId;

				// If we minimized the active card, activate another visible one
				if (activeCardId === id) {
					const visibleCards = cards.filter((c) => !c.minimized);
					if (visibleCards.length > 0) {
						visibleCards.sort(
							(a, b) => b.lastActiveAt.getTime() - a.lastActiveAt.getTime()
						);
						activeCardId = visibleCards[0].id;
					} else {
						activeCardId = null;
					}
				}

				return {
					...state,
					cards,
					activeCardId
				};
			});
		},

		/**
		 * Toggle pin state of a card
		 */
		pinCard(id: string) {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((card) =>
					card.id === id ? { ...card, pinned: !card.pinned } : card
				)
			}));
		},

		/**
		 * Update card properties
		 */
		updateCard(id: string, updates: Partial<Omit<DeckCard, 'id' | 'createdAt'>>) {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((card) =>
					card.id === id ? { ...card, ...updates } : card
				)
			}));
		},

		// ==========================================================================
		// Layout Actions
		// ==========================================================================

		/**
		 * Change the card layout mode
		 */
		setCardLayout(layout: CardLayout) {
			updateAndPersist((state) => ({
				...state,
				cardLayout: layout
			}));
		},

		/**
		 * Toggle context panel collapsed state
		 */
		toggleContextPanel() {
			updateAndPersist((state) => ({
				...state,
				contextPanelCollapsed: !state.contextPanelCollapsed
			}));
		},

		// ==========================================================================
		// Agent Actions (for deck coordination, detailed management in agents.ts)
		// ==========================================================================

		/**
		 * Register a background agent with the deck
		 */
		addAgent(agent: DeckAgent) {
			update((state) => ({
				...state,
				runningAgents: [...state.runningAgents, agent]
			}));
		},

		/**
		 * Update an agent's state
		 */
		updateAgent(id: string, updates: Partial<DeckAgent>) {
			update((state) => ({
				...state,
				runningAgents: state.runningAgents.map((agent) =>
					agent.id === id ? { ...agent, ...updates } : agent
				)
			}));
		},

		/**
		 * Remove an agent from the deck
		 */
		removeAgent(id: string) {
			update((state) => ({
				...state,
				runningAgents: state.runningAgents.filter((a) => a.id !== id)
			}));
		},

		// ==========================================================================
		// Generation Actions (for deck coordination, detailed management in studio.ts)
		// ==========================================================================

		/**
		 * Start a new media generation
		 */
		addGeneration(generation: DeckGeneration) {
			update((state) => ({
				...state,
				activeGenerations: [...state.activeGenerations, generation]
			}));
		},

		/**
		 * Update a generation's state
		 */
		updateGeneration(id: string, updates: Partial<DeckGeneration>) {
			update((state) => ({
				...state,
				activeGenerations: state.activeGenerations.map((gen) =>
					gen.id === id ? { ...gen, ...updates } : gen
				)
			}));
		},

		/**
		 * Remove a completed generation
		 */
		removeGeneration(id: string) {
			update((state) => ({
				...state,
				activeGenerations: state.activeGenerations.filter((g) => g.id !== id)
			}));
		},

		// ==========================================================================
		// Utility Actions
		// ==========================================================================

		/**
		 * Reset deck to default state
		 */
		reset() {
			const defaultState = getDefaultState();
			set(defaultState);
			savePersistedState(defaultState);
		},

		/**
		 * Get current state synchronously
		 */
		getState(): DeckState {
			return get({ subscribe });
		}
	};
}

// =============================================================================
// Export Store Instance
// =============================================================================

export const deck = createDeckStore();

// =============================================================================
// Derived Stores
// =============================================================================

/**
 * Currently active card
 */
export const activeCard = derived(deck, ($deck) =>
	$deck.activeCardId ? $deck.cards.find((c) => c.id === $deck.activeCardId) ?? null : null
);

/**
 * Non-minimized cards (visible in main area)
 */
export const visibleCards = derived(deck, ($deck) =>
	$deck.cards
		.filter((c) => !c.minimized)
		.sort((a, b) => b.lastActiveAt.getTime() - a.lastActiveAt.getTime())
);

/**
 * Minimized cards (in dock)
 */
export const minimizedCards = derived(deck, ($deck) =>
	$deck.cards
		.filter((c) => c.minimized)
		.sort((a, b) => b.lastActiveAt.getTime() - a.lastActiveAt.getTime())
);

/**
 * Pinned cards
 */
export const pinnedCards = derived(deck, ($deck) =>
	$deck.cards.filter((c) => c.pinned).sort((a, b) => b.lastActiveAt.getTime() - a.lastActiveAt.getTime())
);

/**
 * Chat-type cards only
 */
export const chatCards = derived(deck, ($deck) =>
	$deck.cards
		.filter((c) => c.type === 'chat')
		.sort((a, b) => b.lastActiveAt.getTime() - a.lastActiveAt.getTime())
);

/**
 * Agent-type cards only
 */
export const agentCards = derived(deck, ($deck) =>
	$deck.cards
		.filter((c) => c.type === 'agent')
		.sort((a, b) => b.lastActiveAt.getTime() - a.lastActiveAt.getTime())
);

/**
 * File-type cards only
 */
export const fileCards = derived(deck, ($deck) =>
	$deck.cards
		.filter((c) => c.type === 'file')
		.sort((a, b) => b.lastActiveAt.getTime() - a.lastActiveAt.getTime())
);

/**
 * Generation-type cards only
 */
export const generationCards = derived(deck, ($deck) =>
	$deck.cards
		.filter((c) => c.type === 'generation')
		.sort((a, b) => b.lastActiveAt.getTime() - a.lastActiveAt.getTime())
);

/**
 * Current activity mode
 */
export const activeMode = derived(deck, ($deck) => $deck.activeMode);

/**
 * Current card layout
 */
export const cardLayout = derived(deck, ($deck) => $deck.cardLayout);

/**
 * Context panel collapsed state
 */
export const contextPanelCollapsed = derived(deck, ($deck) => $deck.contextPanelCollapsed);

/**
 * Running background agents count
 */
export const runningAgentsCount = derived(
	deck,
	($deck) => $deck.runningAgents.filter((a) => a.status === 'running').length
);

/**
 * Active generations count
 */
export const activeGenerationsCount = derived(
	deck,
	($deck) =>
		$deck.activeGenerations.filter(
			(g) => g.status === 'pending' || g.status === 'generating'
		).length
);

/**
 * Total cards count
 */
export const totalCardsCount = derived(deck, ($deck) => $deck.cards.length);

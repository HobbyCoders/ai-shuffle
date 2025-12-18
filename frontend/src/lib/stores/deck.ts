/**
 * Deck Store - Card-based Workspace Management
 *
 * "The Deck" is a card-based UI paradigm for AI Hub, managing:
 * - Multiple card types (chat, agents, studio, files, settings)
 * - Window-like card behaviors (drag, resize, minimize, maximize, snap)
 * - Workspace persistence across sessions
 * - Mobile-responsive card navigation
 */

import { writable, derived, get } from 'svelte/store';

// ============================================================================
// Types
// ============================================================================

export type DeckMode = 'workspace' | 'agents' | 'studio' | 'files';

export type DeckCardType =
	| 'chat'
	| 'agent-monitor'
	| 'agent-launcher'
	| 'studio-image'
	| 'studio-video'
	| 'file-browser'
	| 'settings'
	| 'profile'
	| 'terminal';

export type SnapZone =
	| 'left'
	| 'right'
	| 'top'
	| 'bottom'
	| 'top-left'
	| 'top-right'
	| 'bottom-left'
	| 'bottom-right'
	| null;

export interface DeckCardPosition {
	x: number;
	y: number;
}

export interface DeckCardSize {
	width: number;
	height: number;
}

export interface DeckCard {
	id: string;
	type: DeckCardType;
	title: string;
	// State
	minimized: boolean;
	maximized: boolean;
	// Position and size
	position: DeckCardPosition;
	size: DeckCardSize;
	// Saved position/size for restore after maximize/snap
	savedPosition?: DeckCardPosition;
	savedSize?: DeckCardSize;
	// Z-ordering
	zIndex: number;
	// Snap state
	snappedTo: SnapZone;
	// Data reference (e.g., sessionId for chat, agentId for monitor)
	dataId: string | null;
	// Card-specific metadata
	meta?: Record<string, unknown>;
	// Timestamp for ordering
	createdAt: Date;
}

export interface DeckState {
	activeMode: DeckMode;
	cards: DeckCard[];
	focusedCardId: string | null;
	nextZIndex: number;
	contextPanelCollapsed: boolean;
	workspaceBounds: { width: number; height: number };
	snapEnabled: boolean;
	gridSnapEnabled: boolean;
	isMobile: boolean;
	mobileActiveCardIndex: number;
}

// ============================================================================
// Constants
// ============================================================================

const STORAGE_KEY = 'deck_state';
const SAVE_DEBOUNCE_MS = 500;
const CASCADE_OFFSET = 30;
const DEFAULT_CARD_SIZE: DeckCardSize = { width: 600, height: 500 };
const MIN_CARD_SIZE: DeckCardSize = { width: 320, height: 200 };

// ============================================================================
// Persistence Helpers
// ============================================================================

interface PersistedDeckState {
	activeMode: DeckMode;
	cards: Array<Omit<DeckCard, 'createdAt'> & { createdAt: string }>;
	contextPanelCollapsed: boolean;
	snapEnabled: boolean;
	gridSnapEnabled: boolean;
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;

function loadFromStorage(): Partial<DeckState> | null {
	if (typeof window === 'undefined') return null;

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (stored) {
			const parsed: PersistedDeckState = JSON.parse(stored);
			return {
				activeMode: parsed.activeMode,
				cards: parsed.cards.map((c) => ({
					...c,
					createdAt: new Date(c.createdAt)
				})),
				contextPanelCollapsed: parsed.contextPanelCollapsed,
				snapEnabled: parsed.snapEnabled,
				gridSnapEnabled: parsed.gridSnapEnabled
			};
		}
	} catch (e) {
		console.error('[Deck] Failed to load state from localStorage:', e);
	}
	return null;
}

function saveToStorage(state: DeckState) {
	if (typeof window === 'undefined') return;

	if (saveTimer) {
		clearTimeout(saveTimer);
	}

	saveTimer = setTimeout(() => {
		try {
			const persisted: PersistedDeckState = {
				activeMode: state.activeMode,
				cards: state.cards.map((c) => ({
					...c,
					createdAt: c.createdAt.toISOString()
				})),
				contextPanelCollapsed: state.contextPanelCollapsed,
				snapEnabled: state.snapEnabled,
				gridSnapEnabled: state.gridSnapEnabled
			};
			localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
		} catch (e) {
			console.error('[Deck] Failed to save state to localStorage:', e);
		}
		saveTimer = null;
	}, SAVE_DEBOUNCE_MS);
}

// ============================================================================
// Helpers
// ============================================================================

function generateCardId(): string {
	return `deck-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

function calculateCascadePosition(existingCards: DeckCard[], bounds: { width: number; height: number }): DeckCardPosition {
	if (existingCards.length === 0) {
		return { x: 50, y: 50 };
	}

	// Find the topmost (highest zIndex) non-minimized card
	const visibleCards = existingCards.filter((c) => !c.minimized);
	if (visibleCards.length === 0) {
		return { x: 50, y: 50 };
	}

	const topCard = visibleCards.reduce((a, b) => (a.zIndex > b.zIndex ? a : b));
	let x = topCard.position.x + CASCADE_OFFSET;
	let y = topCard.position.y + CASCADE_OFFSET;

	// Wrap around if going off screen
	if (x + DEFAULT_CARD_SIZE.width > bounds.width - 50) {
		x = 50;
	}
	if (y + DEFAULT_CARD_SIZE.height > bounds.height - 50) {
		y = 50;
	}

	return { x, y };
}

function getDefaultTitle(type: DeckCardType): string {
	switch (type) {
		case 'chat':
			return 'New Chat';
		case 'agent-monitor':
			return 'Agent Monitor';
		case 'agent-launcher':
			return 'Launch Agent';
		case 'studio-image':
			return 'Image Studio';
		case 'studio-video':
			return 'Video Studio';
		case 'file-browser':
			return 'Files';
		case 'settings':
			return 'Settings';
		case 'profile':
			return 'Profile';
		case 'terminal':
			return 'Terminal';
		default:
			return 'Card';
	}
}

// ============================================================================
// Store Creation
// ============================================================================

function createDeckStore() {
	const persisted = loadFromStorage();

	const initialState: DeckState = {
		activeMode: persisted?.activeMode || 'workspace',
		cards: persisted?.cards || [],
		focusedCardId: null,
		nextZIndex: persisted?.cards ? Math.max(...persisted.cards.map((c) => c.zIndex), 0) + 1 : 1,
		contextPanelCollapsed: persisted?.contextPanelCollapsed ?? false,
		workspaceBounds: { width: 1920, height: 1080 },
		snapEnabled: persisted?.snapEnabled ?? true,
		gridSnapEnabled: persisted?.gridSnapEnabled ?? false,
		isMobile: false,
		mobileActiveCardIndex: 0
	};

	const { subscribe, set, update } = writable<DeckState>(initialState);

	/**
	 * Helper to update state and persist
	 */
	function updateAndPersist(updater: (state: DeckState) => DeckState): void {
		update((state) => {
			const newState = updater(state);
			saveToStorage(newState);
			return newState;
		});
	}

	return {
		subscribe,

		// ========================================================================
		// Mode Management
		// ========================================================================

		/**
		 * Switch active deck mode
		 */
		setMode(mode: DeckMode): void {
			updateAndPersist((state) => ({
				...state,
				activeMode: mode,
				focusedCardId: null
			}));
		},

		// ========================================================================
		// Card Lifecycle
		// ========================================================================

		/**
		 * Add a new card to the deck
		 * Returns the new card's ID
		 */
		addCard(
			type: DeckCardType,
			data?: {
				title?: string;
				dataId?: string | null;
				position?: DeckCardPosition;
				size?: DeckCardSize;
				meta?: Record<string, unknown>;
			}
		): string {
			const cardId = generateCardId();

			update((state) => {
				const position =
					data?.position || calculateCascadePosition(state.cards, state.workspaceBounds);

				const newCard: DeckCard = {
					id: cardId,
					type,
					title: data?.title || getDefaultTitle(type),
					minimized: false,
					maximized: false,
					position,
					size: data?.size || { ...DEFAULT_CARD_SIZE },
					zIndex: state.nextZIndex,
					snappedTo: null,
					dataId: data?.dataId ?? null,
					meta: data?.meta,
					createdAt: new Date()
				};

				const newState = {
					...state,
					cards: [...state.cards, newCard],
					focusedCardId: cardId,
					nextZIndex: state.nextZIndex + 1
				};

				saveToStorage(newState);
				return newState;
			});

			return cardId;
		},

		/**
		 * Remove a card from the deck
		 */
		removeCard(id: string): void {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.filter((c) => c.id !== id),
				focusedCardId: state.focusedCardId === id ? null : state.focusedCardId
			}));
		},

		// ========================================================================
		// Focus Management
		// ========================================================================

		/**
		 * Focus a card (bring to front)
		 */
		focusCard(id: string): void {
			updateAndPersist((state) => {
				const card = state.cards.find((c) => c.id === id);
				if (!card || card.minimized) {
					return state;
				}

				return {
					...state,
					cards: state.cards.map((c) =>
						c.id === id ? { ...c, zIndex: state.nextZIndex } : c
					),
					focusedCardId: id,
					nextZIndex: state.nextZIndex + 1
				};
			});
		},

		// ========================================================================
		// Position and Size
		// ========================================================================

		/**
		 * Move a card to a new position
		 */
		moveCard(id: string, x: number, y: number): void {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((c) =>
					c.id === id
						? {
								...c,
								position: { x, y },
								// Clear snap when manually moved
								snappedTo: null,
								savedPosition: undefined,
								savedSize: undefined
							}
						: c
				)
			}));
		},

		/**
		 * Resize a card
		 */
		resizeCard(id: string, width: number, height: number): void {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((c) =>
					c.id === id
						? {
								...c,
								size: {
									width: Math.max(width, MIN_CARD_SIZE.width),
									height: Math.max(height, MIN_CARD_SIZE.height)
								},
								// Clear snap when manually resized
								snappedTo: null,
								savedPosition: undefined,
								savedSize: undefined
							}
						: c
				)
			}));
		},

		// ========================================================================
		// Minimize/Restore
		// ========================================================================

		/**
		 * Minimize a card
		 */
		minimizeCard(id: string): void {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((c) =>
					c.id === id
						? {
								...c,
								minimized: true,
								maximized: false
							}
						: c
				),
				focusedCardId: state.focusedCardId === id ? null : state.focusedCardId
			}));
		},

		/**
		 * Restore a card from minimized state
		 */
		restoreCard(id: string): void {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((c) =>
					c.id === id
						? {
								...c,
								minimized: false,
								zIndex: state.nextZIndex
							}
						: c
				),
				focusedCardId: id,
				nextZIndex: state.nextZIndex + 1
			}));
		},

		// ========================================================================
		// Maximize/Unmaximize
		// ========================================================================

		/**
		 * Maximize a card to fill the workspace
		 */
		maximizeCard(id: string): void {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((c) =>
					c.id === id
						? {
								...c,
								maximized: true,
								minimized: false,
								// Save current position/size for restore
								savedPosition: c.savedPosition || { ...c.position },
								savedSize: c.savedSize || { ...c.size },
								zIndex: state.nextZIndex
							}
						: c
				),
				focusedCardId: id,
				nextZIndex: state.nextZIndex + 1
			}));
		},

		/**
		 * Restore a card from maximized state
		 */
		unmaximizeCard(id: string): void {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((c) =>
					c.id === id
						? {
								...c,
								maximized: false,
								position: c.savedPosition || c.position,
								size: c.savedSize || c.size,
								savedPosition: undefined,
								savedSize: undefined
							}
						: c
				)
			}));
		},

		/**
		 * Toggle maximize state
		 */
		toggleMaximize(id: string): void {
			const state = get({ subscribe });
			const card = state.cards.find((c) => c.id === id);
			if (card?.maximized) {
				this.unmaximizeCard(id);
			} else {
				this.maximizeCard(id);
			}
		},

		// ========================================================================
		// Snap Zones
		// ========================================================================

		/**
		 * Snap a card to a zone
		 */
		snapCard(id: string, zone: SnapZone): void {
			if (!zone) return;

			updateAndPersist((state) => {
				const card = state.cards.find((c) => c.id === id);
				if (!card) return state;

				// Calculate snap position and size
				const { width: boundsW, height: boundsH } = state.workspaceBounds;
				let position: DeckCardPosition;
				let size: DeckCardSize;

				switch (zone) {
					case 'left':
						position = { x: 0, y: 0 };
						size = { width: boundsW / 2, height: boundsH };
						break;
					case 'right':
						position = { x: boundsW / 2, y: 0 };
						size = { width: boundsW / 2, height: boundsH };
						break;
					case 'top':
						position = { x: 0, y: 0 };
						size = { width: boundsW, height: boundsH / 2 };
						break;
					case 'bottom':
						position = { x: 0, y: boundsH / 2 };
						size = { width: boundsW, height: boundsH / 2 };
						break;
					case 'top-left':
						position = { x: 0, y: 0 };
						size = { width: boundsW / 2, height: boundsH / 2 };
						break;
					case 'top-right':
						position = { x: boundsW / 2, y: 0 };
						size = { width: boundsW / 2, height: boundsH / 2 };
						break;
					case 'bottom-left':
						position = { x: 0, y: boundsH / 2 };
						size = { width: boundsW / 2, height: boundsH / 2 };
						break;
					case 'bottom-right':
						position = { x: boundsW / 2, y: boundsH / 2 };
						size = { width: boundsW / 2, height: boundsH / 2 };
						break;
					default:
						return state;
				}

				return {
					...state,
					cards: state.cards.map((c) =>
						c.id === id
							? {
									...c,
									snappedTo: zone,
									position,
									size,
									savedPosition: c.savedPosition || { ...card.position },
									savedSize: c.savedSize || { ...card.size },
									maximized: false,
									minimized: false,
									zIndex: state.nextZIndex
								}
							: c
					),
					focusedCardId: id,
					nextZIndex: state.nextZIndex + 1
				};
			});
		},

		/**
		 * Remove snap from a card
		 */
		unsnapCard(id: string): void {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((c) =>
					c.id === id
						? {
								...c,
								snappedTo: null,
								position: c.savedPosition || c.position,
								size: c.savedSize || c.size,
								savedPosition: undefined,
								savedSize: undefined
							}
						: c
				)
			}));
		},

		// ========================================================================
		// Layout Actions
		// ========================================================================

		/**
		 * Cascade all visible cards
		 */
		cascadeCards(): void {
			updateAndPersist((state) => {
				const visibleCards = state.cards.filter((c) => !c.minimized);
				let x = 50;
				let y = 50;

				const updatedCards = state.cards.map((card) => {
					if (card.minimized) return card;

					const newCard = {
						...card,
						position: { x, y },
						size: { ...DEFAULT_CARD_SIZE },
						maximized: false,
						snappedTo: null as SnapZone,
						savedPosition: undefined,
						savedSize: undefined
					};

					x += CASCADE_OFFSET;
					y += CASCADE_OFFSET;

					// Wrap around
					if (x + DEFAULT_CARD_SIZE.width > state.workspaceBounds.width - 50) {
						x = 50;
					}
					if (y + DEFAULT_CARD_SIZE.height > state.workspaceBounds.height - 50) {
						y = 50;
					}

					return newCard;
				});

				return {
					...state,
					cards: updatedCards
				};
			});
		},

		/**
		 * Tile all visible cards
		 */
		tileCards(): void {
			updateAndPersist((state) => {
				const visibleCards = state.cards.filter((c) => !c.minimized);
				if (visibleCards.length === 0) return state;

				const count = visibleCards.length;
				const cols = Math.ceil(Math.sqrt(count));
				const rows = Math.ceil(count / cols);

				const cellWidth = state.workspaceBounds.width / cols;
				const cellHeight = state.workspaceBounds.height / rows;

				let idx = 0;
				const updatedCards = state.cards.map((card) => {
					if (card.minimized) return card;

					const col = idx % cols;
					const row = Math.floor(idx / cols);
					idx++;

					return {
						...card,
						position: { x: col * cellWidth, y: row * cellHeight },
						size: { width: cellWidth, height: cellHeight },
						maximized: false,
						snappedTo: null as SnapZone,
						savedPosition: undefined,
						savedSize: undefined
					};
				});

				return {
					...state,
					cards: updatedCards
				};
			});
		},

		// ========================================================================
		// Workspace Settings
		// ========================================================================

		/**
		 * Set workspace bounds (for responsive layout)
		 */
		setWorkspaceBounds(width: number, height: number): void {
			update((state) => ({
				...state,
				workspaceBounds: { width, height }
			}));
		},

		/**
		 * Toggle context panel collapsed state
		 */
		toggleContextPanel(): void {
			updateAndPersist((state) => ({
				...state,
				contextPanelCollapsed: !state.contextPanelCollapsed
			}));
		},

		/**
		 * Set snap enabled
		 */
		setSnapEnabled(enabled: boolean): void {
			updateAndPersist((state) => ({
				...state,
				snapEnabled: enabled
			}));
		},

		/**
		 * Set grid snap enabled
		 */
		setGridSnapEnabled(enabled: boolean): void {
			updateAndPersist((state) => ({
				...state,
				gridSnapEnabled: enabled
			}));
		},

		// ========================================================================
		// Mobile Support
		// ========================================================================

		/**
		 * Set mobile mode
		 */
		setIsMobile(isMobile: boolean): void {
			update((state) => ({
				...state,
				isMobile,
				mobileActiveCardIndex: 0
			}));
		},

		/**
		 * Set active card index for mobile navigation
		 */
		setMobileActiveCardIndex(index: number): void {
			update((state) => ({
				...state,
				mobileActiveCardIndex: Math.max(0, Math.min(index, state.cards.length - 1))
			}));
		},

		// ========================================================================
		// Card Updates
		// ========================================================================

		/**
		 * Update card title
		 */
		setCardTitle(id: string, title: string): void {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((c) => (c.id === id ? { ...c, title } : c))
			}));
		},

		/**
		 * Update card dataId
		 */
		setCardDataId(id: string, dataId: string | null): void {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((c) => (c.id === id ? { ...c, dataId } : c))
			}));
		},

		/**
		 * Update card metadata
		 */
		setCardMeta(id: string, meta: Record<string, unknown>): void {
			updateAndPersist((state) => ({
				...state,
				cards: state.cards.map((c) =>
					c.id === id ? { ...c, meta: { ...c.meta, ...meta } } : c
				)
			}));
		},

		// ========================================================================
		// Utility
		// ========================================================================

		/**
		 * Get a card by ID
		 */
		getCard(id: string): DeckCard | undefined {
			const state = get({ subscribe });
			return state.cards.find((c) => c.id === id);
		},

		/**
		 * Find a card by dataId
		 */
		findCardByDataId(dataId: string): DeckCard | undefined {
			const state = get({ subscribe });
			return state.cards.find((c) => c.dataId === dataId);
		},

		/**
		 * Open or focus an existing card for a dataId
		 */
		openOrFocusCard(
			type: DeckCardType,
			dataId: string,
			options: { title?: string; meta?: Record<string, unknown> } = {}
		): string {
			const state = get({ subscribe });
			const existingCard = state.cards.find((c) => c.dataId === dataId && c.type === type);

			if (existingCard) {
				if (existingCard.minimized) {
					this.restoreCard(existingCard.id);
				} else {
					this.focusCard(existingCard.id);
				}
				return existingCard.id;
			}

			return this.addCard(type, { ...options, dataId });
		},

		/**
		 * Reset store to initial state
		 */
		reset(): void {
			const freshState: DeckState = {
				activeMode: 'workspace',
				cards: [],
				focusedCardId: null,
				nextZIndex: 1,
				contextPanelCollapsed: false,
				workspaceBounds: { width: 1920, height: 1080 },
				snapEnabled: true,
				gridSnapEnabled: false,
				isMobile: false,
				mobileActiveCardIndex: 0
			};
			set(freshState);
			saveToStorage(freshState);
		}
	};
}

// ============================================================================
// Export Store & Derived Stores
// ============================================================================

export const deck = createDeckStore();

// Active mode
export const activeMode = derived(deck, ($deck) => $deck.activeMode);

// All cards
export const allCards = derived(deck, ($deck) => $deck.cards);

// Visible cards (not minimized)
export const visibleCards = derived(deck, ($deck) =>
	$deck.cards.filter((c) => !c.minimized)
);

// Minimized cards
export const minimizedCards = derived(deck, ($deck) =>
	$deck.cards.filter((c) => c.minimized)
);

// Focused card
export const focusedCard = derived(deck, ($deck) => {
	if (!$deck.focusedCardId) return null;
	return $deck.cards.find((c) => c.id === $deck.focusedCardId) || null;
});

// Focused card ID
export const focusedCardId = derived(deck, ($deck) => $deck.focusedCardId);

// Context panel state
export const contextPanelCollapsed = derived(deck, ($deck) => $deck.contextPanelCollapsed);

// Workspace bounds
export const workspaceBounds = derived(deck, ($deck) => $deck.workspaceBounds);

// Mobile state
export const isMobile = derived(deck, ($deck) => $deck.isMobile);
export const mobileActiveCardIndex = derived(deck, ($deck) => $deck.mobileActiveCardIndex);

// Snap settings
export const snapEnabled = derived(deck, ($deck) => $deck.snapEnabled);
export const gridSnapEnabled = derived(deck, ($deck) => $deck.gridSnapEnabled);

// Check if a specific card is focused
export const isCardFocused = (cardId: string) =>
	derived(focusedCardId, ($id) => $id === cardId);

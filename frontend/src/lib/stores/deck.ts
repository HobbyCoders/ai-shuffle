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
import { getPreference, setPreference } from '$lib/api/client';

// ============================================================================
// Types
// ============================================================================

export type DeckMode = 'workspace' | 'agents' | 'studio' | 'files';

/**
 * Layout modes for card arrangement in the workspace
 * - freeflow: Default mode - cards can be freely positioned and resized
 * - sidebyside: Cards arranged side-by-side in vertical columns
 * - tile: Cards tiled in a grid pattern
 * - stack: Cards stacked in a cascade pattern (like a card deck)
 * - focus: One main card takes most of the space, others in a sidebar
 */
export type LayoutMode = 'freeflow' | 'sidebyside' | 'tile' | 'stack' | 'focus';

export type DeckCardType =
	| 'chat'
	| 'agent-monitor'
	| 'agent-launcher'
	| 'studio-image'
	| 'studio-video'
	| 'file-browser'
	| 'settings'
	| 'profile'
	| 'terminal'
	| 'subagent'
	| 'project';

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
	layoutMode: LayoutMode;
}

// ============================================================================
// Constants
// ============================================================================

const STORAGE_KEY = 'deck_state';
const SERVER_PREFERENCE_KEY = 'deck_state';
const SAVE_DEBOUNCE_MS = 500;
const SERVER_SYNC_DEBOUNCE_MS = 1000; // Slightly longer debounce for server sync
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
	layoutMode?: LayoutMode;
	lastModified?: number; // Timestamp for conflict resolution
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;
let serverSyncTimer: ReturnType<typeof setTimeout> | null = null;
let isLoadingFromServer = false;
let lastServerSync = 0;

function parsePersistedState(parsed: PersistedDeckState): Partial<DeckState> {
	return {
		activeMode: parsed.activeMode,
		cards: parsed.cards.map((c) => ({
			...c,
			createdAt: new Date(c.createdAt)
		})),
		contextPanelCollapsed: parsed.contextPanelCollapsed,
		snapEnabled: parsed.snapEnabled,
		gridSnapEnabled: parsed.gridSnapEnabled,
		layoutMode: parsed.layoutMode || 'freeflow'
	};
}

function createPersistedState(state: DeckState): PersistedDeckState {
	return {
		activeMode: state.activeMode,
		cards: state.cards.map((c) => ({
			...c,
			createdAt: c.createdAt.toISOString()
		})),
		contextPanelCollapsed: state.contextPanelCollapsed,
		snapEnabled: state.snapEnabled,
		gridSnapEnabled: state.gridSnapEnabled,
		layoutMode: state.layoutMode,
		lastModified: Date.now()
	};
}

function loadFromStorage(): Partial<DeckState> | null {
	if (typeof window === 'undefined') return null;

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (stored) {
			const parsed: PersistedDeckState = JSON.parse(stored);
			return parsePersistedState(parsed);
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
			const persisted = createPersistedState(state);
			localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
		} catch (e) {
			console.error('[Deck] Failed to save state to localStorage:', e);
		}
		saveTimer = null;
	}, SAVE_DEBOUNCE_MS);
}

/**
 * Save deck state to server for cross-device sync
 */
async function saveToServer(state: DeckState) {
	if (typeof window === 'undefined' || isLoadingFromServer) return;

	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
	}

	serverSyncTimer = setTimeout(async () => {
		try {
			const persisted = createPersistedState(state);
			await setPreference(SERVER_PREFERENCE_KEY, persisted);
			lastServerSync = Date.now();
			console.log('[Deck] Synced state to server');
		} catch (e) {
			console.error('[Deck] Failed to sync state to server:', e);
		}
		serverSyncTimer = null;
	}, SERVER_SYNC_DEBOUNCE_MS);
}

/**
 * Load deck state from server
 * Returns the server state if newer than local, otherwise null
 */
async function loadFromServer(): Promise<Partial<DeckState> | null> {
	if (typeof window === 'undefined') return null;

	try {
		isLoadingFromServer = true;
		const response = await getPreference<PersistedDeckState>(SERVER_PREFERENCE_KEY);

		if (response && response.value) {
			const serverState = response.value as PersistedDeckState;
			console.log('[Deck] Loaded state from server');
			return parsePersistedState(serverState);
		}
	} catch (e) {
		console.error('[Deck] Failed to load state from server:', e);
	} finally {
		isLoadingFromServer = false;
	}
	return null;
}

/**
 * Merge server state with local state
 * - Server cards take precedence for positions/sizes
 * - Local-only cards are preserved
 * - Server-only cards are added
 */
function mergeStates(local: Partial<DeckState> | null, server: Partial<DeckState> | null): Partial<DeckState> | null {
	if (!server) return local;
	if (!local) return server;

	const localCards = local.cards || [];
	const serverCards = server.cards || [];

	// Create a map of server cards by ID
	const serverCardMap = new Map(serverCards.map(c => [c.id, c]));
	const localCardMap = new Map(localCards.map(c => [c.id, c]));

	// Merged cards: start with server cards, add local-only cards
	const mergedCards: DeckCard[] = [];

	// Add all server cards (they have the latest positions from other devices)
	for (const serverCard of serverCards) {
		mergedCards.push(serverCard);
	}

	// Add local-only cards (created on this device but not yet synced to server)
	for (const localCard of localCards) {
		if (!serverCardMap.has(localCard.id)) {
			mergedCards.push(localCard);
		}
	}

	return {
		...server,
		cards: mergedCards,
		// Preserve local-only runtime state
		focusedCardId: local.focusedCardId
	};
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
		case 'subagent':
			return 'Subagents';
		case 'project':
			return 'Projects';
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
		mobileActiveCardIndex: 0,
		layoutMode: persisted?.layoutMode || 'freeflow'
	};

	const { subscribe, set, update } = writable<DeckState>(initialState);

	/**
	 * Helper to update state and persist (local + server)
	 */
	function updateAndPersist(updater: (state: DeckState) => DeckState): void {
		update((state) => {
			const newState = updater(state);
			saveToStorage(newState);
			saveToServer(newState); // Sync to server for cross-device access
			return newState;
		});
	}

	return {
		subscribe,

		// ========================================================================
		// Server Sync
		// ========================================================================

		/**
		 * Sync state from server (call on page load/focus)
		 */
		async syncFromServer(): Promise<void> {
			const serverState = await loadFromServer();
			if (serverState) {
				update((state) => {
					const merged = mergeStates(
						{ ...state },
						serverState
					);
					if (merged) {
						const newState = {
							...state,
							...merged,
							// Recalculate nextZIndex
							nextZIndex: merged.cards
								? Math.max(...merged.cards.map((c) => c.zIndex), state.nextZIndex) + 1
								: state.nextZIndex
						};
						// Save merged state back to localStorage
						saveToStorage(newState);
						return newState;
					}
					return state;
				});
			}
		},

		/**
		 * Force sync current state to server
		 */
		async forceServerSync(): Promise<void> {
			const state = get({ subscribe });
			const persisted = createPersistedState(state);
			try {
				await setPreference(SERVER_PREFERENCE_KEY, persisted);
				console.log('[Deck] Force synced state to server');
			} catch (e) {
				console.error('[Deck] Failed to force sync to server:', e);
			}
		},

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
		 * In focus mode: minimizes current maximized card and maximizes the new card
		 * In other modes: automatically reapplies the current layout mode
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

				// Ensure new card gets the highest zIndex (safeguard against sync issues)
				const maxExistingZ = state.cards.length > 0
					? Math.max(...state.cards.map(c => c.zIndex))
					: 0;
				const newZIndex = Math.max(state.nextZIndex, maxExistingZ + 1);

				// In focus mode, new card starts maximized
				const isFocusMode = state.layoutMode === 'focus';

				const newCard: DeckCard = {
					id: cardId,
					type,
					title: data?.title || getDefaultTitle(type),
					minimized: false,
					maximized: isFocusMode, // Maximize in focus mode
					position,
					size: data?.size || { ...DEFAULT_CARD_SIZE },
					zIndex: newZIndex,
					snappedTo: null,
					dataId: data?.dataId ?? null,
					meta: data?.meta,
					createdAt: new Date(),
					// Save position/size for when we exit focus mode
					savedPosition: isFocusMode ? position : undefined,
					savedSize: isFocusMode ? (data?.size || { ...DEFAULT_CARD_SIZE }) : undefined
				};

				let updatedCards: DeckCard[];

				if (isFocusMode) {
					// In focus mode: minimize currently maximized card, then add new maximized card
					updatedCards = state.cards.map((c) => {
						if (c.maximized) {
							return {
								...c,
								minimized: true,
								maximized: false
							};
						}
						return c;
					});
					updatedCards.push(newCard);
				} else {
					updatedCards = [...state.cards, newCard];
				}

				let newState: DeckState = {
					...state,
					cards: updatedCards,
					focusedCardId: cardId,
					nextZIndex: newZIndex + 1,
					// On mobile, navigate to the newly added card (it's the last visible card)
					mobileActiveCardIndex: state.isMobile
						? updatedCards.filter(c => !c.minimized).length - 1
						: state.mobileActiveCardIndex
				};

				// Auto-reapply layout mode if not freeflow (skip for focus, already handled)
				if (state.layoutMode !== 'freeflow' && state.layoutMode !== 'focus') {
					newState = this.applyLayout(newState, state.layoutMode);
				}

				saveToStorage(newState);
				saveToServer(newState);
				return newState;
			});

			return cardId;
		},

		/**
		 * Remove a card from the deck
		 * Automatically reapplies the current layout mode if not freeflow
		 */
		removeCard(id: string): void {
			update((state) => {
				let newState: DeckState = {
					...state,
					cards: state.cards.filter((c) => c.id !== id),
					focusedCardId: state.focusedCardId === id ? null : state.focusedCardId
				};

				// Auto-reapply layout mode if not freeflow
				if (state.layoutMode !== 'freeflow') {
					newState = this.applyLayout(newState, state.layoutMode);
				}

				saveToStorage(newState);
				saveToServer(newState);
				return newState;
			});
		},

		// ========================================================================
		// Focus Management
		// ========================================================================

		/**
		 * Focus a card (bring to front)
		 * Note: This only updates focusedCardId and zIndex, does NOT persist to server
		 * to avoid position sync issues during frequent focus changes
		 */
		focusCard(id: string): void {
			update((state) => {
				const card = state.cards.find((c) => c.id === id);
				if (!card || card.minimized) {
					return state;
				}

				// Ensure focused card gets the highest zIndex
				const maxExistingZ = state.cards.length > 0
					? Math.max(...state.cards.map(c => c.zIndex))
					: 0;
				const newZIndex = Math.max(state.nextZIndex, maxExistingZ + 1);

				const newState = {
					...state,
					cards: state.cards.map((c) =>
						c.id === id ? { ...c, zIndex: newZIndex } : c
					),
					focusedCardId: id,
					nextZIndex: newZIndex + 1
				};

				// Only save to localStorage, not server (avoid position sync issues)
				saveToStorage(newState);
				return newState;
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
		 * Automatically reapplies the current layout mode if not freeflow
		 */
		minimizeCard(id: string): void {
			update((state) => {
				let newState: DeckState = {
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
				};

				// Auto-reapply layout mode if not freeflow
				if (state.layoutMode !== 'freeflow') {
					newState = this.applyLayout(newState, state.layoutMode);
				}

				saveToStorage(newState);
				saveToServer(newState);
				return newState;
			});
		},

		/**
		 * Restore a card from minimized state
		 * In focus mode: minimizes the current maximized card and maximizes the restored card
		 * In other modes: automatically reapplies the current layout mode
		 */
		restoreCard(id: string): void {
			update((state) => {
				// Ensure restored card gets the highest zIndex
				const maxExistingZ = state.cards.length > 0
					? Math.max(...state.cards.map(c => c.zIndex))
					: 0;
				const newZIndex = Math.max(state.nextZIndex, maxExistingZ + 1);

				let newState: DeckState;

				if (state.layoutMode === 'focus') {
					// Focus mode: swap maximized card with restored card
					newState = {
						...state,
						cards: state.cards.map((c) => {
							if (c.id === id) {
								// Restore and maximize this card
								return {
									...c,
									minimized: false,
									maximized: true,
									zIndex: newZIndex
								};
							} else if (c.maximized) {
								// Minimize the currently maximized card
								return {
									...c,
									minimized: true,
									maximized: false
								};
							}
							return c;
						}),
						focusedCardId: id,
						nextZIndex: newZIndex + 1
					};
				} else {
					// Other modes: just restore the card
					newState = {
						...state,
						cards: state.cards.map((c) =>
							c.id === id
								? {
										...c,
										minimized: false,
										zIndex: newZIndex
									}
								: c
						),
						focusedCardId: id,
						nextZIndex: newZIndex + 1
					};

					// Auto-reapply layout mode if not freeflow
					if (state.layoutMode !== 'freeflow') {
						newState = this.applyLayout(newState, state.layoutMode);
					}
				}

				saveToStorage(newState);
				saveToServer(newState);
				return newState;
			});
		},

		// ========================================================================
		// Maximize/Unmaximize
		// ========================================================================

		/**
		 * Maximize a card to fill the workspace
		 */
		maximizeCard(id: string): void {
			updateAndPersist((state) => {
				// Ensure maximized card gets the highest zIndex
				const maxExistingZ = state.cards.length > 0
					? Math.max(...state.cards.map(c => c.zIndex))
					: 0;
				const newZIndex = Math.max(state.nextZIndex, maxExistingZ + 1);

				return {
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
									zIndex: newZIndex
								}
							: c
					),
					focusedCardId: id,
					nextZIndex: newZIndex + 1
				};
			});
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

				// Ensure snapped card gets the highest zIndex
				const maxExistingZ = state.cards.length > 0
					? Math.max(...state.cards.map(c => c.zIndex))
					: 0;
				const newZIndex = Math.max(state.nextZIndex, maxExistingZ + 1);

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
									zIndex: newZIndex
								}
							: c
					),
					focusedCardId: id,
					nextZIndex: newZIndex + 1
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
				mobileActiveCardIndex: 0,
				layoutMode: 'freeflow'
			};
			set(freshState);
			saveToStorage(freshState);
		},

		// ========================================================================
		// Layout Mode Management
		// ========================================================================

		/**
		 * Set layout mode and rearrange cards accordingly
		 * When leaving focus mode, restores all minimized cards to visible state
		 */
		setLayoutMode(mode: LayoutMode): void {
			updateAndPersist((state) => {
				let newState = { ...state, layoutMode: mode };

				// If leaving focus mode, restore all cards first
				if (state.layoutMode === 'focus' && mode !== 'focus') {
					newState = {
						...newState,
						cards: newState.cards.map((card) => ({
							...card,
							// Unmaximize any maximized cards
							maximized: false,
							// Restore minimized cards (that were minimized by focus mode)
							minimized: false,
							// Restore saved position/size if available
							position: card.savedPosition || card.position,
							size: card.savedSize || card.size,
							savedPosition: undefined,
							savedSize: undefined
						}))
					};
				}

				// Apply layout arrangement based on mode
				if (mode !== 'freeflow') {
					return this.applyLayout(newState, mode);
				}

				return newState;
			});
		},

		/**
		 * Get current layout mode
		 */
		getLayoutMode(): LayoutMode {
			const state = get({ subscribe });
			return state.layoutMode;
		},

		/**
		 * Apply layout arrangement to cards
		 */
		applyLayout(state: DeckState, mode: LayoutMode): DeckState {
			const visibleCards = state.cards.filter((c) => !c.minimized);
			if (visibleCards.length === 0) return state;

			const { width: boundsW, height: boundsH } = state.workspaceBounds;
			const padding = 8; // Padding between cards
			const count = visibleCards.length;

			let updatedCards: DeckCard[];

			switch (mode) {
				case 'sidebyside':
					// Cards arranged side-by-side in vertical columns
					updatedCards = this.applySideBySideLayout(state.cards, boundsW, boundsH, padding);
					break;

				case 'tile':
					// Cards tiled in a grid pattern
					updatedCards = this.applyTileLayout(state.cards, boundsW, boundsH, padding);
					break;

				case 'stack':
					// Cards stacked in a cascade pattern
					updatedCards = this.applyStackLayout(state.cards, boundsW, boundsH);
					break;

				case 'focus':
					// One main card takes most space, others in sidebar
					updatedCards = this.applyFocusLayout(state.cards, boundsW, boundsH, padding, state.focusedCardId);
					break;

				default:
					// freeflow - don't modify positions
					return state;
			}

			return { ...state, cards: updatedCards };
		},

		/**
		 * Side-by-side layout: Cards arranged in equal-width vertical columns
		 */
		applySideBySideLayout(cards: DeckCard[], boundsW: number, boundsH: number, padding: number): DeckCard[] {
			const visibleCards = cards.filter((c) => !c.minimized);
			const count = visibleCards.length;
			if (count === 0) return cards;

			// Calculate column width - max 4 columns, then start scrolling
			const maxCols = Math.min(count, 4);
			const cardWidth = (boundsW - (maxCols + 1) * padding) / maxCols;
			const cardHeight = boundsH - padding * 2;

			let colIndex = 0;
			return cards.map((card) => {
				if (card.minimized) return card;

				const x = padding + colIndex * (cardWidth + padding);
				colIndex++;

				return {
					...card,
					position: { x, y: padding },
					size: { width: cardWidth, height: cardHeight },
					maximized: false,
					snappedTo: null as SnapZone,
					savedPosition: card.savedPosition || { ...card.position },
					savedSize: card.savedSize || { ...card.size }
				};
			});
		},

		/**
		 * Tile layout: Cards tiled in a grid pattern with equal sizes
		 */
		applyTileLayout(cards: DeckCard[], boundsW: number, boundsH: number, padding: number): DeckCard[] {
			const visibleCards = cards.filter((c) => !c.minimized);
			const count = visibleCards.length;
			if (count === 0) return cards;

			// Calculate optimal grid dimensions
			const cols = Math.ceil(Math.sqrt(count));
			const rows = Math.ceil(count / cols);

			const cardWidth = (boundsW - (cols + 1) * padding) / cols;
			const cardHeight = (boundsH - (rows + 1) * padding) / rows;

			let idx = 0;
			return cards.map((card) => {
				if (card.minimized) return card;

				const col = idx % cols;
				const row = Math.floor(idx / cols);
				idx++;

				return {
					...card,
					position: {
						x: padding + col * (cardWidth + padding),
						y: padding + row * (cardHeight + padding)
					},
					size: { width: cardWidth, height: cardHeight },
					maximized: false,
					snappedTo: null as SnapZone,
					savedPosition: card.savedPosition || { ...card.position },
					savedSize: card.savedSize || { ...card.size }
				};
			});
		},

		/**
		 * Stack layout: Cards stacked in a cascade pattern (like a deck of cards)
		 */
		applyStackLayout(cards: DeckCard[], boundsW: number, boundsH: number): DeckCard[] {
			const visibleCards = cards.filter((c) => !c.minimized);
			if (visibleCards.length === 0) return cards;

			// Card size for stack view - slightly smaller to show cascade
			const cardWidth = Math.min(600, boundsW * 0.7);
			const cardHeight = Math.min(500, boundsH * 0.8);

			// Starting position - centered with room for cascade
			const startX = (boundsW - cardWidth) / 2 - (visibleCards.length * 15);
			const startY = (boundsH - cardHeight) / 2 - (visibleCards.length * 10);

			const cascadeOffsetX = 30;
			const cascadeOffsetY = 25;

			let idx = 0;
			let nextZ = Math.max(...cards.map(c => c.zIndex), 0) + 1;

			return cards.map((card) => {
				if (card.minimized) return card;

				const x = Math.max(20, startX + idx * cascadeOffsetX);
				const y = Math.max(20, startY + idx * cascadeOffsetY);
				const zIndex = nextZ + idx;
				idx++;

				return {
					...card,
					position: { x, y },
					size: { width: cardWidth, height: cardHeight },
					zIndex,
					maximized: false,
					snappedTo: null as SnapZone,
					savedPosition: card.savedPosition || { ...card.position },
					savedSize: card.savedSize || { ...card.size }
				};
			});
		},

		/**
		 * Focus layout: One card is maximized, all others are minimized
		 * When switching cards in focus mode, the current card is minimized
		 * and the new card is maximized
		 */
		applyFocusLayout(cards: DeckCard[], boundsW: number, boundsH: number, padding: number, focusedCardId: string | null): DeckCard[] {
			// Find cards that aren't already minimized (before this layout change)
			const nonMinimizedCards = cards.filter((c) => !c.minimized);
			if (nonMinimizedCards.length === 0) return cards;

			// Find the focused card, or use the first non-minimized card
			const focusedCard = focusedCardId
				? nonMinimizedCards.find(c => c.id === focusedCardId) || nonMinimizedCards[0]
				: nonMinimizedCards[0];

			const highestZ = Math.max(...cards.map(c => c.zIndex), 0);

			return cards.map((card) => {
				if (card.id === focusedCard?.id) {
					// Focused card: maximize it
					return {
						...card,
						minimized: false,
						maximized: true,
						zIndex: highestZ + 1,
						// Save current position/size for when we exit focus mode
						savedPosition: card.savedPosition || { ...card.position },
						savedSize: card.savedSize || { ...card.size }
					};
				} else if (!card.minimized) {
					// Other non-minimized cards: minimize them
					return {
						...card,
						minimized: true,
						maximized: false,
						// Save current position/size for restore
						savedPosition: card.savedPosition || { ...card.position },
						savedSize: card.savedSize || { ...card.size }
					};
				}
				// Already minimized cards stay minimized
				return card;
			});
		},

		/**
		 * Reapply current layout (useful when cards are added/removed or window resized)
		 */
		reapplyLayout(): void {
			update((state) => {
				if (state.layoutMode === 'freeflow') return state;

				const newState = this.applyLayout(state, state.layoutMode);
				saveToStorage(newState);
				saveToServer(newState);
				return newState;
			});
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

// Layout mode
export const layoutMode = derived(deck, ($deck) => $deck.layoutMode);

// Check if a specific card is focused
export const isCardFocused = (cardId: string) =>
	derived(focusedCardId, ($id) => $id === cardId);

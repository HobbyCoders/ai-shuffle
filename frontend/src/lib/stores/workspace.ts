/**
 * Workspace Store - Universal Card System
 *
 * Manages the desktop-like workspace where cards can be:
 * - Opened, closed, minimized, maximized
 * - Dragged and repositioned
 * - Resized
 * - Persisted across sessions
 *
 * Each "mode" (Chat, Canvas, Tasks, etc.) has its own workspace.
 */

import { writable, derived, get } from 'svelte/store';
import { api } from '$lib/api/client';

// ============================================================================
// Types
// ============================================================================

export type CardState = 'open' | 'minimized' | 'maximized';

export type CardType = 'chat' | 'settings' | 'profile' | 'project';

export type WorkspaceMode = 'chat'; // Will expand: 'canvas' | 'tasks' | etc.

export interface CardPosition {
	x: number;
	y: number;
}

export interface CardSize {
	width: number;
	height: number;
}

export interface WorkspaceCard {
	id: string;
	type: CardType;
	title: string;
	state: CardState;
	position: CardPosition;
	size: CardSize;
	zIndex: number;
	// Card-specific data reference
	// For chat cards, this is the sessionId (or null for new chat)
	dataId: string | null;
	// Additional metadata per card type
	meta?: Record<string, unknown>;
}

export interface Workspace {
	mode: WorkspaceMode;
	cards: WorkspaceCard[];
	nextZIndex: number;
}

interface WorkspaceState {
	workspaces: Record<WorkspaceMode, Workspace>;
	activeMode: WorkspaceMode;
	// Track which card is focused (for keyboard shortcuts, etc.)
	focusedCardId: string | null;
}

// ============================================================================
// Constants
// ============================================================================

const DEFAULT_CARD_SIZE: CardSize = { width: 600, height: 500 };
const MIN_CARD_SIZE: CardSize = { width: 320, height: 200 };
const CARD_CASCADE_OFFSET = 30;

const PREFERENCE_KEY = 'workspace_state';
const SAVE_DEBOUNCE_MS = 1000;

// ============================================================================
// Persistence Helpers
// ============================================================================

interface PersistedWorkspaceState {
	workspaces: Record<WorkspaceMode, Workspace>;
	activeMode: WorkspaceMode;
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;
let pendingState: WorkspaceState | null = null;
let isInitializing = false;

function flushPendingSave() {
	if (!pendingState || isInitializing) return;

	if (saveTimer) {
		clearTimeout(saveTimer);
		saveTimer = null;
	}

	const persistedState: PersistedWorkspaceState = {
		workspaces: pendingState.workspaces,
		activeMode: pendingState.activeMode
	};

	const data = JSON.stringify({
		key: PREFERENCE_KEY,
		value: persistedState
	});

	navigator.sendBeacon(
		`/api/v1/preferences/${PREFERENCE_KEY}`,
		new Blob([data], { type: 'application/json' })
	);

	pendingState = null;
}

// Register beforeunload handler
if (typeof window !== 'undefined') {
	window.addEventListener('beforeunload', flushPendingSave);
}

// ============================================================================
// Store Creation
// ============================================================================

function createDefaultWorkspace(mode: WorkspaceMode): Workspace {
	return {
		mode,
		cards: [],
		nextZIndex: 1
	};
}

function createInitialState(): WorkspaceState {
	return {
		workspaces: {
			chat: createDefaultWorkspace('chat')
		},
		activeMode: 'chat',
		focusedCardId: null
	};
}

function generateCardId(): string {
	return `card-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

function calculateCascadePosition(existingCards: WorkspaceCard[]): CardPosition {
	if (existingCards.length === 0) {
		return { x: 50, y: 50 };
	}

	// Find the last opened card's position and offset from it
	const lastCard = existingCards[existingCards.length - 1];
	return {
		x: lastCard.position.x + CARD_CASCADE_OFFSET,
		y: lastCard.position.y + CARD_CASCADE_OFFSET
	};
}

function createWorkspaceStore() {
	const { subscribe, set, update } = writable<WorkspaceState>(createInitialState());

	// Debounced save to backend
	function scheduleSave(state: WorkspaceState) {
		if (isInitializing) return;

		pendingState = state;

		if (saveTimer) {
			clearTimeout(saveTimer);
		}

		saveTimer = setTimeout(async () => {
			if (!pendingState) return;

			const persistedState: PersistedWorkspaceState = {
				workspaces: pendingState.workspaces,
				activeMode: pendingState.activeMode
			};

			try {
				await api.put(`/preferences/${PREFERENCE_KEY}`, {
					key: PREFERENCE_KEY,
					value: persistedState
				});
				pendingState = null;
			} catch (err) {
				console.error('[Workspace] Failed to save state:', err);
			}
		}, SAVE_DEBOUNCE_MS);
	}

	return {
		subscribe,

		/**
		 * Initialize workspace from persisted state
		 */
		async init() {
			isInitializing = true;

			try {
				const response = await api.get<{ key: string; value: PersistedWorkspaceState } | null>(
					`/preferences/${PREFERENCE_KEY}`
				);

				if (response?.value) {
					const persisted = response.value;

					// Merge persisted state with defaults (in case new modes are added)
					const state: WorkspaceState = {
						workspaces: {
							...createInitialState().workspaces,
							...persisted.workspaces
						},
						activeMode: persisted.activeMode || 'chat',
						focusedCardId: null
					};

					set(state);
					console.log('[Workspace] Restored state:', state);
				}
			} catch (err) {
				console.error('[Workspace] Failed to load state:', err);
			} finally {
				isInitializing = false;
			}
		},

		/**
		 * Switch active mode
		 */
		setMode(mode: WorkspaceMode) {
			update(state => {
				const newState = {
					...state,
					activeMode: mode,
					focusedCardId: null
				};
				scheduleSave(newState);
				return newState;
			});
		},

		/**
		 * Open a new card in the current workspace
		 */
		openCard(
			type: CardType,
			options: {
				title?: string;
				dataId?: string | null;
				position?: CardPosition;
				size?: CardSize;
				meta?: Record<string, unknown>;
			} = {}
		): string {
			const cardId = generateCardId();

			update(state => {
				const workspace = state.workspaces[state.activeMode];
				const position = options.position || calculateCascadePosition(workspace.cards);

				const newCard: WorkspaceCard = {
					id: cardId,
					type,
					title: options.title || getDefaultTitle(type),
					state: 'open',
					position,
					size: options.size || { ...DEFAULT_CARD_SIZE },
					zIndex: workspace.nextZIndex,
					dataId: options.dataId ?? null,
					meta: options.meta
				};

				const newState = {
					...state,
					focusedCardId: cardId,
					workspaces: {
						...state.workspaces,
						[state.activeMode]: {
							...workspace,
							cards: [...workspace.cards, newCard],
							nextZIndex: workspace.nextZIndex + 1
						}
					}
				};

				scheduleSave(newState);
				return newState;
			});

			return cardId;
		},

		/**
		 * Close a card
		 */
		closeCard(cardId: string) {
			update(state => {
				const workspace = state.workspaces[state.activeMode];
				const newState = {
					...state,
					focusedCardId: state.focusedCardId === cardId ? null : state.focusedCardId,
					workspaces: {
						...state.workspaces,
						[state.activeMode]: {
							...workspace,
							cards: workspace.cards.filter(c => c.id !== cardId)
						}
					}
				};
				scheduleSave(newState);
				return newState;
			});
		},

		/**
		 * Minimize a card (move to dock)
		 */
		minimizeCard(cardId: string) {
			update(state => {
				const workspace = state.workspaces[state.activeMode];
				const newState = {
					...state,
					focusedCardId: state.focusedCardId === cardId ? null : state.focusedCardId,
					workspaces: {
						...state.workspaces,
						[state.activeMode]: {
							...workspace,
							cards: workspace.cards.map(c =>
								c.id === cardId ? { ...c, state: 'minimized' as CardState } : c
							)
						}
					}
				};
				scheduleSave(newState);
				return newState;
			});
		},

		/**
		 * Maximize a card (full workspace)
		 */
		maximizeCard(cardId: string) {
			update(state => {
				const workspace = state.workspaces[state.activeMode];
				const newState = {
					...state,
					focusedCardId: cardId,
					workspaces: {
						...state.workspaces,
						[state.activeMode]: {
							...workspace,
							cards: workspace.cards.map(c =>
								c.id === cardId
									? { ...c, state: 'maximized' as CardState, zIndex: workspace.nextZIndex }
									: c
							),
							nextZIndex: workspace.nextZIndex + 1
						}
					}
				};
				scheduleSave(newState);
				return newState;
			});
		},

		/**
		 * Restore a card from minimized/maximized to open
		 */
		restoreCard(cardId: string) {
			update(state => {
				const workspace = state.workspaces[state.activeMode];
				const newState = {
					...state,
					focusedCardId: cardId,
					workspaces: {
						...state.workspaces,
						[state.activeMode]: {
							...workspace,
							cards: workspace.cards.map(c =>
								c.id === cardId
									? { ...c, state: 'open' as CardState, zIndex: workspace.nextZIndex }
									: c
							),
							nextZIndex: workspace.nextZIndex + 1
						}
					}
				};
				scheduleSave(newState);
				return newState;
			});
		},

		/**
		 * Bring a card to front (focus)
		 */
		focusCard(cardId: string) {
			update(state => {
				const workspace = state.workspaces[state.activeMode];
				const card = workspace.cards.find(c => c.id === cardId);

				if (!card || card.state === 'minimized') {
					return state;
				}

				const newState = {
					...state,
					focusedCardId: cardId,
					workspaces: {
						...state.workspaces,
						[state.activeMode]: {
							...workspace,
							cards: workspace.cards.map(c =>
								c.id === cardId
									? { ...c, zIndex: workspace.nextZIndex }
									: c
							),
							nextZIndex: workspace.nextZIndex + 1
						}
					}
				};
				scheduleSave(newState);
				return newState;
			});
		},

		/**
		 * Update card position (from drag)
		 */
		moveCard(cardId: string, position: CardPosition) {
			update(state => {
				const workspace = state.workspaces[state.activeMode];
				const newState = {
					...state,
					workspaces: {
						...state.workspaces,
						[state.activeMode]: {
							...workspace,
							cards: workspace.cards.map(c =>
								c.id === cardId ? { ...c, position } : c
							)
						}
					}
				};
				scheduleSave(newState);
				return newState;
			});
		},

		/**
		 * Update card size (from resize)
		 */
		resizeCard(cardId: string, size: CardSize) {
			update(state => {
				const workspace = state.workspaces[state.activeMode];
				// Enforce minimum size
				const constrainedSize = {
					width: Math.max(size.width, MIN_CARD_SIZE.width),
					height: Math.max(size.height, MIN_CARD_SIZE.height)
				};

				const newState = {
					...state,
					workspaces: {
						...state.workspaces,
						[state.activeMode]: {
							...workspace,
							cards: workspace.cards.map(c =>
								c.id === cardId ? { ...c, size: constrainedSize } : c
							)
						}
					}
				};
				scheduleSave(newState);
				return newState;
			});
		},

		/**
		 * Update card title
		 */
		setCardTitle(cardId: string, title: string) {
			update(state => {
				const workspace = state.workspaces[state.activeMode];
				const newState = {
					...state,
					workspaces: {
						...state.workspaces,
						[state.activeMode]: {
							...workspace,
							cards: workspace.cards.map(c =>
								c.id === cardId ? { ...c, title } : c
							)
						}
					}
				};
				scheduleSave(newState);
				return newState;
			});
		},

		/**
		 * Update card dataId (e.g., when a new chat gets a sessionId)
		 */
		setCardDataId(cardId: string, dataId: string | null) {
			update(state => {
				const workspace = state.workspaces[state.activeMode];
				const newState = {
					...state,
					workspaces: {
						...state.workspaces,
						[state.activeMode]: {
							...workspace,
							cards: workspace.cards.map(c =>
								c.id === cardId ? { ...c, dataId } : c
							)
						}
					}
				};
				scheduleSave(newState);
				return newState;
			});
		},

		/**
		 * Update card metadata
		 */
		setCardMeta(cardId: string, meta: Record<string, unknown>) {
			update(state => {
				const workspace = state.workspaces[state.activeMode];
				const newState = {
					...state,
					workspaces: {
						...state.workspaces,
						[state.activeMode]: {
							...workspace,
							cards: workspace.cards.map(c =>
								c.id === cardId ? { ...c, meta: { ...c.meta, ...meta } } : c
							)
						}
					}
				};
				scheduleSave(newState);
				return newState;
			});
		},

		/**
		 * Get a card by ID
		 */
		getCard(cardId: string): WorkspaceCard | undefined {
			const state = get({ subscribe });
			const workspace = state.workspaces[state.activeMode];
			return workspace.cards.find(c => c.id === cardId);
		},

		/**
		 * Find a card by dataId (e.g., find chat card for a session)
		 */
		findCardByDataId(dataId: string): WorkspaceCard | undefined {
			const state = get({ subscribe });
			const workspace = state.workspaces[state.activeMode];
			return workspace.cards.find(c => c.dataId === dataId);
		},

		/**
		 * Open or focus an existing card for a dataId
		 * Returns the card ID
		 */
		openOrFocusCard(
			type: CardType,
			dataId: string,
			options: { title?: string; meta?: Record<string, unknown> } = {}
		): string {
			const state = get({ subscribe });
			const workspace = state.workspaces[state.activeMode];
			const existingCard = workspace.cards.find(c => c.dataId === dataId && c.type === type);

			if (existingCard) {
				// Card already exists - restore if minimized, then focus
				if (existingCard.state === 'minimized') {
					this.restoreCard(existingCard.id);
				} else {
					this.focusCard(existingCard.id);
				}
				return existingCard.id;
			}

			// Open new card
			return this.openCard(type, { ...options, dataId });
		}
	};
}

function getDefaultTitle(type: CardType): string {
	switch (type) {
		case 'chat':
			return 'New Chat';
		case 'settings':
			return 'Settings';
		case 'profile':
			return 'Profile';
		case 'project':
			return 'Project';
		default:
			return 'Card';
	}
}

// ============================================================================
// Export Store & Derived Stores
// ============================================================================

export const workspace = createWorkspaceStore();

// Current mode
export const activeMode = derived(workspace, $ws => $ws.activeMode);

// Current workspace
export const activeWorkspace = derived(workspace, $ws => $ws.workspaces[$ws.activeMode]);

// All cards in current workspace
export const cards = derived(activeWorkspace, $aw => $aw.cards);

// Open cards (not minimized)
export const openCards = derived(cards, $cards =>
	$cards.filter(c => c.state !== 'minimized')
);

// Minimized cards (for dock)
export const minimizedCards = derived(cards, $cards =>
	$cards.filter(c => c.state === 'minimized')
);

// Maximized card (if any)
export const maximizedCard = derived(cards, $cards =>
	$cards.find(c => c.state === 'maximized')
);

// Focused card ID
export const focusedCardId = derived(workspace, $ws => $ws.focusedCardId);

// Check if a card is focused
export const isCardFocused = (cardId: string) => derived(focusedCardId, $id => $id === cardId);

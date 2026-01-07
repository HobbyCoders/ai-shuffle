/**
 * Navigator Store - Card Navigator State Management
 *
 * Manages the AI Shuffle card navigator state including:
 * - Card ordering preferences
 * - Custom decks (user-created groupings)
 * - Edit mode state
 *
 * Persists to localStorage and syncs to server for cross-device access.
 */

import { writable, derived, get } from 'svelte/store';
import { getPreference, setPreference } from '$lib/api/client';

// ============================================================================
// Types
// ============================================================================

/**
 * Custom deck for grouping cards
 */
export interface CustomDeck {
	id: string;
	name: string;
	icon: string; // Lucide icon name
	color: string; // CSS color for accent
	cardIds: string[]; // IDs of cards in this deck (action cards + thread IDs)
	createdAt: string;
	updatedAt: string;
}

/**
 * View mode for recent sessions display
 */
export type RecentSessionsViewMode = 'list' | 'timeline';

/**
 * Persisted navigator state
 */
export interface NavigatorPersistedState {
	cardOrder: string[]; // Card IDs in user's preferred order
	decks: CustomDeck[];
	recentSessionsView: RecentSessionsViewMode; // User's preferred view for recent sessions
	lastModified: number;
}

/**
 * Full navigator state (includes runtime-only state)
 */
export interface NavigatorState extends NavigatorPersistedState {
	editMode: boolean;
	initialized: boolean;
}

// ============================================================================
// Constants
// ============================================================================

const STORAGE_KEY = 'navigator_state';
const SERVER_PREFERENCE_KEY = 'navigator_state';
const SAVE_DEBOUNCE_MS = 500;
const SERVER_SYNC_DEBOUNCE_MS = 1000;

// Default deck colors (AI Shuffle palette)
export const DECK_COLORS = [
	'#22d3ee', // Cyan (primary)
	'#a78bfa', // Purple
	'#f472b6', // Pink
	'#fb923c', // Orange
	'#4ade80', // Green
	'#facc15', // Yellow
	'#60a5fa', // Blue
	'#f87171', // Red
];

// Default deck icons
export const DECK_ICONS = [
	'Folder',
	'Star',
	'Heart',
	'Zap',
	'Bookmark',
	'Flag',
	'Tag',
	'Archive',
	'Briefcase',
	'Code',
	'Database',
	'Globe',
];

// ============================================================================
// Persistence Helpers
// ============================================================================

let saveTimer: ReturnType<typeof setTimeout> | null = null;
let serverSyncTimer: ReturnType<typeof setTimeout> | null = null;
let isLoadingFromServer = false;
let hasSyncedFromServer = false; // Prevents writing to server before initial sync completes

function generateId(): string {
	return `deck-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

function loadFromStorage(): NavigatorPersistedState | null {
	if (typeof window === 'undefined') return null;

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (stored) {
			return JSON.parse(stored);
		}
	} catch (e) {
		console.error('[Navigator] Failed to load state from localStorage:', e);
	}
	return null;
}

function saveToStorage(state: NavigatorPersistedState) {
	if (typeof window === 'undefined') return;

	if (saveTimer) {
		clearTimeout(saveTimer);
	}

	saveTimer = setTimeout(() => {
		try {
			localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
		} catch (e) {
			console.error('[Navigator] Failed to save state to localStorage:', e);
		}
		saveTimer = null;
	}, SAVE_DEBOUNCE_MS);
}

async function saveToServer(state: NavigatorPersistedState) {
	if (typeof window === 'undefined' || isLoadingFromServer) return;

	// Don't write to server until we've completed initial sync
	// This prevents empty local state from overwriting server data on first load
	if (!hasSyncedFromServer) {
		console.log('[Navigator] Skipping server save - initial sync not complete');
		return;
	}

	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
	}

	serverSyncTimer = setTimeout(async () => {
		try {
			await setPreference(SERVER_PREFERENCE_KEY, state);
			console.log('[Navigator] Synced state to server');
		} catch (e) {
			console.error('[Navigator] Failed to sync state to server:', e);
		}
		serverSyncTimer = null;
	}, SERVER_SYNC_DEBOUNCE_MS);
}

async function loadFromServer(): Promise<NavigatorPersistedState | null> {
	if (typeof window === 'undefined') return null;

	try {
		isLoadingFromServer = true;
		const response = await getPreference<NavigatorPersistedState>(SERVER_PREFERENCE_KEY);

		if (response && response.value) {
			console.log('[Navigator] Loaded state from server');
			return response.value as NavigatorPersistedState;
		}
	} catch (e) {
		console.error('[Navigator] Failed to load state from server:', e);
	} finally {
		isLoadingFromServer = false;
	}
	return null;
}

// ============================================================================
// Store Creation
// ============================================================================

function createNavigatorStore() {
	const persisted = loadFromStorage();

	const initialState: NavigatorState = {
		cardOrder: persisted?.cardOrder || [],
		decks: persisted?.decks || [],
		recentSessionsView: persisted?.recentSessionsView || 'list',
		lastModified: persisted?.lastModified || Date.now(),
		editMode: false,
		initialized: false,
	};

	const { subscribe, set, update } = writable<NavigatorState>(initialState);

	/**
	 * Get persisted state from full state
	 */
	function getPersistedState(state: NavigatorState): NavigatorPersistedState {
		return {
			cardOrder: state.cardOrder,
			decks: state.decks,
			recentSessionsView: state.recentSessionsView,
			lastModified: Date.now(),
		};
	}

	/**
	 * Update and persist state
	 */
	function updateAndPersist(updater: (state: NavigatorState) => NavigatorState): void {
		update((state) => {
			const newState = updater(state);
			const persisted = getPersistedState(newState);
			saveToStorage(persisted);
			saveToServer(persisted);
			return { ...newState, lastModified: persisted.lastModified };
		});
	}

	return {
		subscribe,

		// ========================================================================
		// Initialization & Sync
		// ========================================================================

		/**
		 * Initialize store and sync from server
		 *
		 * Merge strategy:
		 * 1. If local is empty (no decks), always use server state
		 * 2. If server is empty but local has data, keep local
		 * 3. If both have data, merge: server decks win, add local-only decks
		 */
		async initialize(): Promise<void> {
			const serverState = await loadFromServer();

			update((state) => {
				const localDecks = state.decks || [];
				const serverDecks = serverState?.decks || [];
				const localIsEmpty = localDecks.length === 0 && (state.cardOrder || []).length === 0;
				const serverIsEmpty = serverDecks.length === 0 && (serverState?.cardOrder || []).length === 0;

				let mergedState: NavigatorState;

				if (serverState && !serverIsEmpty) {
					if (localIsEmpty) {
						// Local is empty, use server state entirely
						console.log('[Navigator] Local empty, using server state');
						mergedState = {
							...state,
							...serverState,
							editMode: false,
							initialized: true,
						};
					} else {
						// Both have data - merge them
						// Server decks take precedence, but preserve local-only decks
						const serverDeckIds = new Set(serverDecks.map(d => d.id));
						const localOnlyDecks = localDecks.filter(d => !serverDeckIds.has(d.id));

						// Merge card orders: server order first, then any local-only cards
						const serverCardOrder = serverState.cardOrder || [];
						const localCardOrder = state.cardOrder || [];
						const serverCardOrderSet = new Set(serverCardOrder);
						const localOnlyCards = localCardOrder.filter(id => !serverCardOrderSet.has(id));

						console.log('[Navigator] Merging local and server state');
						mergedState = {
							...state,
							...serverState,
							decks: [...serverDecks, ...localOnlyDecks],
							cardOrder: [...serverCardOrder, ...localOnlyCards],
							editMode: false,
							initialized: true,
						};
					}
				} else {
					// No server state or server is empty - keep local
					console.log('[Navigator] No server state, keeping local');
					mergedState = { ...state, initialized: true };
				}

				// Save merged state to localStorage
				const persisted = {
					cardOrder: mergedState.cardOrder,
					decks: mergedState.decks,
					recentSessionsView: mergedState.recentSessionsView,
					lastModified: Date.now(),
				};
				saveToStorage(persisted);

				return mergedState;
			});

			// Mark sync as complete - now server writes are allowed
			hasSyncedFromServer = true;
			console.log('[Navigator] Initial sync complete, server writes enabled');
		},

		/**
		 * Force sync current state to server
		 */
		async forceSync(): Promise<void> {
			const state = get({ subscribe });
			const persisted = getPersistedState(state);
			try {
				await setPreference(SERVER_PREFERENCE_KEY, persisted);
				console.log('[Navigator] Force synced state to server');
			} catch (e) {
				console.error('[Navigator] Failed to force sync to server:', e);
			}
		},

		// ========================================================================
		// Edit Mode
		// ========================================================================

		/**
		 * Enter edit mode
		 */
		enterEditMode(): void {
			update((state) => ({ ...state, editMode: true }));
		},

		/**
		 * Exit edit mode and save changes
		 */
		exitEditMode(): void {
			update((state) => {
				const newState = { ...state, editMode: false };
				// Persist on exit
				const persisted = getPersistedState(newState);
				saveToStorage(persisted);
				saveToServer(persisted);
				return newState;
			});
		},

		/**
		 * Toggle edit mode
		 */
		toggleEditMode(): void {
			const state = get({ subscribe });
			if (state.editMode) {
				this.exitEditMode();
			} else {
				this.enterEditMode();
			}
		},

		// ========================================================================
		// Recent Sessions View
		// ========================================================================

		/**
		 * Set the view mode for recent sessions (list or timeline)
		 */
		setRecentSessionsView(view: RecentSessionsViewMode): void {
			updateAndPersist((state) => ({
				...state,
				recentSessionsView: view,
			}));
		},

		/**
		 * Toggle between list and timeline views
		 */
		toggleRecentSessionsView(): void {
			update((state) => {
				const newView: RecentSessionsViewMode = state.recentSessionsView === 'list' ? 'timeline' : 'list';
				const newState = { ...state, recentSessionsView: newView };
				const persisted = getPersistedState(newState);
				saveToStorage(persisted);
				saveToServer(persisted);
				return newState;
			});
		},

		// ========================================================================
		// Card Ordering
		// ========================================================================

		/**
		 * Set the order of cards
		 */
		setCardOrder(order: string[]): void {
			updateAndPersist((state) => ({
				...state,
				cardOrder: order,
			}));
		},

		/**
		 * Reorder a card by moving it to a new index
		 */
		reorderCard(cardId: string, newIndex: number): void {
			update((state) => {
				const currentOrder = [...state.cardOrder];
				const currentIndex = currentOrder.indexOf(cardId);

				// If card is not in order yet, we need all card IDs to be passed
				// This is handled by the component which passes the full sorted list
				if (currentIndex === -1) {
					// Insert at new index
					currentOrder.splice(newIndex, 0, cardId);
				} else {
					// Remove from current position
					currentOrder.splice(currentIndex, 1);
					// Insert at new position
					currentOrder.splice(newIndex, 0, cardId);
				}

				return { ...state, cardOrder: currentOrder };
			});
		},

		/**
		 * Move a card to a new position (used during drag)
		 */
		moveCard(fromIndex: number, toIndex: number, allCardIds: string[]): void {
			update((state) => {
				const newOrder = [...allCardIds];
				const [moved] = newOrder.splice(fromIndex, 1);
				newOrder.splice(toIndex, 0, moved);
				return { ...state, cardOrder: newOrder };
			});
		},

		// ========================================================================
		// Deck Management
		// ========================================================================

		/**
		 * Create a new deck
		 */
		createDeck(name: string, icon?: string, color?: string): string {
			const deckId = generateId();
			const now = new Date().toISOString();

			updateAndPersist((state) => ({
				...state,
				decks: [
					...state.decks,
					{
						id: deckId,
						name,
						icon: icon || DECK_ICONS[0],
						color: color || DECK_COLORS[state.decks.length % DECK_COLORS.length],
						cardIds: [],
						createdAt: now,
						updatedAt: now,
					},
				],
			}));

			return deckId;
		},

		/**
		 * Delete a deck (cards return to main view)
		 */
		deleteDeck(deckId: string): void {
			updateAndPersist((state) => ({
				...state,
				decks: state.decks.filter((d) => d.id !== deckId),
			}));
		},

		/**
		 * Rename a deck
		 */
		renameDeck(deckId: string, name: string): void {
			updateAndPersist((state) => ({
				...state,
				decks: state.decks.map((d) =>
					d.id === deckId
						? { ...d, name, updatedAt: new Date().toISOString() }
						: d
				),
			}));
		},

		/**
		 * Update deck icon
		 */
		setDeckIcon(deckId: string, icon: string): void {
			updateAndPersist((state) => ({
				...state,
				decks: state.decks.map((d) =>
					d.id === deckId
						? { ...d, icon, updatedAt: new Date().toISOString() }
						: d
				),
			}));
		},

		/**
		 * Update deck color
		 */
		setDeckColor(deckId: string, color: string): void {
			updateAndPersist((state) => ({
				...state,
				decks: state.decks.map((d) =>
					d.id === deckId
						? { ...d, color, updatedAt: new Date().toISOString() }
						: d
				),
			}));
		},

		/**
		 * Add a card to a deck (removes from main view)
		 */
		addCardToDeck(deckId: string, cardId: string): void {
			updateAndPersist((state) => {
				// First, remove from any other deck
				const decksWithCardRemoved = state.decks.map((d) => ({
					...d,
					cardIds: d.cardIds.filter((id) => id !== cardId),
				}));

				// Then add to target deck
				return {
					...state,
					decks: decksWithCardRemoved.map((d) =>
						d.id === deckId
							? {
									...d,
									cardIds: [...d.cardIds, cardId],
									updatedAt: new Date().toISOString(),
								}
							: d
					),
				};
			});
		},

		/**
		 * Remove a card from a deck (returns to main view)
		 */
		removeCardFromDeck(deckId: string, cardId: string): void {
			updateAndPersist((state) => ({
				...state,
				decks: state.decks.map((d) =>
					d.id === deckId
						? {
								...d,
								cardIds: d.cardIds.filter((id) => id !== cardId),
								updatedAt: new Date().toISOString(),
							}
						: d
				),
			}));
		},

		/**
		 * Reorder cards within a deck
		 */
		reorderDeckCards(deckId: string, cardIds: string[]): void {
			updateAndPersist((state) => ({
				...state,
				decks: state.decks.map((d) =>
					d.id === deckId
						? { ...d, cardIds, updatedAt: new Date().toISOString() }
						: d
				),
			}));
		},

		/**
		 * Get the deck a card belongs to (if any)
		 */
		getCardDeck(cardId: string): CustomDeck | null {
			const state = get({ subscribe });
			return state.decks.find((d) => d.cardIds.includes(cardId)) || null;
		},

		/**
		 * Check if a card is in any deck
		 */
		isCardInDeck(cardId: string): boolean {
			const state = get({ subscribe });
			return state.decks.some((d) => d.cardIds.includes(cardId));
		},

		/**
		 * Get all cards that are NOT in any deck
		 */
		getMainViewCardIds(): string[] {
			const state = get({ subscribe });
			const allDeckCardIds = new Set(state.decks.flatMap((d) => d.cardIds));
			return state.cardOrder.filter((id) => !allDeckCardIds.has(id));
		},

		// ========================================================================
		// Utility
		// ========================================================================

		/**
		 * Get a deck by ID
		 */
		getDeck(deckId: string): CustomDeck | undefined {
			const state = get({ subscribe });
			return state.decks.find((d) => d.id === deckId);
		},

		/**
		 * Reset store to initial state
		 */
		reset(): void {
			const freshState: NavigatorState = {
				cardOrder: [],
				decks: [],
				lastModified: Date.now(),
				editMode: false,
				initialized: true,
			};
			set(freshState);
			saveToStorage(getPersistedState(freshState));
		},
	};
}

// ============================================================================
// Export Store & Derived Stores
// ============================================================================

export const navigator = createNavigatorStore();

// Edit mode state
export const editMode = derived(navigator, ($nav) => $nav.editMode);

// All decks
export const decks = derived(navigator, ($nav) => $nav.decks);

// Card order
export const cardOrder = derived(navigator, ($nav) => $nav.cardOrder);

// Initialization state
export const navigatorInitialized = derived(navigator, ($nav) => $nav.initialized);

// Recent sessions view mode
export const recentSessionsView = derived(navigator, ($nav) => $nav.recentSessionsView);

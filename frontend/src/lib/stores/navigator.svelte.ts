/**
 * Navigator Store - Card Navigator State Management
 *
 * Manages the AI Shuffle card navigator state including:
 * - Card ordering preferences
 * - Custom decks (user-created groupings)
 * - Edit mode state
 *
 * Persists to localStorage and syncs to server for cross-device access.
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import { untrack } from 'svelte';
import { browser } from '$app/environment';
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
// Module-level mutable state (timers, flags)
// ============================================================================

let saveTimer: ReturnType<typeof setTimeout> | null = null;
let serverSyncTimer: ReturnType<typeof setTimeout> | null = null;
let isLoadingFromServer = false;
let hasSyncedFromServer = false;

// ============================================================================
// Persistence Helpers
// ============================================================================

function generateId(): string {
	return `deck-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

function loadFromStorage(): NavigatorPersistedState | null {
	if (!browser) return null;

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
	if (!browser) return;

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
	if (!browser || isLoadingFromServer) return;

	// Don't write to server until we've completed initial sync
	if (!hasSyncedFromServer) {
		return;
	}

	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
	}

	serverSyncTimer = setTimeout(async () => {
		try {
			await setPreference(SERVER_PREFERENCE_KEY, state);
		} catch (e) {
			console.error('[Navigator] Failed to sync state to server:', e);
		}
		serverSyncTimer = null;
	}, SERVER_SYNC_DEBOUNCE_MS);
}

async function loadFromServer(): Promise<NavigatorPersistedState | null> {
	if (!browser) return null;

	try {
		isLoadingFromServer = true;
		const response = await getPreference<NavigatorPersistedState>(SERVER_PREFERENCE_KEY);

		if (response && response.value) {
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
// State helpers
// ============================================================================

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

// ============================================================================
// Reactive State
// ============================================================================

const persisted = loadFromStorage();

let _state = $state<NavigatorState>({
	cardOrder: persisted?.cardOrder || [],
	decks: persisted?.decks || [],
	recentSessionsView: persisted?.recentSessionsView || 'list',
	lastModified: persisted?.lastModified || Date.now(),
	editMode: false,
	initialized: false,
});

// ============================================================================
// Subscribe shim for backward compat with $storeName syntax
// ============================================================================

type Subscriber<T> = (value: T) => void;

const _subscribers = new Set<Subscriber<NavigatorState>>();

function _notifySubscribers() {
	untrack(() => { for (const fn of _subscribers) fn(_state); });
}

function _update(fn: (s: NavigatorState) => NavigatorState) {
	_state = fn(_state);
	_notifySubscribers();
}

/**
 * Update and persist state (debounced localStorage + server)
 */
function _updateAndPersist(updater: (state: NavigatorState) => NavigatorState): void {
	_state = updater(_state);
	const persistedData = getPersistedState(_state);
	_state = { ..._state, lastModified: persistedData.lastModified };
	saveToStorage(persistedData);
	saveToServer(persistedData);
	_notifySubscribers();
}

// ============================================================================
// Exported Store
// ============================================================================

export const navigator = {
	get current() {
		return _state;
	},

	subscribe(fn: Subscriber<NavigatorState>) {
		_subscribers.add(fn);
		untrack(() => fn(_state));
		return () => {
			_subscribers.delete(fn);
		};
	},

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

		const localDecks = _state.decks || [];
		const serverDecks = serverState?.decks || [];
		const localIsEmpty = localDecks.length === 0 && (_state.cardOrder || []).length === 0;
		const serverIsEmpty = serverDecks.length === 0 && (serverState?.cardOrder || []).length === 0;

		let mergedState: NavigatorState;

		if (serverState && !serverIsEmpty) {
			if (localIsEmpty) {
				// Local is empty, use server state entirely
				mergedState = {
					..._state,
					...serverState,
					editMode: false,
					initialized: true,
				};
			} else {
				// Both have data - merge them
				// Server decks take precedence, but preserve local-only decks
				const serverDeckIds = new Set(serverDecks.map((d) => d.id));
				const localOnlyDecks = localDecks.filter((d) => !serverDeckIds.has(d.id));

				// Merge card orders: server order first, then any local-only cards
				const serverCardOrder = serverState.cardOrder || [];
				const localCardOrder = _state.cardOrder || [];
				const serverCardOrderSet = new Set(serverCardOrder);
				const localOnlyCards = localCardOrder.filter((id) => !serverCardOrderSet.has(id));

				mergedState = {
					..._state,
					...serverState,
					decks: [...serverDecks, ...localOnlyDecks],
					cardOrder: [...serverCardOrder, ...localOnlyCards],
					editMode: false,
					initialized: true,
				};
			}
		} else {
			// No server state or server is empty - keep local
			mergedState = { ..._state, initialized: true };
		}

		// Save merged state to localStorage
		const persistedData = {
			cardOrder: mergedState.cardOrder,
			decks: mergedState.decks,
			recentSessionsView: mergedState.recentSessionsView,
			lastModified: Date.now(),
		};
		saveToStorage(persistedData);

		_state = mergedState;
		_notifySubscribers();

		// Mark sync as complete - now server writes are allowed
		hasSyncedFromServer = true;
	},

	/**
	 * Force sync current state to server
	 */
	async forceSync(): Promise<void> {
		const persistedData = getPersistedState(_state);
		try {
			await setPreference(SERVER_PREFERENCE_KEY, persistedData);
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
		_update((s) => ({ ...s, editMode: true }));
	},

	/**
	 * Exit edit mode and save changes
	 */
	exitEditMode(): void {
		_state = { ..._state, editMode: false };
		const persistedData = getPersistedState(_state);
		saveToStorage(persistedData);
		saveToServer(persistedData);
		_notifySubscribers();
	},

	/**
	 * Toggle edit mode
	 */
	toggleEditMode(): void {
		if (_state.editMode) {
			navigator.exitEditMode();
		} else {
			navigator.enterEditMode();
		}
	},

	// ========================================================================
	// Recent Sessions View
	// ========================================================================

	/**
	 * Set the view mode for recent sessions (list or timeline)
	 */
	setRecentSessionsView(view: RecentSessionsViewMode): void {
		_updateAndPersist((s) => ({
			...s,
			recentSessionsView: view,
		}));
	},

	/**
	 * Toggle between list and timeline views
	 */
	toggleRecentSessionsView(): void {
		const newView: RecentSessionsViewMode =
			_state.recentSessionsView === 'list' ? 'timeline' : 'list';
		_updateAndPersist((s) => ({
			...s,
			recentSessionsView: newView,
		}));
	},

	// ========================================================================
	// Card Ordering
	// ========================================================================

	/**
	 * Set the order of cards
	 */
	setCardOrder(order: string[]): void {
		_updateAndPersist((s) => ({
			...s,
			cardOrder: order,
		}));
	},

	/**
	 * Reorder a card by moving it to a new index
	 */
	reorderCard(cardId: string, newIndex: number): void {
		_update((s) => {
			const currentOrder = [...s.cardOrder];
			const currentIndex = currentOrder.indexOf(cardId);

			if (currentIndex === -1) {
				// Insert at new index
				currentOrder.splice(newIndex, 0, cardId);
			} else {
				// Remove from current position
				currentOrder.splice(currentIndex, 1);
				// Insert at new position
				currentOrder.splice(newIndex, 0, cardId);
			}

			return { ...s, cardOrder: currentOrder };
		});
	},

	/**
	 * Move a card to a new position (used during drag)
	 */
	moveCard(fromIndex: number, toIndex: number, allCardIds: string[]): void {
		_update((s) => {
			const newOrder = [...allCardIds];
			const [moved] = newOrder.splice(fromIndex, 1);
			newOrder.splice(toIndex, 0, moved);
			return { ...s, cardOrder: newOrder };
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

		_updateAndPersist((s) => ({
			...s,
			decks: [
				...s.decks,
				{
					id: deckId,
					name,
					icon: icon || DECK_ICONS[0],
					color: color || DECK_COLORS[s.decks.length % DECK_COLORS.length],
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
		_updateAndPersist((s) => ({
			...s,
			decks: s.decks.filter((d) => d.id !== deckId),
		}));
	},

	/**
	 * Rename a deck
	 */
	renameDeck(deckId: string, name: string): void {
		_updateAndPersist((s) => ({
			...s,
			decks: s.decks.map((d) =>
				d.id === deckId ? { ...d, name, updatedAt: new Date().toISOString() } : d
			),
		}));
	},

	/**
	 * Update deck icon
	 */
	setDeckIcon(deckId: string, icon: string): void {
		_updateAndPersist((s) => ({
			...s,
			decks: s.decks.map((d) =>
				d.id === deckId ? { ...d, icon, updatedAt: new Date().toISOString() } : d
			),
		}));
	},

	/**
	 * Update deck color
	 */
	setDeckColor(deckId: string, color: string): void {
		_updateAndPersist((s) => ({
			...s,
			decks: s.decks.map((d) =>
				d.id === deckId ? { ...d, color, updatedAt: new Date().toISOString() } : d
			),
		}));
	},

	/**
	 * Add a card to a deck (removes from main view)
	 */
	addCardToDeck(deckId: string, cardId: string): void {
		_updateAndPersist((s) => {
			// First, remove from any other deck
			const decksWithCardRemoved = s.decks.map((d) => ({
				...d,
				cardIds: d.cardIds.filter((id) => id !== cardId),
			}));

			// Then add to target deck
			return {
				...s,
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
		_updateAndPersist((s) => ({
			...s,
			decks: s.decks.map((d) =>
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
		_updateAndPersist((s) => ({
			...s,
			decks: s.decks.map((d) =>
				d.id === deckId ? { ...d, cardIds, updatedAt: new Date().toISOString() } : d
			),
		}));
	},

	/**
	 * Get the deck a card belongs to (if any)
	 */
	getCardDeck(cardId: string): CustomDeck | null {
		return _state.decks.find((d) => d.cardIds.includes(cardId)) || null;
	},

	/**
	 * Check if a card is in any deck
	 */
	isCardInDeck(cardId: string): boolean {
		return _state.decks.some((d) => d.cardIds.includes(cardId));
	},

	/**
	 * Get all cards that are NOT in any deck
	 */
	getMainViewCardIds(): string[] {
		const allDeckCardIds = new Set(_state.decks.flatMap((d) => d.cardIds));
		return _state.cardOrder.filter((id) => !allDeckCardIds.has(id));
	},

	// ========================================================================
	// Utility
	// ========================================================================

	/**
	 * Get a deck by ID
	 */
	getDeck(deckId: string): CustomDeck | undefined {
		return _state.decks.find((d) => d.id === deckId);
	},

	/**
	 * Reset store to initial state
	 */
	reset(): void {
		const freshState: NavigatorState = {
			cardOrder: [],
			decks: [],
			recentSessionsView: 'list',
			lastModified: Date.now(),
			editMode: false,
			initialized: true,
		};
		_state = freshState;
		saveToStorage(getPersistedState(freshState));
		_notifySubscribers();
	},
};

// ============================================================================
// Derived Stores
// ============================================================================

function createDerived<T>(getter: () => T) {
	return {
		get current() {
			return getter();
		},
		subscribe(fn: Subscriber<T>) {
			const handler: Subscriber<NavigatorState> = () => fn(getter());
			_subscribers.add(handler);
			untrack(() => fn(getter()));
			return () => {
				_subscribers.delete(handler);
			};
		},
	};
}

// Edit mode state
export const editMode = createDerived(() => _state.editMode);

// All decks
export const decks = createDerived(() => _state.decks);

// Card order
export const cardOrder = createDerived(() => _state.cardOrder);

// Initialization state
export const navigatorInitialized = createDerived(() => _state.initialized);

// Recent sessions view mode
export const recentSessionsView = createDerived(() => _state.recentSessionsView);

/**
 * Canvas Store - Media Generation State Management
 *
 * Manages the canvas feature for AI-powered image and video generation.
 * Supports multiple providers (Gemini, Imagen, GPT Image, Veo, Sora)
 * and tracks generation state, gallery items, and form inputs.
 */

import { writable, derived, get } from 'svelte/store';
import { api } from '$lib/api/client';

// ============================================================================
// Types
// ============================================================================

export interface CanvasItem {
	id: string;
	type: 'image' | 'video';
	filename: string;
	url: string; // API URL to serve the media
	prompt: string; // Generation prompt used
	provider: string; // Provider used (google-gemini, google-imagen, openai-gpt-image, google-veo, openai-sora)
	model: string | null; // Specific model used
	settings: {
		// Generation settings
		aspectRatio?: string;
		resolution?: string;
		duration?: number; // For videos
	};
	fileSize?: number;
	createdAt: string;
	// For videos only
	sourceVideoUri?: string; // For extending Veo videos
}

export type CanvasView = 'gallery' | 'create' | 'edit';
export type CanvasCreateType = 'image' | 'video' | null;

export interface CanvasState {
	items: CanvasItem[];
	selectedItemId: string | null;
	isGenerating: boolean;
	generationProgress: string | null; // Status message during generation
	error: string | null;

	// View state
	view: CanvasView;
	createType: CanvasCreateType;

	// Current generation form state
	prompt: string;
	provider: string; // Current provider selection
	model: string | null; // Current model selection
	aspectRatio: string;
	resolution: string;
	duration: number; // For videos (4, 6, 8, 12)

	// Edit state
	editingItem: CanvasItem | null;
	editPrompt: string;

	// Reference images for generateWithReference
	referenceImages: string[]; // File paths

	// Image-to-video state
	sourceImage: string | null; // For animating images
}

// ============================================================================
// Constants
// ============================================================================

const PREFERENCE_KEY = 'canvas_view_prefs';
const SAVE_DEBOUNCE_MS = 500;

const DEFAULT_STATE: CanvasState = {
	items: [],
	selectedItemId: null,
	isGenerating: false,
	generationProgress: null,
	error: null,

	view: 'gallery',
	createType: null,

	prompt: '',
	provider: 'google-gemini',
	model: null,
	aspectRatio: '16:9',
	resolution: '1K',
	duration: 8,

	editingItem: null,
	editPrompt: '',

	referenceImages: [],
	sourceImage: null
};

// ============================================================================
// Persistence Helpers
// ============================================================================

interface PersistedViewPrefs {
	view: CanvasView;
	createType: CanvasCreateType;
	provider: string;
	aspectRatio: string;
	resolution: string;
	duration: number;
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;

function loadViewPrefsFromStorage(): Partial<PersistedViewPrefs> {
	if (typeof window === 'undefined') return {};

	try {
		const stored = localStorage.getItem(PREFERENCE_KEY);
		if (stored) {
			return JSON.parse(stored);
		}
	} catch (e) {
		console.error('[Canvas] Failed to load view prefs from localStorage:', e);
	}
	return {};
}

function saveViewPrefsToStorage(state: CanvasState) {
	if (typeof window === 'undefined') return;

	if (saveTimer) {
		clearTimeout(saveTimer);
	}

	saveTimer = setTimeout(() => {
		try {
			const prefs: PersistedViewPrefs = {
				view: state.view,
				createType: state.createType,
				provider: state.provider,
				aspectRatio: state.aspectRatio,
				resolution: state.resolution,
				duration: state.duration
			};
			localStorage.setItem(PREFERENCE_KEY, JSON.stringify(prefs));
		} catch (e) {
			console.error('[Canvas] Failed to save view prefs to localStorage:', e);
		}
		saveTimer = null;
	}, SAVE_DEBOUNCE_MS);
}

// ============================================================================
// Store Creation
// ============================================================================

function createCanvasStore() {
	// Load persisted view preferences
	const persistedPrefs = loadViewPrefsFromStorage();

	const initialState: CanvasState = {
		...DEFAULT_STATE,
		...persistedPrefs
	};

	const { subscribe, set, update } = writable<CanvasState>(initialState);

	/**
	 * Helper to update state and persist view preferences
	 */
	function updateAndPersist(
		updater: (state: CanvasState) => CanvasState
	): void {
		update((state) => {
			const newState = updater(state);
			saveViewPrefsToStorage(newState);
			return newState;
		});
	}

	return {
		subscribe,

		// ========================================================================
		// View Management
		// ========================================================================

		/**
		 * Switch between gallery/create/edit views
		 */
		setView(view: CanvasView): void {
			updateAndPersist((state) => ({
				...state,
				view,
				// Clear edit state when leaving edit view
				editingItem: view === 'edit' ? state.editingItem : null,
				editPrompt: view === 'edit' ? state.editPrompt : ''
			}));
		},

		/**
		 * Set image or video creation mode
		 */
		setCreateType(type: CanvasCreateType): void {
			updateAndPersist((state) => ({
				...state,
				createType: type,
				view: type ? 'create' : state.view
			}));
		},

		// ========================================================================
		// Form State Setters
		// ========================================================================

		/**
		 * Set the generation prompt
		 */
		setPrompt(prompt: string): void {
			update((state) => ({ ...state, prompt }));
		},

		/**
		 * Set the provider for generation
		 */
		setProvider(provider: string): void {
			updateAndPersist((state) => ({
				...state,
				provider,
				// Reset model when provider changes
				model: null
			}));
		},

		/**
		 * Set the specific model to use
		 */
		setModel(model: string | null): void {
			update((state) => ({ ...state, model }));
		},

		/**
		 * Set the aspect ratio for generation
		 */
		setAspectRatio(aspectRatio: string): void {
			updateAndPersist((state) => ({ ...state, aspectRatio }));
		},

		/**
		 * Set the resolution for generation
		 */
		setResolution(resolution: string): void {
			updateAndPersist((state) => ({ ...state, resolution }));
		},

		/**
		 * Set the duration for video generation
		 */
		setDuration(duration: number): void {
			updateAndPersist((state) => ({ ...state, duration }));
		},

		// ========================================================================
		// Gallery Selection
		// ========================================================================

		/**
		 * Select a gallery item
		 */
		selectItem(id: string | null): void {
			update((state) => ({ ...state, selectedItemId: id }));
		},

		/**
		 * Delete an item from the gallery
		 */
		async deleteItem(id: string): Promise<void> {
			try {
				await api.delete(`/canvas/${id}`);
				update((state) => ({
					...state,
					items: state.items.filter((item) => item.id !== id),
					selectedItemId: state.selectedItemId === id ? null : state.selectedItemId
				}));
			} catch (error) {
				console.error('[Canvas] Failed to delete item:', error);
				update((state) => ({
					...state,
					error: error instanceof Error ? error.message : 'Failed to delete item'
				}));
			}
		},

		// ========================================================================
		// Data Loading
		// ========================================================================

		/**
		 * Fetch items from the backend API
		 */
		async loadItems(): Promise<void> {
			try {
				const response = await api.get<{ items: CanvasItem[]; total: number }>('/canvas');
				update((state) => ({
					...state,
					items: response?.items || [],
					error: null
				}));
			} catch (error) {
				console.error('[Canvas] Failed to load items:', error);
				update((state) => ({
					...state,
					items: [], // Reset to empty array on error
					error: error instanceof Error ? error.message : 'Failed to load canvas items'
				}));
			}
		},

		// ========================================================================
		// Error Handling
		// ========================================================================

		/**
		 * Clear the error state
		 */
		clearError(): void {
			update((state) => ({ ...state, error: null }));
		},

		// ========================================================================
		// Generation Lifecycle
		// ========================================================================

		/**
		 * Start the generation process
		 */
		startGeneration(progressMessage?: string): void {
			update((state) => ({
				...state,
				isGenerating: true,
				generationProgress: progressMessage || 'Starting generation...',
				error: null
			}));
		},

		/**
		 * Update generation progress message
		 */
		updateProgress(message: string): void {
			update((state) => ({
				...state,
				generationProgress: message
			}));
		},

		/**
		 * Complete generation successfully with the new item
		 */
		completeGeneration(item: CanvasItem): void {
			update((state) => ({
				...state,
				isGenerating: false,
				generationProgress: null,
				error: null,
				items: [item, ...state.items],
				selectedItemId: item.id,
				view: 'gallery',
				// Clear form state after successful generation
				prompt: '',
				referenceImages: [],
				sourceImage: null
			}));
		},

		/**
		 * Handle generation failure
		 */
		failGeneration(error: string): void {
			update((state) => ({
				...state,
				isGenerating: false,
				generationProgress: null,
				error
			}));
		},

		// ========================================================================
		// Edit Mode
		// ========================================================================

		/**
		 * Enter edit mode for an item
		 */
		setEditingItem(item: CanvasItem | null): void {
			update((state) => ({
				...state,
				editingItem: item,
				editPrompt: item?.prompt || '',
				view: item ? 'edit' : 'gallery'
			}));
		},

		/**
		 * Update the edit prompt
		 */
		setEditPrompt(editPrompt: string): void {
			update((state) => ({ ...state, editPrompt }));
		},

		// ========================================================================
		// Reference Images
		// ========================================================================

		/**
		 * Add a reference image path
		 */
		addReferenceImage(path: string): void {
			update((state) => ({
				...state,
				referenceImages: state.referenceImages.includes(path)
					? state.referenceImages
					: [...state.referenceImages, path]
			}));
		},

		/**
		 * Remove a reference image path
		 */
		removeReferenceImage(path: string): void {
			update((state) => ({
				...state,
				referenceImages: state.referenceImages.filter((p) => p !== path)
			}));
		},

		/**
		 * Clear all reference images
		 */
		clearReferenceImages(): void {
			update((state) => ({ ...state, referenceImages: [] }));
		},

		// ========================================================================
		// Image-to-Video
		// ========================================================================

		/**
		 * Set source image for image-to-video generation
		 */
		setSourceImage(path: string | null): void {
			update((state) => ({ ...state, sourceImage: path }));
		},

		// ========================================================================
		// Reset
		// ========================================================================

		/**
		 * Reset form to defaults
		 */
		reset(): void {
			update((state) => ({
				...state,
				prompt: '',
				model: null,
				editingItem: null,
				editPrompt: '',
				referenceImages: [],
				sourceImage: null,
				error: null,
				isGenerating: false,
				generationProgress: null
			}));
		},

		/**
		 * Reset entire store to initial state
		 */
		resetAll(): void {
			const persistedPrefs = loadViewPrefsFromStorage();
			set({
				...DEFAULT_STATE,
				...persistedPrefs
			});
		},

		// ========================================================================
		// Utility
		// ========================================================================

		/**
		 * Get the current state snapshot
		 */
		getState(): CanvasState {
			return get({ subscribe });
		},

		/**
		 * Get an item by ID
		 */
		getItem(id: string): CanvasItem | undefined {
			const state = get({ subscribe });
			return state.items.find((item) => item.id === id);
		},

		/**
		 * Add an item to the gallery (used when receiving from backend)
		 */
		addItem(item: CanvasItem): void {
			update((state) => ({
				...state,
				items: [item, ...state.items.filter((i) => i.id !== item.id)]
			}));
		},

		/**
		 * Update an existing item
		 */
		updateItem(id: string, updates: Partial<CanvasItem>): void {
			update((state) => ({
				...state,
				items: state.items.map((item) =>
					item.id === id ? { ...item, ...updates } : item
				)
			}));
		}
	};
}

// ============================================================================
// Export Store & Derived Stores
// ============================================================================

export const canvas = createCanvasStore();

// All canvas items
export const canvasItems = derived(canvas, ($canvas) => $canvas.items);

// Currently selected item
export const selectedItem = derived(canvas, ($canvas) => {
	if (!$canvas.selectedItemId) return null;
	return $canvas.items.find((item) => item.id === $canvas.selectedItemId) || null;
});

// Generation in progress
export const isLoading = derived(canvas, ($canvas) => $canvas.isGenerating);

// Current view state
export const canvasView = derived(canvas, ($canvas) => $canvas.view);

// Current create type
export const canvasCreateType = derived(canvas, ($canvas) => $canvas.createType);

// Generation error
export const canvasError = derived(canvas, ($canvas) => $canvas.error);

// Generation progress message
export const generationProgress = derived(canvas, ($canvas) => $canvas.generationProgress);

// Item counts by type
export const imageCount = derived(canvasItems, ($items) =>
	$items.filter((item) => item.type === 'image').length
);

export const videoCount = derived(canvasItems, ($items) =>
	$items.filter((item) => item.type === 'video').length
);

// Reference images array
export const referenceImages = derived(canvas, ($canvas) => $canvas.referenceImages);

// Edit mode state
export const editingItem = derived(canvas, ($canvas) => $canvas.editingItem);

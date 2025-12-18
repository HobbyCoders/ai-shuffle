/**
 * Studio Store - Media Generation Management
 *
 * Manages the Studio feature for AI-powered media generation:
 * - Image generation (Gemini, Imagen, GPT Image)
 * - Video generation (Veo, Sora)
 * - Asset management and history
 * - Provider selection and preferences
 */

import { writable, derived, get } from 'svelte/store';
import { api } from '$lib/api/client';

// ============================================================================
// Types
// ============================================================================

export type ImageProvider = 'google-gemini' | 'google-imagen' | 'openai-gpt-image';
export type VideoProvider = 'google-veo' | 'openai-sora';
export type GenerationType = 'image' | 'video';
export type GenerationStatus = 'pending' | 'generating' | 'completed' | 'failed';

export interface DeckGeneration {
	id: string;
	type: GenerationType;
	prompt: string;
	provider: ImageProvider | VideoProvider;
	status: GenerationStatus;
	progress: number; // 0-100
	result?: {
		url: string;
		width?: number;
		height?: number;
		duration?: number; // For videos, in seconds
	};
	error?: string;
	settings: {
		aspectRatio?: string;
		resolution?: string;
		duration?: number;
		style?: string;
	};
	startedAt: Date;
	completedAt?: Date;
}

export interface Asset {
	id: string;
	type: GenerationType;
	url: string;
	prompt: string;
	provider: string;
	createdAt: Date;
	tags: string[];
	// Media metadata
	width?: number;
	height?: number;
	duration?: number;
	fileSize?: number;
}

export interface StudioState {
	activeGeneration: DeckGeneration | null;
	imageProvider: ImageProvider;
	videoProvider: VideoProvider;
	defaultAspectRatio: string;
	defaultResolution: string;
	defaultVideoDuration: number;
	recentGenerations: DeckGeneration[];
	savedAssets: Asset[];
	// UI state
	isLoading: boolean;
	error: string | null;
}

// ============================================================================
// Constants
// ============================================================================

const STORAGE_KEY = 'studio_state';
const SAVE_DEBOUNCE_MS = 500;
const MAX_RECENT_GENERATIONS = 50;

// ============================================================================
// Persistence Helpers
// ============================================================================

interface PersistedStudioState {
	imageProvider: ImageProvider;
	videoProvider: VideoProvider;
	defaultAspectRatio: string;
	defaultResolution: string;
	defaultVideoDuration: number;
	recentGenerations: Array<Omit<DeckGeneration, 'startedAt' | 'completedAt'> & {
		startedAt: string;
		completedAt?: string;
	}>;
	savedAssets: Array<Omit<Asset, 'createdAt'> & { createdAt: string }>;
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;

function loadFromStorage(): Partial<StudioState> | null {
	if (typeof window === 'undefined') return null;

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (stored) {
			const parsed: PersistedStudioState = JSON.parse(stored);
			return {
				imageProvider: parsed.imageProvider,
				videoProvider: parsed.videoProvider,
				defaultAspectRatio: parsed.defaultAspectRatio,
				defaultResolution: parsed.defaultResolution,
				defaultVideoDuration: parsed.defaultVideoDuration,
				recentGenerations: parsed.recentGenerations.map((g) => ({
					...g,
					startedAt: new Date(g.startedAt),
					completedAt: g.completedAt ? new Date(g.completedAt) : undefined
				})),
				savedAssets: parsed.savedAssets.map((a) => ({
					...a,
					createdAt: new Date(a.createdAt)
				}))
			};
		}
	} catch (e) {
		console.error('[Studio] Failed to load from localStorage:', e);
	}
	return null;
}

function saveToStorage(state: StudioState) {
	if (typeof window === 'undefined') return;

	if (saveTimer) {
		clearTimeout(saveTimer);
	}

	saveTimer = setTimeout(() => {
		try {
			const persisted: PersistedStudioState = {
				imageProvider: state.imageProvider,
				videoProvider: state.videoProvider,
				defaultAspectRatio: state.defaultAspectRatio,
				defaultResolution: state.defaultResolution,
				defaultVideoDuration: state.defaultVideoDuration,
				recentGenerations: state.recentGenerations.map((g) => ({
					...g,
					startedAt: g.startedAt.toISOString(),
					completedAt: g.completedAt?.toISOString()
				})),
				savedAssets: state.savedAssets.map((a) => ({
					...a,
					createdAt: a.createdAt.toISOString()
				}))
			};
			localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
		} catch (e) {
			console.error('[Studio] Failed to save to localStorage:', e);
		}
		saveTimer = null;
	}, SAVE_DEBOUNCE_MS);
}

// ============================================================================
// Helpers
// ============================================================================

function generateId(): string {
	return `gen-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

// ============================================================================
// Store Creation
// ============================================================================

function createStudioStore() {
	const persisted = loadFromStorage();

	const initialState: StudioState = {
		activeGeneration: null,
		imageProvider: persisted?.imageProvider || 'google-gemini',
		videoProvider: persisted?.videoProvider || 'google-veo',
		defaultAspectRatio: persisted?.defaultAspectRatio || '16:9',
		defaultResolution: persisted?.defaultResolution || '1K',
		defaultVideoDuration: persisted?.defaultVideoDuration || 8,
		recentGenerations: persisted?.recentGenerations || [],
		savedAssets: persisted?.savedAssets || [],
		isLoading: false,
		error: null
	};

	const { subscribe, set, update } = writable<StudioState>(initialState);

	/**
	 * Helper to update state and persist
	 */
	function updateAndPersist(updater: (state: StudioState) => StudioState): void {
		update((state) => {
			const newState = updater(state);
			saveToStorage(newState);
			return newState;
		});
	}

	return {
		subscribe,

		// ========================================================================
		// Provider Settings
		// ========================================================================

		/**
		 * Set provider for a generation type
		 */
		setProvider(type: 'image' | 'video', provider: string): void {
			updateAndPersist((state) => {
				if (type === 'image') {
					return { ...state, imageProvider: provider as ImageProvider };
				} else {
					return { ...state, videoProvider: provider as VideoProvider };
				}
			});
		},

		/**
		 * Set default aspect ratio
		 */
		setDefaultAspectRatio(aspectRatio: string): void {
			updateAndPersist((state) => ({
				...state,
				defaultAspectRatio: aspectRatio
			}));
		},

		/**
		 * Set default resolution
		 */
		setDefaultResolution(resolution: string): void {
			updateAndPersist((state) => ({
				...state,
				defaultResolution: resolution
			}));
		},

		/**
		 * Set default video duration
		 */
		setDefaultVideoDuration(duration: number): void {
			updateAndPersist((state) => ({
				...state,
				defaultVideoDuration: duration
			}));
		},

		// ========================================================================
		// Image Generation
		// ========================================================================

		/**
		 * Generate an image
		 */
		async generateImage(
			prompt: string,
			options?: {
				provider?: ImageProvider;
				aspectRatio?: string;
				resolution?: string;
				style?: string;
			}
		): Promise<DeckGeneration | null> {
			const state = get({ subscribe });
			const provider = options?.provider || state.imageProvider;

			const generation: DeckGeneration = {
				id: generateId(),
				type: 'image',
				prompt,
				provider,
				status: 'generating',
				progress: 0,
				settings: {
					aspectRatio: options?.aspectRatio || state.defaultAspectRatio,
					resolution: options?.resolution || state.defaultResolution,
					style: options?.style
				},
				startedAt: new Date()
			};

			update((s) => ({
				...s,
				activeGeneration: generation,
				isLoading: true,
				error: null
			}));

			try {
				// Call the API
				const response = await api.post<{
					id: string;
					url: string;
					width?: number;
					height?: number;
				}>('/canvas/generate/image', {
					prompt,
					provider,
					aspect_ratio: generation.settings.aspectRatio,
					resolution: generation.settings.resolution
				});

				const completed: DeckGeneration = {
					...generation,
					status: 'completed',
					progress: 100,
					result: {
						url: response.url,
						width: response.width,
						height: response.height
					},
					completedAt: new Date()
				};

				updateAndPersist((s) => ({
					...s,
					activeGeneration: null,
					isLoading: false,
					recentGenerations: [completed, ...s.recentGenerations].slice(0, MAX_RECENT_GENERATIONS)
				}));

				return completed;
			} catch (e) {
				const errorMessage = e instanceof Error ? e.message : 'Failed to generate image';

				const failed: DeckGeneration = {
					...generation,
					status: 'failed',
					error: errorMessage,
					completedAt: new Date()
				};

				updateAndPersist((s) => ({
					...s,
					activeGeneration: null,
					isLoading: false,
					error: errorMessage,
					recentGenerations: [failed, ...s.recentGenerations].slice(0, MAX_RECENT_GENERATIONS)
				}));

				return null;
			}
		},

		// ========================================================================
		// Video Generation
		// ========================================================================

		/**
		 * Generate a video
		 */
		async generateVideo(
			prompt: string,
			options?: {
				provider?: VideoProvider;
				aspectRatio?: string;
				duration?: number;
				sourceImage?: string;
			}
		): Promise<DeckGeneration | null> {
			const state = get({ subscribe });
			const provider = options?.provider || state.videoProvider;

			const generation: DeckGeneration = {
				id: generateId(),
				type: 'video',
				prompt,
				provider,
				status: 'generating',
				progress: 0,
				settings: {
					aspectRatio: options?.aspectRatio || state.defaultAspectRatio,
					duration: options?.duration || state.defaultVideoDuration
				},
				startedAt: new Date()
			};

			update((s) => ({
				...s,
				activeGeneration: generation,
				isLoading: true,
				error: null
			}));

			try {
				// Call the API
				const response = await api.post<{
					id: string;
					url: string;
					duration?: number;
				}>('/canvas/generate/video', {
					prompt,
					provider,
					aspect_ratio: generation.settings.aspectRatio,
					duration: generation.settings.duration,
					source_image: options?.sourceImage
				});

				const completed: DeckGeneration = {
					...generation,
					status: 'completed',
					progress: 100,
					result: {
						url: response.url,
						duration: response.duration
					},
					completedAt: new Date()
				};

				updateAndPersist((s) => ({
					...s,
					activeGeneration: null,
					isLoading: false,
					recentGenerations: [completed, ...s.recentGenerations].slice(0, MAX_RECENT_GENERATIONS)
				}));

				return completed;
			} catch (e) {
				const errorMessage = e instanceof Error ? e.message : 'Failed to generate video';

				const failed: DeckGeneration = {
					...generation,
					status: 'failed',
					error: errorMessage,
					completedAt: new Date()
				};

				updateAndPersist((s) => ({
					...s,
					activeGeneration: null,
					isLoading: false,
					error: errorMessage,
					recentGenerations: [failed, ...s.recentGenerations].slice(0, MAX_RECENT_GENERATIONS)
				}));

				return null;
			}
		},

		// ========================================================================
		// Edit Operations
		// ========================================================================

		/**
		 * Edit an existing image with a prompt
		 */
		async editImage(
			assetId: string,
			prompt: string
		): Promise<DeckGeneration | null> {
			const state = get({ subscribe });
			const asset = state.savedAssets.find((a) => a.id === assetId);

			if (!asset || asset.type !== 'image') {
				update((s) => ({ ...s, error: 'Asset not found or not an image' }));
				return null;
			}

			const generation: DeckGeneration = {
				id: generateId(),
				type: 'image',
				prompt: `Edit: ${prompt}`,
				provider: state.imageProvider,
				status: 'generating',
				progress: 0,
				settings: {},
				startedAt: new Date()
			};

			update((s) => ({
				...s,
				activeGeneration: generation,
				isLoading: true,
				error: null
			}));

			try {
				const response = await api.post<{
					id: string;
					url: string;
					width?: number;
					height?: number;
				}>('/canvas/edit/image', {
					source_id: assetId,
					prompt,
					provider: state.imageProvider
				});

				const completed: DeckGeneration = {
					...generation,
					status: 'completed',
					progress: 100,
					result: {
						url: response.url,
						width: response.width,
						height: response.height
					},
					completedAt: new Date()
				};

				updateAndPersist((s) => ({
					...s,
					activeGeneration: null,
					isLoading: false,
					recentGenerations: [completed, ...s.recentGenerations].slice(0, MAX_RECENT_GENERATIONS)
				}));

				return completed;
			} catch (e) {
				const errorMessage = e instanceof Error ? e.message : 'Failed to edit image';

				update((s) => ({
					...s,
					activeGeneration: null,
					isLoading: false,
					error: errorMessage
				}));

				return null;
			}
		},

		/**
		 * Extend a video with additional content
		 */
		async extendVideo(
			assetId: string,
			prompt: string
		): Promise<DeckGeneration | null> {
			const state = get({ subscribe });
			const asset = state.savedAssets.find((a) => a.id === assetId);

			if (!asset || asset.type !== 'video') {
				update((s) => ({ ...s, error: 'Asset not found or not a video' }));
				return null;
			}

			const generation: DeckGeneration = {
				id: generateId(),
				type: 'video',
				prompt: `Extend: ${prompt}`,
				provider: state.videoProvider,
				status: 'generating',
				progress: 0,
				settings: {},
				startedAt: new Date()
			};

			update((s) => ({
				...s,
				activeGeneration: generation,
				isLoading: true,
				error: null
			}));

			try {
				const response = await api.post<{
					id: string;
					url: string;
					duration?: number;
				}>('/canvas/extend/video', {
					source_id: assetId,
					prompt,
					provider: state.videoProvider
				});

				const completed: DeckGeneration = {
					...generation,
					status: 'completed',
					progress: 100,
					result: {
						url: response.url,
						duration: response.duration
					},
					completedAt: new Date()
				};

				updateAndPersist((s) => ({
					...s,
					activeGeneration: null,
					isLoading: false,
					recentGenerations: [completed, ...s.recentGenerations].slice(0, MAX_RECENT_GENERATIONS)
				}));

				return completed;
			} catch (e) {
				const errorMessage = e instanceof Error ? e.message : 'Failed to extend video';

				update((s) => ({
					...s,
					activeGeneration: null,
					isLoading: false,
					error: errorMessage
				}));

				return null;
			}
		},

		// ========================================================================
		// Asset Management
		// ========================================================================

		/**
		 * Save a generation as an asset
		 */
		saveAsset(generation: DeckGeneration): string | null {
			if (generation.status !== 'completed' || !generation.result) {
				return null;
			}

			const assetId = `asset-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

			const asset: Asset = {
				id: assetId,
				type: generation.type,
				url: generation.result.url,
				prompt: generation.prompt,
				provider: generation.provider,
				createdAt: new Date(),
				tags: [],
				width: generation.result.width,
				height: generation.result.height,
				duration: generation.result.duration
			};

			updateAndPersist((state) => ({
				...state,
				savedAssets: [asset, ...state.savedAssets]
			}));

			return assetId;
		},

		/**
		 * Delete an asset
		 */
		deleteAsset(id: string): void {
			updateAndPersist((state) => ({
				...state,
				savedAssets: state.savedAssets.filter((a) => a.id !== id)
			}));
		},

		/**
		 * Add tags to an asset
		 */
		addAssetTags(id: string, tags: string[]): void {
			updateAndPersist((state) => ({
				...state,
				savedAssets: state.savedAssets.map((a) =>
					a.id === id
						? { ...a, tags: [...new Set([...a.tags, ...tags])] }
						: a
				)
			}));
		},

		/**
		 * Remove a tag from an asset
		 */
		removeAssetTag(id: string, tag: string): void {
			updateAndPersist((state) => ({
				...state,
				savedAssets: state.savedAssets.map((a) =>
					a.id === id
						? { ...a, tags: a.tags.filter((t) => t !== tag) }
						: a
				)
			}));
		},

		// ========================================================================
		// Generation History
		// ========================================================================

		/**
		 * Clear recent generations
		 */
		clearRecentGenerations(): void {
			updateAndPersist((state) => ({
				...state,
				recentGenerations: []
			}));
		},

		/**
		 * Remove a specific generation from history
		 */
		removeGeneration(id: string): void {
			updateAndPersist((state) => ({
				...state,
				recentGenerations: state.recentGenerations.filter((g) => g.id !== id)
			}));
		},

		// ========================================================================
		// Progress Updates
		// ========================================================================

		/**
		 * Update generation progress (for long-running operations)
		 */
		setGenerationProgress(id: string, progress: number): void {
			update((state) => {
				if (state.activeGeneration?.id === id) {
					return {
						...state,
						activeGeneration: {
							...state.activeGeneration,
							progress: Math.max(0, Math.min(100, progress))
						}
					};
				}
				return state;
			});
		},

		/**
		 * Cancel the active generation
		 */
		cancelGeneration(): void {
			update((state) => ({
				...state,
				activeGeneration: null,
				isLoading: false
			}));
		},

		// ========================================================================
		// Utility
		// ========================================================================

		/**
		 * Clear error
		 */
		clearError(): void {
			update((state) => ({ ...state, error: null }));
		},

		/**
		 * Get an asset by ID
		 */
		getAsset(id: string): Asset | undefined {
			const state = get({ subscribe });
			return state.savedAssets.find((a) => a.id === id);
		},

		/**
		 * Get a generation by ID
		 */
		getGeneration(id: string): DeckGeneration | undefined {
			const state = get({ subscribe });
			if (state.activeGeneration?.id === id) {
				return state.activeGeneration;
			}
			return state.recentGenerations.find((g) => g.id === id);
		},

		/**
		 * Reset store to initial state
		 */
		reset(): void {
			const freshState: StudioState = {
				activeGeneration: null,
				imageProvider: 'google-gemini',
				videoProvider: 'google-veo',
				defaultAspectRatio: '16:9',
				defaultResolution: '1K',
				defaultVideoDuration: 8,
				recentGenerations: [],
				savedAssets: [],
				isLoading: false,
				error: null
			};
			set(freshState);
			saveToStorage(freshState);
		}
	};
}

// ============================================================================
// Export Store & Derived Stores
// ============================================================================

export const studio = createStudioStore();

// Active generation
export const activeGeneration = derived(studio, ($studio) => $studio.activeGeneration);

// Loading state
export const studioLoading = derived(studio, ($studio) => $studio.isLoading);

// Error state
export const studioError = derived(studio, ($studio) => $studio.error);

// Provider settings
export const imageProvider = derived(studio, ($studio) => $studio.imageProvider);
export const videoProvider = derived(studio, ($studio) => $studio.videoProvider);

// Default settings
export const defaultAspectRatio = derived(studio, ($studio) => $studio.defaultAspectRatio);
export const defaultResolution = derived(studio, ($studio) => $studio.defaultResolution);
export const defaultVideoDuration = derived(studio, ($studio) => $studio.defaultVideoDuration);

// Recent generations
export const recentGenerations = derived(studio, ($studio) => $studio.recentGenerations);

// Saved assets
export const savedAssets = derived(studio, ($studio) => $studio.savedAssets);

// Image assets only
export const imageAssets = derived(studio, ($studio) =>
	$studio.savedAssets.filter((a) => a.type === 'image')
);

// Video assets only
export const videoAssets = derived(studio, ($studio) =>
	$studio.savedAssets.filter((a) => a.type === 'video')
);

// Completed generations
export const completedGenerations = derived(studio, ($studio) =>
	$studio.recentGenerations.filter((g) => g.status === 'completed')
);

// Failed generations
export const failedGenerations = derived(studio, ($studio) =>
	$studio.recentGenerations.filter((g) => g.status === 'failed')
);

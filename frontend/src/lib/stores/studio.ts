/**
 * Studio Store - Content creation state for "The Deck" UI
 *
 * Manages image and video generation, provider settings,
 * asset library, and generation history.
 */

import { writable, derived, get } from 'svelte/store';
import { deck } from './deck';
import type { DeckGeneration, GenerationStatus, GenerationType } from './deck';

// =============================================================================
// Types
// =============================================================================

export type ImageProvider = 'google-gemini' | 'google-imagen' | 'openai-gpt-image';

export type VideoProvider = 'google-veo' | 'openai-sora';

export type AspectRatio = '1:1' | '16:9' | '9:16' | '4:3' | '3:4';

export interface GenerationOptions {
	aspectRatio?: AspectRatio;
	style?: string;
	seed?: number;
	negativePrompt?: string;
	// Image-specific
	imageCount?: number;
	enhancePrompt?: boolean;
	// Video-specific
	duration?: number;
	fps?: number;
}

export interface Asset {
	id: string;
	type: GenerationType;
	url: string;
	thumbnailUrl?: string;
	prompt: string;
	provider: string;
	options?: GenerationOptions;
	createdAt: Date;
	tags: string[];
	// Metadata
	width?: number;
	height?: number;
	duration?: number; // For videos
	fileSize?: number;
}

export interface Generation extends DeckGeneration {
	options?: GenerationOptions;
}

interface StudioState {
	// Current generation
	activeGeneration: Generation | null;

	// Provider settings
	imageProvider: ImageProvider;
	videoProvider: VideoProvider;
	defaultAspectRatio: AspectRatio;

	// History
	recentGenerations: Generation[];

	// Asset library
	savedAssets: Asset[];

	// UI state
	loading: boolean;
	error: string | null;
}

// =============================================================================
// Persistence
// =============================================================================

const STORAGE_KEY = 'aihub_studio_state';
const ASSETS_STORAGE_KEY = 'aihub_studio_assets';

interface PersistedStudioState {
	imageProvider: ImageProvider;
	videoProvider: VideoProvider;
	defaultAspectRatio: AspectRatio;
	recentGenerations: Array<{
		id: string;
		type: GenerationType;
		prompt: string;
		status: GenerationStatus;
		progress: number;
		provider: string;
		resultUrl?: string;
		error?: string;
		options?: GenerationOptions;
		createdAt: string;
		completedAt?: string;
	}>;
}

interface PersistedAssets {
	assets: Array<{
		id: string;
		type: GenerationType;
		url: string;
		thumbnailUrl?: string;
		prompt: string;
		provider: string;
		options?: GenerationOptions;
		createdAt: string;
		tags: string[];
		width?: number;
		height?: number;
		duration?: number;
		fileSize?: number;
	}>;
}

/**
 * Load settings from localStorage
 */
function loadPersistedSettings(): Partial<StudioState> | null {
	if (typeof window === 'undefined') return null;

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (!stored) return null;

		const persisted: PersistedStudioState = JSON.parse(stored);

		return {
			imageProvider: persisted.imageProvider,
			videoProvider: persisted.videoProvider,
			defaultAspectRatio: persisted.defaultAspectRatio,
			recentGenerations: persisted.recentGenerations.map((gen) => ({
				...gen,
				createdAt: new Date(gen.createdAt),
				completedAt: gen.completedAt ? new Date(gen.completedAt) : undefined
			}))
		};
	} catch (e) {
		console.error('[Studio] Failed to load persisted settings:', e);
		return null;
	}
}

/**
 * Load assets from localStorage
 */
function loadPersistedAssets(): Asset[] {
	if (typeof window === 'undefined') return [];

	try {
		const stored = localStorage.getItem(ASSETS_STORAGE_KEY);
		if (!stored) return [];

		const persisted: PersistedAssets = JSON.parse(stored);

		return persisted.assets.map((asset) => ({
			...asset,
			createdAt: new Date(asset.createdAt)
		}));
	} catch (e) {
		console.error('[Studio] Failed to load persisted assets:', e);
		return [];
	}
}

/**
 * Save settings to localStorage
 */
function savePersistedSettings(state: StudioState) {
	if (typeof window === 'undefined') return;

	try {
		const persisted: PersistedStudioState = {
			imageProvider: state.imageProvider,
			videoProvider: state.videoProvider,
			defaultAspectRatio: state.defaultAspectRatio,
			recentGenerations: state.recentGenerations.slice(0, 50).map((gen) => ({
				...gen,
				createdAt: gen.createdAt.toISOString(),
				completedAt: gen.completedAt?.toISOString()
			}))
		};

		localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
	} catch (e) {
		console.error('[Studio] Failed to save settings:', e);
	}
}

/**
 * Save assets to localStorage
 */
function savePersistedAssets(assets: Asset[]) {
	if (typeof window === 'undefined') return;

	try {
		const persisted: PersistedAssets = {
			assets: assets.map((asset) => ({
				...asset,
				createdAt: asset.createdAt.toISOString()
			}))
		};

		localStorage.setItem(ASSETS_STORAGE_KEY, JSON.stringify(persisted));
	} catch (e) {
		console.error('[Studio] Failed to save assets:', e);
	}
}

// =============================================================================
// Store Creation
// =============================================================================

function getDefaultState(): StudioState {
	return {
		activeGeneration: null,
		imageProvider: 'google-gemini',
		videoProvider: 'google-veo',
		defaultAspectRatio: '1:1',
		recentGenerations: [],
		savedAssets: [],
		loading: false,
		error: null
	};
}

function createStudioStore() {
	// Load persisted state
	const persistedSettings = loadPersistedSettings();
	const persistedAssets = loadPersistedAssets();

	const initialState: StudioState = {
		...getDefaultState(),
		...persistedSettings,
		savedAssets: persistedAssets
	};

	const { subscribe, set, update } = writable<StudioState>(initialState);

	// Polling timers for active generations
	const pollTimers: Map<string, ReturnType<typeof setInterval>> = new Map();

	/**
	 * Update and persist settings
	 */
	function updateAndPersist(updater: (state: StudioState) => StudioState) {
		update((state) => {
			const newState = updater(state);
			savePersistedSettings(newState);
			return newState;
		});
	}

	/**
	 * Start polling for generation status
	 */
	function startPolling(generationId: string) {
		if (pollTimers.has(generationId)) return;

		const timer = setInterval(async () => {
			try {
				const response = await fetch(`/api/v1/generations/${generationId}`, {
					credentials: 'include'
				});

				if (!response.ok) {
					throw new Error('Failed to fetch generation status');
				}

				const data = await response.json();
				const status = data.status as GenerationStatus;
				const progress = (data.progress as number) || 0;

				update((state) => {
					// Update active generation if this is it
					let activeGeneration = state.activeGeneration;
					if (activeGeneration?.id === generationId) {
						activeGeneration = {
							...activeGeneration,
							status,
							progress,
							resultUrl: data.result_url,
							error: data.error,
							completedAt: data.completed_at ? new Date(data.completed_at) : undefined
						};
					}

					// Update in recent generations
					const recentGenerations = state.recentGenerations.map((gen) =>
						gen.id === generationId
							? {
									...gen,
									status,
									progress,
									resultUrl: data.result_url,
									error: data.error,
									completedAt: data.completed_at ? new Date(data.completed_at) : undefined
								}
							: gen
					);

					return { ...state, activeGeneration, recentGenerations };
				});

				// Update deck store
				deck.updateGeneration(generationId, {
					status,
					progress,
					resultUrl: data.result_url,
					error: data.error,
					completedAt: data.completed_at ? new Date(data.completed_at) : undefined
				});

				// Stop polling if completed or failed
				if (status === 'completed' || status === 'failed') {
					stopPolling(generationId);
				}
			} catch (e) {
				console.error('[Studio] Polling error:', e);
			}
		}, 2000); // Poll every 2 seconds

		pollTimers.set(generationId, timer);
	}

	/**
	 * Stop polling for a generation
	 */
	function stopPolling(generationId: string) {
		const timer = pollTimers.get(generationId);
		if (timer) {
			clearInterval(timer);
			pollTimers.delete(generationId);
		}
	}

	/**
	 * Stop all polling
	 */
	function stopAllPolling() {
		pollTimers.forEach((timer) => clearInterval(timer));
		pollTimers.clear();
	}

	return {
		subscribe,

		// ==========================================================================
		// Generation Actions
		// ==========================================================================

		/**
		 * Generate an image
		 */
		async generateImage(prompt: string, options: GenerationOptions = {}): Promise<string> {
			const state = get({ subscribe });
			const id = `gen-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
			const now = new Date();

			const generation: Generation = {
				id,
				type: 'image',
				prompt,
				status: 'pending',
				progress: 0,
				provider: state.imageProvider,
				options: {
					aspectRatio: options.aspectRatio || state.defaultAspectRatio,
					...options
				},
				createdAt: now
			};

			// Add to local state
			updateAndPersist((s) => ({
				...s,
				activeGeneration: generation,
				recentGenerations: [generation, ...s.recentGenerations],
				loading: true,
				error: null
			}));

			// Add to deck store
			deck.addGeneration({
				id,
				type: 'image',
				prompt,
				status: 'pending',
				progress: 0,
				provider: state.imageProvider,
				createdAt: now
			});

			try {
				const response = await fetch('/api/v1/generate/image', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					credentials: 'include',
					body: JSON.stringify({
						generation_id: id,
						prompt,
						provider: state.imageProvider,
						aspect_ratio: options.aspectRatio || state.defaultAspectRatio,
						style: options.style,
						seed: options.seed,
						negative_prompt: options.negativePrompt,
						image_count: options.imageCount,
						enhance_prompt: options.enhancePrompt
					})
				});

				if (!response.ok) {
					const error = await response.json().catch(() => ({ detail: 'Generation failed' }));
					throw new Error(error.detail || 'Generation failed');
				}

				// Update status to generating
				update((s) => {
					const activeGeneration = s.activeGeneration?.id === id
						? { ...s.activeGeneration, status: 'generating' as GenerationStatus }
						: s.activeGeneration;

					const recentGenerations = s.recentGenerations.map((gen) =>
						gen.id === id ? { ...gen, status: 'generating' as GenerationStatus } : gen
					);

					return { ...s, activeGeneration, recentGenerations, loading: false };
				});

				deck.updateGeneration(id, { status: 'generating' });

				// Start polling for completion
				startPolling(id);

				return id;
			} catch (e) {
				const errorMessage = e instanceof Error ? e.message : 'Generation failed';

				update((s) => {
					const activeGeneration = s.activeGeneration?.id === id
						? { ...s.activeGeneration, status: 'failed' as GenerationStatus, error: errorMessage }
						: s.activeGeneration;

					const recentGenerations = s.recentGenerations.map((gen) =>
						gen.id === id
							? { ...gen, status: 'failed' as GenerationStatus, error: errorMessage }
							: gen
					);

					return { ...s, activeGeneration, recentGenerations, loading: false, error: errorMessage };
				});

				deck.updateGeneration(id, { status: 'failed', error: errorMessage });

				throw e;
			}
		},

		/**
		 * Generate a video
		 */
		async generateVideo(prompt: string, options: GenerationOptions = {}): Promise<string> {
			const state = get({ subscribe });
			const id = `gen-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
			const now = new Date();

			const generation: Generation = {
				id,
				type: 'video',
				prompt,
				status: 'pending',
				progress: 0,
				provider: state.videoProvider,
				options: {
					aspectRatio: options.aspectRatio || state.defaultAspectRatio,
					...options
				},
				createdAt: now
			};

			// Add to local state
			updateAndPersist((s) => ({
				...s,
				activeGeneration: generation,
				recentGenerations: [generation, ...s.recentGenerations],
				loading: true,
				error: null
			}));

			// Add to deck store
			deck.addGeneration({
				id,
				type: 'video',
				prompt,
				status: 'pending',
				progress: 0,
				provider: state.videoProvider,
				createdAt: now
			});

			try {
				const response = await fetch('/api/v1/generate/video', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					credentials: 'include',
					body: JSON.stringify({
						generation_id: id,
						prompt,
						provider: state.videoProvider,
						aspect_ratio: options.aspectRatio || state.defaultAspectRatio,
						duration: options.duration,
						fps: options.fps
					})
				});

				if (!response.ok) {
					const error = await response.json().catch(() => ({ detail: 'Generation failed' }));
					throw new Error(error.detail || 'Generation failed');
				}

				// Update status to generating
				update((s) => {
					const activeGeneration = s.activeGeneration?.id === id
						? { ...s.activeGeneration, status: 'generating' as GenerationStatus }
						: s.activeGeneration;

					const recentGenerations = s.recentGenerations.map((gen) =>
						gen.id === id ? { ...gen, status: 'generating' as GenerationStatus } : gen
					);

					return { ...s, activeGeneration, recentGenerations, loading: false };
				});

				deck.updateGeneration(id, { status: 'generating' });

				// Start polling for completion
				startPolling(id);

				return id;
			} catch (e) {
				const errorMessage = e instanceof Error ? e.message : 'Generation failed';

				update((s) => {
					const activeGeneration = s.activeGeneration?.id === id
						? { ...s.activeGeneration, status: 'failed' as GenerationStatus, error: errorMessage }
						: s.activeGeneration;

					const recentGenerations = s.recentGenerations.map((gen) =>
						gen.id === id
							? { ...gen, status: 'failed' as GenerationStatus, error: errorMessage }
							: gen
					);

					return { ...s, activeGeneration, recentGenerations, loading: false, error: errorMessage };
				});

				deck.updateGeneration(id, { status: 'failed', error: errorMessage });

				throw e;
			}
		},

		/**
		 * Edit an existing image
		 */
		async editImage(assetId: string, prompt: string, options: GenerationOptions = {}): Promise<string> {
			const state = get({ subscribe });
			const asset = state.savedAssets.find((a) => a.id === assetId);

			if (!asset || asset.type !== 'image') {
				throw new Error('Asset not found or not an image');
			}

			const id = `gen-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
			const now = new Date();

			const generation: Generation = {
				id,
				type: 'image',
				prompt: `Edit: ${prompt}`,
				status: 'pending',
				progress: 0,
				provider: state.imageProvider,
				options,
				createdAt: now
			};

			// Add to local state
			updateAndPersist((s) => ({
				...s,
				activeGeneration: generation,
				recentGenerations: [generation, ...s.recentGenerations],
				loading: true,
				error: null
			}));

			// Add to deck store
			deck.addGeneration({
				id,
				type: 'image',
				prompt: `Edit: ${prompt}`,
				status: 'pending',
				progress: 0,
				provider: state.imageProvider,
				createdAt: now
			});

			try {
				const response = await fetch('/api/v1/generate/image/edit', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					credentials: 'include',
					body: JSON.stringify({
						generation_id: id,
						source_asset_id: assetId,
						source_url: asset.url,
						prompt,
						provider: state.imageProvider
					})
				});

				if (!response.ok) {
					const error = await response.json().catch(() => ({ detail: 'Edit failed' }));
					throw new Error(error.detail || 'Edit failed');
				}

				update((s) => {
					const activeGeneration = s.activeGeneration?.id === id
						? { ...s.activeGeneration, status: 'generating' as GenerationStatus }
						: s.activeGeneration;

					const recentGenerations = s.recentGenerations.map((gen) =>
						gen.id === id ? { ...gen, status: 'generating' as GenerationStatus } : gen
					);

					return { ...s, activeGeneration, recentGenerations, loading: false };
				});

				deck.updateGeneration(id, { status: 'generating' });
				startPolling(id);

				return id;
			} catch (e) {
				const errorMessage = e instanceof Error ? e.message : 'Edit failed';

				update((s) => ({
					...s,
					activeGeneration: s.activeGeneration?.id === id
						? { ...s.activeGeneration, status: 'failed' as GenerationStatus, error: errorMessage }
						: s.activeGeneration,
					recentGenerations: s.recentGenerations.map((gen) =>
						gen.id === id
							? { ...gen, status: 'failed' as GenerationStatus, error: errorMessage }
							: gen
					),
					loading: false,
					error: errorMessage
				}));

				deck.updateGeneration(id, { status: 'failed', error: errorMessage });
				throw e;
			}
		},

		/**
		 * Extend a video
		 */
		async extendVideo(assetId: string, prompt: string, options: GenerationOptions = {}): Promise<string> {
			const state = get({ subscribe });
			const asset = state.savedAssets.find((a) => a.id === assetId);

			if (!asset || asset.type !== 'video') {
				throw new Error('Asset not found or not a video');
			}

			const id = `gen-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
			const now = new Date();

			const generation: Generation = {
				id,
				type: 'video',
				prompt: `Extend: ${prompt}`,
				status: 'pending',
				progress: 0,
				provider: state.videoProvider,
				options,
				createdAt: now
			};

			// Add to local state
			updateAndPersist((s) => ({
				...s,
				activeGeneration: generation,
				recentGenerations: [generation, ...s.recentGenerations],
				loading: true,
				error: null
			}));

			// Add to deck store
			deck.addGeneration({
				id,
				type: 'video',
				prompt: `Extend: ${prompt}`,
				status: 'pending',
				progress: 0,
				provider: state.videoProvider,
				createdAt: now
			});

			try {
				const response = await fetch('/api/v1/generate/video/extend', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					credentials: 'include',
					body: JSON.stringify({
						generation_id: id,
						source_asset_id: assetId,
						source_url: asset.url,
						prompt,
						provider: state.videoProvider,
						duration: options.duration
					})
				});

				if (!response.ok) {
					const error = await response.json().catch(() => ({ detail: 'Extension failed' }));
					throw new Error(error.detail || 'Extension failed');
				}

				update((s) => {
					const activeGeneration = s.activeGeneration?.id === id
						? { ...s.activeGeneration, status: 'generating' as GenerationStatus }
						: s.activeGeneration;

					const recentGenerations = s.recentGenerations.map((gen) =>
						gen.id === id ? { ...gen, status: 'generating' as GenerationStatus } : gen
					);

					return { ...s, activeGeneration, recentGenerations, loading: false };
				});

				deck.updateGeneration(id, { status: 'generating' });
				startPolling(id);

				return id;
			} catch (e) {
				const errorMessage = e instanceof Error ? e.message : 'Extension failed';

				update((s) => ({
					...s,
					activeGeneration: s.activeGeneration?.id === id
						? { ...s.activeGeneration, status: 'failed' as GenerationStatus, error: errorMessage }
						: s.activeGeneration,
					recentGenerations: s.recentGenerations.map((gen) =>
						gen.id === id
							? { ...gen, status: 'failed' as GenerationStatus, error: errorMessage }
							: gen
					),
					loading: false,
					error: errorMessage
				}));

				deck.updateGeneration(id, { status: 'failed', error: errorMessage });
				throw e;
			}
		},

		// ==========================================================================
		// Asset Management
		// ==========================================================================

		/**
		 * Save a generation as an asset
		 */
		saveAsset(generation: Generation): string | null {
			if (generation.status !== 'completed' || !generation.resultUrl) {
				console.error('[Studio] Cannot save incomplete generation');
				return null;
			}

			const asset: Asset = {
				id: `asset-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
				type: generation.type,
				url: generation.resultUrl,
				prompt: generation.prompt,
				provider: generation.provider,
				options: generation.options,
				createdAt: new Date(),
				tags: []
			};

			update((state) => {
				const savedAssets = [asset, ...state.savedAssets];
				savePersistedAssets(savedAssets);
				return { ...state, savedAssets };
			});

			return asset.id;
		},

		/**
		 * Delete an asset
		 */
		deleteAsset(id: string) {
			update((state) => {
				const savedAssets = state.savedAssets.filter((a) => a.id !== id);
				savePersistedAssets(savedAssets);
				return { ...state, savedAssets };
			});
		},

		/**
		 * Update asset tags
		 */
		updateAssetTags(id: string, tags: string[]) {
			update((state) => {
				const savedAssets = state.savedAssets.map((a) =>
					a.id === id ? { ...a, tags } : a
				);
				savePersistedAssets(savedAssets);
				return { ...state, savedAssets };
			});
		},

		// ==========================================================================
		// Provider Settings
		// ==========================================================================

		/**
		 * Set default provider for a generation type
		 */
		setProvider(type: 'image' | 'video', provider: string) {
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
		setDefaultAspectRatio(ratio: AspectRatio) {
			updateAndPersist((state) => ({
				...state,
				defaultAspectRatio: ratio
			}));
		},

		// ==========================================================================
		// Utility
		// ==========================================================================

		/**
		 * Clear generation history
		 */
		clearHistory() {
			updateAndPersist((state) => ({
				...state,
				recentGenerations: []
			}));
		},

		/**
		 * Clear error
		 */
		clearError() {
			update((state) => ({ ...state, error: null }));
		},

		/**
		 * Set active generation (for viewing details)
		 */
		setActiveGeneration(id: string | null) {
			update((state) => {
				if (!id) {
					return { ...state, activeGeneration: null };
				}

				const generation = state.recentGenerations.find((g) => g.id === id);
				return { ...state, activeGeneration: generation || null };
			});
		},

		/**
		 * Get generation by ID
		 */
		getGeneration(id: string): Generation | undefined {
			const state = get({ subscribe });
			return state.recentGenerations.find((g) => g.id === id);
		},

		/**
		 * Get asset by ID
		 */
		getAsset(id: string): Asset | undefined {
			const state = get({ subscribe });
			return state.savedAssets.find((a) => a.id === id);
		},

		/**
		 * Cleanup - stop all polling
		 */
		destroy() {
			stopAllPolling();
		}
	};
}

// =============================================================================
// Export Store Instance
// =============================================================================

export const studio = createStudioStore();

// =============================================================================
// Derived Stores
// =============================================================================

/**
 * Currently active generation
 */
export const activeGeneration = derived(studio, ($studio) => $studio.activeGeneration);

/**
 * Recent generations
 */
export const recentGenerations = derived(studio, ($studio) => $studio.recentGenerations);

/**
 * Saved assets
 */
export const savedAssets = derived(studio, ($studio) => $studio.savedAssets);

/**
 * Image assets only
 */
export const imageAssets = derived(studio, ($studio) =>
	$studio.savedAssets.filter((a) => a.type === 'image')
);

/**
 * Video assets only
 */
export const videoAssets = derived(studio, ($studio) =>
	$studio.savedAssets.filter((a) => a.type === 'video')
);

/**
 * Current image provider
 */
export const imageProvider = derived(studio, ($studio) => $studio.imageProvider);

/**
 * Current video provider
 */
export const videoProvider = derived(studio, ($studio) => $studio.videoProvider);

/**
 * Default aspect ratio
 */
export const defaultAspectRatio = derived(studio, ($studio) => $studio.defaultAspectRatio);

/**
 * Loading state
 */
export const studioLoading = derived(studio, ($studio) => $studio.loading);

/**
 * Error state
 */
export const studioError = derived(studio, ($studio) => $studio.error);

/**
 * Pending/generating generations count
 */
export const activeGenerationsCount = derived(
	studio,
	($studio) =>
		$studio.recentGenerations.filter(
			(g) => g.status === 'pending' || g.status === 'generating'
		).length
);

/**
 * Total assets count
 */
export const totalAssetsCount = derived(studio, ($studio) => $studio.savedAssets.length);

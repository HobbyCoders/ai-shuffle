/**
 * Studio Store - Media Generation Management
 *
 * Manages the Studio feature for AI-powered media generation:
 * - Image generation (Gemini, Imagen, GPT Image, DALL-E)
 * - Video generation (Veo, Sora)
 * - TTS (Text-to-Speech) generation
 * - STT (Speech-to-Text) transcription
 * - Asset management and history
 * - Provider/model selection and preferences
 */

import { writable, derived, get } from 'svelte/store';
import { api } from '$lib/api/client';
import type {
	ImageProvider,
	VideoProvider,
	TTSProvider,
	STTProvider,
	ImageModel,
	VideoModel,
	TTSModel,
	STTModel,
	TTSVoice
} from '$lib/types/ai-models';
import {
	ALL_IMAGE_MODELS,
	ALL_VIDEO_MODELS,
	ALL_TTS_MODELS,
	ALL_STT_MODELS,
	getImageModel,
	getVideoModel,
	getTTSModel,
	getSTTModel
} from '$lib/types/ai-models';

// ============================================================================
// Types
// ============================================================================

export type { ImageProvider, VideoProvider, TTSProvider, STTProvider };

export type GenerationType = 'image' | 'video' | 'tts' | 'stt';
export type GenerationStatus = 'pending' | 'generating' | 'completed' | 'failed';

export interface GenerationSettings {
	// Common
	aspectRatio?: string;
	resolution?: string;
	// Image specific
	style?: string;
	referenceImages?: string[];
	mask?: string;
	fidelity?: 'low' | 'high';
	negativePrompt?: string;
	// Video specific
	duration?: number;
	audioEnabled?: boolean;
	startFrame?: string;
	endFrame?: string;
	// TTS specific
	voice?: string;
	voiceInstructions?: string;
	speed?: number;
	outputFormat?: string;
	// STT specific
	language?: string;
	diarization?: boolean;
	translate?: boolean;
	timestampGranularity?: 'word' | 'segment';
}

export interface DeckGeneration {
	id: string;
	type: GenerationType;
	prompt: string;
	provider: ImageProvider | VideoProvider | TTSProvider | STTProvider;
	model: string;
	status: GenerationStatus;
	progress: number; // 0-100
	result?: {
		url: string;
		width?: number;
		height?: number;
		duration?: number; // For videos/audio, in seconds
		transcript?: string; // For STT
		speakers?: { id: string; segments: { start: number; end: number; text: string }[] }[]; // For STT with diarization
	};
	error?: string;
	settings: GenerationSettings;
	startedAt: Date;
	completedAt?: Date;
}

export interface Asset {
	id: string;
	type: GenerationType;
	url: string;
	prompt: string;
	provider: string;
	model: string;
	createdAt: Date;
	tags: string[];
	// Media metadata
	width?: number;
	height?: number;
	duration?: number;
	fileSize?: number;
	// Audio specific
	transcript?: string;
}

export interface StudioState {
	activeGeneration: DeckGeneration | null;
	// Image settings
	imageProvider: ImageProvider;
	imageModel: string;
	// Video settings
	videoProvider: VideoProvider;
	videoModel: string;
	// TTS settings
	ttsProvider: TTSProvider;
	ttsModel: string;
	ttsVoice: string;
	// STT settings
	sttProvider: STTProvider;
	sttModel: string;
	// Default settings
	defaultAspectRatio: string;
	defaultResolution: string;
	defaultVideoDuration: number;
	defaultTTSSpeed: number;
	defaultTTSFormat: string;
	// Collections
	recentGenerations: DeckGeneration[];
	savedAssets: Asset[];
	// UI state
	isLoading: boolean;
	error: string | null;
	// Active tab in studio
	activeTab: 'image' | 'video' | 'tts' | 'stt';
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
	imageModel: string;
	videoProvider: VideoProvider;
	videoModel: string;
	ttsProvider: TTSProvider;
	ttsModel: string;
	ttsVoice: string;
	sttProvider: STTProvider;
	sttModel: string;
	defaultAspectRatio: string;
	defaultResolution: string;
	defaultVideoDuration: number;
	defaultTTSSpeed: number;
	defaultTTSFormat: string;
	activeTab: 'image' | 'video' | 'tts' | 'stt';
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
				imageModel: parsed.imageModel || 'gemini-2.5-flash-image',
				videoProvider: parsed.videoProvider,
				videoModel: parsed.videoModel || 'veo-3.1-generate-preview',
				ttsProvider: parsed.ttsProvider || 'openai-tts',
				ttsModel: parsed.ttsModel || 'gpt-4o-mini-tts',
				ttsVoice: parsed.ttsVoice || 'alloy',
				sttProvider: parsed.sttProvider || 'openai-stt',
				sttModel: parsed.sttModel || 'whisper-1',
				defaultAspectRatio: parsed.defaultAspectRatio,
				defaultResolution: parsed.defaultResolution,
				defaultVideoDuration: parsed.defaultVideoDuration,
				defaultTTSSpeed: parsed.defaultTTSSpeed || 1.0,
				defaultTTSFormat: parsed.defaultTTSFormat || 'mp3',
				activeTab: parsed.activeTab || 'image',
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
				imageModel: state.imageModel,
				videoProvider: state.videoProvider,
				videoModel: state.videoModel,
				ttsProvider: state.ttsProvider,
				ttsModel: state.ttsModel,
				ttsVoice: state.ttsVoice,
				sttProvider: state.sttProvider,
				sttModel: state.sttModel,
				defaultAspectRatio: state.defaultAspectRatio,
				defaultResolution: state.defaultResolution,
				defaultVideoDuration: state.defaultVideoDuration,
				defaultTTSSpeed: state.defaultTTSSpeed,
				defaultTTSFormat: state.defaultTTSFormat,
				activeTab: state.activeTab,
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
		imageModel: persisted?.imageModel || 'gemini-2.5-flash-image',
		videoProvider: persisted?.videoProvider || 'google-veo',
		videoModel: persisted?.videoModel || 'veo-3.1-generate-preview',
		ttsProvider: persisted?.ttsProvider || 'openai-tts',
		ttsModel: persisted?.ttsModel || 'gpt-4o-mini-tts',
		ttsVoice: persisted?.ttsVoice || 'alloy',
		sttProvider: persisted?.sttProvider || 'openai-stt',
		sttModel: persisted?.sttModel || 'whisper-1',
		defaultAspectRatio: persisted?.defaultAspectRatio || '16:9',
		defaultResolution: persisted?.defaultResolution || '1K',
		defaultVideoDuration: persisted?.defaultVideoDuration || 8,
		defaultTTSSpeed: persisted?.defaultTTSSpeed || 1.0,
		defaultTTSFormat: persisted?.defaultTTSFormat || 'mp3',
		activeTab: persisted?.activeTab || 'image',
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
		// Tab Management
		// ========================================================================

		/**
		 * Set active tab
		 */
		setActiveTab(tab: 'image' | 'video' | 'tts' | 'stt'): void {
			updateAndPersist((state) => ({ ...state, activeTab: tab }));
		},

		// ========================================================================
		// Provider & Model Settings
		// ========================================================================

		/**
		 * Set image provider and optionally model
		 */
		setImageProvider(provider: ImageProvider, model?: string): void {
			updateAndPersist((state) => {
				const defaultModel = model || ALL_IMAGE_MODELS.find(m => m.provider === provider && !m.deprecated)?.id || state.imageModel;
				return { ...state, imageProvider: provider, imageModel: defaultModel };
			});
		},

		/**
		 * Set image model
		 */
		setImageModel(model: string): void {
			updateAndPersist((state) => {
				const modelInfo = getImageModel(model);
				return {
					...state,
					imageModel: model,
					imageProvider: modelInfo?.provider || state.imageProvider
				};
			});
		},

		/**
		 * Set video provider and optionally model
		 */
		setVideoProvider(provider: VideoProvider, model?: string): void {
			updateAndPersist((state) => {
				const defaultModel = model || ALL_VIDEO_MODELS.find(m => m.provider === provider && !m.deprecated)?.id || state.videoModel;
				return { ...state, videoProvider: provider, videoModel: defaultModel };
			});
		},

		/**
		 * Set video model
		 */
		setVideoModel(model: string): void {
			updateAndPersist((state) => {
				const modelInfo = getVideoModel(model);
				return {
					...state,
					videoModel: model,
					videoProvider: modelInfo?.provider || state.videoProvider
				};
			});
		},

		/**
		 * Set TTS provider and optionally model
		 */
		setTTSProvider(provider: TTSProvider, model?: string): void {
			updateAndPersist((state) => {
				const defaultModel = model || ALL_TTS_MODELS.find(m => m.provider === provider)?.id || state.ttsModel;
				const modelInfo = getTTSModel(defaultModel);
				const defaultVoice = modelInfo?.voices[0]?.id || state.ttsVoice;
				return { ...state, ttsProvider: provider, ttsModel: defaultModel, ttsVoice: defaultVoice };
			});
		},

		/**
		 * Set TTS model
		 */
		setTTSModel(model: string): void {
			updateAndPersist((state) => {
				const modelInfo = getTTSModel(model);
				const defaultVoice = modelInfo?.voices[0]?.id || state.ttsVoice;
				return {
					...state,
					ttsModel: model,
					ttsProvider: modelInfo?.provider || state.ttsProvider,
					ttsVoice: defaultVoice
				};
			});
		},

		/**
		 * Set TTS voice
		 */
		setTTSVoice(voice: string): void {
			updateAndPersist((state) => ({ ...state, ttsVoice: voice }));
		},

		/**
		 * Set STT provider and optionally model
		 */
		setSTTProvider(provider: STTProvider, model?: string): void {
			updateAndPersist((state) => {
				const defaultModel = model || ALL_STT_MODELS.find(m => m.provider === provider)?.id || state.sttModel;
				return { ...state, sttProvider: provider, sttModel: defaultModel };
			});
		},

		/**
		 * Set STT model
		 */
		setSTTModel(model: string): void {
			updateAndPersist((state) => {
				const modelInfo = getSTTModel(model);
				return {
					...state,
					sttModel: model,
					sttProvider: modelInfo?.provider || state.sttProvider
				};
			});
		},

		/**
		 * Set default aspect ratio
		 */
		setDefaultAspectRatio(aspectRatio: string): void {
			updateAndPersist((state) => ({ ...state, defaultAspectRatio: aspectRatio }));
		},

		/**
		 * Set default resolution
		 */
		setDefaultResolution(resolution: string): void {
			updateAndPersist((state) => ({ ...state, defaultResolution: resolution }));
		},

		/**
		 * Set default video duration
		 */
		setDefaultVideoDuration(duration: number): void {
			updateAndPersist((state) => ({ ...state, defaultVideoDuration: duration }));
		},

		/**
		 * Set default TTS speed
		 */
		setDefaultTTSSpeed(speed: number): void {
			updateAndPersist((state) => ({ ...state, defaultTTSSpeed: speed }));
		},

		/**
		 * Set default TTS output format
		 */
		setDefaultTTSFormat(format: string): void {
			updateAndPersist((state) => ({ ...state, defaultTTSFormat: format }));
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
				model?: string;
				aspectRatio?: string;
				resolution?: string;
				style?: string;
				referenceImages?: string[];
				negativePrompt?: string;
				fidelity?: 'low' | 'high';
			}
		): Promise<DeckGeneration | null> {
			const state = get({ subscribe });
			const provider = options?.provider || state.imageProvider;
			const model = options?.model || state.imageModel;

			const generation: DeckGeneration = {
				id: generateId(),
				type: 'image',
				prompt,
				provider,
				model,
				status: 'generating',
				progress: 0,
				settings: {
					aspectRatio: options?.aspectRatio || state.defaultAspectRatio,
					resolution: options?.resolution || state.defaultResolution,
					style: options?.style,
					referenceImages: options?.referenceImages,
					negativePrompt: options?.negativePrompt,
					fidelity: options?.fidelity
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
				const response = await api.post<{
					id: string;
					url: string;
					width?: number;
					height?: number;
				}>('/canvas/generate/image', {
					prompt,
					provider,
					model,
					aspect_ratio: generation.settings.aspectRatio,
					resolution: generation.settings.resolution,
					style: generation.settings.style,
					reference_images: generation.settings.referenceImages,
					negative_prompt: generation.settings.negativePrompt,
					fidelity: generation.settings.fidelity
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

		/**
		 * Edit an existing image with a prompt
		 */
		async editImage(
			sourceUrl: string,
			prompt: string,
			options?: {
				provider?: ImageProvider;
				model?: string;
				mask?: string;
			}
		): Promise<DeckGeneration | null> {
			const state = get({ subscribe });
			const provider = options?.provider || state.imageProvider;
			const model = options?.model || state.imageModel;

			const generation: DeckGeneration = {
				id: generateId(),
				type: 'image',
				prompt: `Edit: ${prompt}`,
				provider,
				model,
				status: 'generating',
				progress: 0,
				settings: {
					mask: options?.mask
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
				const response = await api.post<{
					id: string;
					url: string;
					width?: number;
					height?: number;
				}>('/canvas/edit/image', {
					source_url: sourceUrl,
					prompt,
					provider,
					model,
					mask: options?.mask
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
				model?: string;
				aspectRatio?: string;
				duration?: number;
				audioEnabled?: boolean;
				sourceImage?: string;
				startFrame?: string;
				endFrame?: string;
			}
		): Promise<DeckGeneration | null> {
			const state = get({ subscribe });
			const provider = options?.provider || state.videoProvider;
			const model = options?.model || state.videoModel;

			const generation: DeckGeneration = {
				id: generateId(),
				type: 'video',
				prompt,
				provider,
				model,
				status: 'generating',
				progress: 0,
				settings: {
					aspectRatio: options?.aspectRatio || state.defaultAspectRatio,
					duration: options?.duration || state.defaultVideoDuration,
					audioEnabled: options?.audioEnabled,
					startFrame: options?.startFrame,
					endFrame: options?.endFrame
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
				const response = await api.post<{
					id: string;
					url: string;
					duration?: number;
					source_video_uri?: string;
				}>('/canvas/generate/video', {
					prompt,
					provider,
					model,
					aspect_ratio: generation.settings.aspectRatio,
					duration: generation.settings.duration,
					audio_enabled: generation.settings.audioEnabled,
					source_image: options?.sourceImage,
					start_frame: options?.startFrame,
					end_frame: options?.endFrame
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

		/**
		 * Extend a video (Veo only)
		 */
		async extendVideo(
			sourceVideoUri: string,
			prompt: string
		): Promise<DeckGeneration | null> {
			const state = get({ subscribe });

			const generation: DeckGeneration = {
				id: generateId(),
				type: 'video',
				prompt: `Extend: ${prompt}`,
				provider: state.videoProvider,
				model: state.videoModel,
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
					source_video_uri: sourceVideoUri,
					prompt,
					provider: state.videoProvider,
					model: state.videoModel
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
		// TTS Generation
		// ========================================================================

		/**
		 * Generate speech from text
		 */
		async generateTTS(
			text: string,
			options?: {
				provider?: TTSProvider;
				model?: string;
				voice?: string;
				voiceInstructions?: string;
				speed?: number;
				outputFormat?: string;
			}
		): Promise<DeckGeneration | null> {
			const state = get({ subscribe });
			const provider = options?.provider || state.ttsProvider;
			const model = options?.model || state.ttsModel;
			const voice = options?.voice || state.ttsVoice;

			const generation: DeckGeneration = {
				id: generateId(),
				type: 'tts',
				prompt: text,
				provider,
				model,
				status: 'generating',
				progress: 0,
				settings: {
					voice,
					voiceInstructions: options?.voiceInstructions,
					speed: options?.speed || state.defaultTTSSpeed,
					outputFormat: options?.outputFormat || state.defaultTTSFormat
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
				const response = await api.post<{
					id: string;
					url: string;
					duration?: number;
				}>('/canvas/generate/tts', {
					text,
					provider,
					model,
					voice,
					voice_instructions: options?.voiceInstructions,
					speed: generation.settings.speed,
					output_format: generation.settings.outputFormat
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
				const errorMessage = e instanceof Error ? e.message : 'Failed to generate speech';

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
		// STT Transcription
		// ========================================================================

		/**
		 * Transcribe audio to text
		 */
		async transcribeAudio(
			audioUrl: string,
			options?: {
				provider?: STTProvider;
				model?: string;
				language?: string;
				diarization?: boolean;
				translate?: boolean;
				timestampGranularity?: 'word' | 'segment';
			}
		): Promise<DeckGeneration | null> {
			const state = get({ subscribe });
			const provider = options?.provider || state.sttProvider;
			const model = options?.model || state.sttModel;

			const generation: DeckGeneration = {
				id: generateId(),
				type: 'stt',
				prompt: `Transcribe: ${audioUrl}`,
				provider,
				model,
				status: 'generating',
				progress: 0,
				settings: {
					language: options?.language,
					diarization: options?.diarization,
					translate: options?.translate,
					timestampGranularity: options?.timestampGranularity
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
				const response = await api.post<{
					id: string;
					transcript: string;
					duration?: number;
					speakers?: { id: string; segments: { start: number; end: number; text: string }[] }[];
				}>('/canvas/transcribe', {
					audio_url: audioUrl,
					provider,
					model,
					language: options?.language,
					diarization: options?.diarization,
					translate: options?.translate,
					timestamp_granularity: options?.timestampGranularity
				});

				const completed: DeckGeneration = {
					...generation,
					status: 'completed',
					progress: 100,
					result: {
						url: audioUrl,
						transcript: response.transcript,
						duration: response.duration,
						speakers: response.speakers
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
				const errorMessage = e instanceof Error ? e.message : 'Failed to transcribe audio';

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
				model: generation.model,
				createdAt: new Date(),
				tags: [],
				width: generation.result.width,
				height: generation.result.height,
				duration: generation.result.duration,
				transcript: generation.result.transcript
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
		 * Get current image model info
		 */
		getCurrentImageModel(): ImageModel | undefined {
			const state = get({ subscribe });
			return getImageModel(state.imageModel);
		},

		/**
		 * Get current video model info
		 */
		getCurrentVideoModel(): VideoModel | undefined {
			const state = get({ subscribe });
			return getVideoModel(state.videoModel);
		},

		/**
		 * Get current TTS model info
		 */
		getCurrentTTSModel(): TTSModel | undefined {
			const state = get({ subscribe });
			return getTTSModel(state.ttsModel);
		},

		/**
		 * Get current STT model info
		 */
		getCurrentSTTModel(): STTModel | undefined {
			const state = get({ subscribe });
			return getSTTModel(state.sttModel);
		},

		/**
		 * Reset store to initial state
		 */
		reset(): void {
			const freshState: StudioState = {
				activeGeneration: null,
				imageProvider: 'google-gemini',
				imageModel: 'gemini-2.5-flash-image',
				videoProvider: 'google-veo',
				videoModel: 'veo-3.1-generate-preview',
				ttsProvider: 'openai-tts',
				ttsModel: 'gpt-4o-mini-tts',
				ttsVoice: 'alloy',
				sttProvider: 'openai-stt',
				sttModel: 'whisper-1',
				defaultAspectRatio: '16:9',
				defaultResolution: '1K',
				defaultVideoDuration: 8,
				defaultTTSSpeed: 1.0,
				defaultTTSFormat: 'mp3',
				activeTab: 'image',
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

// Active tab
export const activeTab = derived(studio, ($studio) => $studio.activeTab);

// Image settings
export const imageProvider = derived(studio, ($studio) => $studio.imageProvider);
export const imageModel = derived(studio, ($studio) => $studio.imageModel);
export const currentImageModelInfo = derived(studio, ($studio) => getImageModel($studio.imageModel));

// Video settings
export const videoProvider = derived(studio, ($studio) => $studio.videoProvider);
export const videoModel = derived(studio, ($studio) => $studio.videoModel);
export const currentVideoModelInfo = derived(studio, ($studio) => getVideoModel($studio.videoModel));

// TTS settings
export const ttsProvider = derived(studio, ($studio) => $studio.ttsProvider);
export const ttsModel = derived(studio, ($studio) => $studio.ttsModel);
export const ttsVoice = derived(studio, ($studio) => $studio.ttsVoice);
export const currentTTSModelInfo = derived(studio, ($studio) => getTTSModel($studio.ttsModel));

// STT settings
export const sttProvider = derived(studio, ($studio) => $studio.sttProvider);
export const sttModel = derived(studio, ($studio) => $studio.sttModel);
export const currentSTTModelInfo = derived(studio, ($studio) => getSTTModel($studio.sttModel));

// Default settings
export const defaultAspectRatio = derived(studio, ($studio) => $studio.defaultAspectRatio);
export const defaultResolution = derived(studio, ($studio) => $studio.defaultResolution);
export const defaultVideoDuration = derived(studio, ($studio) => $studio.defaultVideoDuration);
export const defaultTTSSpeed = derived(studio, ($studio) => $studio.defaultTTSSpeed);
export const defaultTTSFormat = derived(studio, ($studio) => $studio.defaultTTSFormat);

// Recent generations
export const recentGenerations = derived(studio, ($studio) => $studio.recentGenerations);

// Saved assets
export const savedAssets = derived(studio, ($studio) => $studio.savedAssets);

// Filtered assets
export const imageAssets = derived(studio, ($studio) =>
	$studio.savedAssets.filter((a) => a.type === 'image')
);

export const videoAssets = derived(studio, ($studio) =>
	$studio.savedAssets.filter((a) => a.type === 'video')
);

export const audioAssets = derived(studio, ($studio) =>
	$studio.savedAssets.filter((a) => a.type === 'tts' || a.type === 'stt')
);

// Completed/failed generations
export const completedGenerations = derived(studio, ($studio) =>
	$studio.recentGenerations.filter((g) => g.status === 'completed')
);

export const failedGenerations = derived(studio, ($studio) =>
	$studio.recentGenerations.filter((g) => g.status === 'failed')
);

// Generations by type
export const imageGenerations = derived(studio, ($studio) =>
	$studio.recentGenerations.filter((g) => g.type === 'image')
);

export const videoGenerations = derived(studio, ($studio) =>
	$studio.recentGenerations.filter((g) => g.type === 'video')
);

export const ttsGenerations = derived(studio, ($studio) =>
	$studio.recentGenerations.filter((g) => g.type === 'tts')
);

export const sttGenerations = derived(studio, ($studio) =>
	$studio.recentGenerations.filter((g) => g.type === 'stt')
);

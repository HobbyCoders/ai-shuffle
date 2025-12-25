/**
 * AI Models Type Definitions
 *
 * Comprehensive type definitions for all AI models across providers:
 * - Google (Gemini, Imagen, Veo, Cloud TTS, Cloud STT)
 * - OpenAI (GPT-Image, DALL-E, Sora, TTS, Whisper)
 */

// ============================================================================
// Provider Types
// ============================================================================

export type ImageProvider = 'google-gemini' | 'google-imagen' | 'openai-gpt-image' | 'openai-dalle';
export type VideoProvider = 'google-veo' | 'openai-sora';
export type TTSProvider = 'google-tts' | 'openai-tts';
export type STTProvider = 'google-stt' | 'openai-stt';

// ============================================================================
// Image Model Types
// ============================================================================

export interface ImageModelCapabilities {
	generation: boolean;
	editing: boolean;
	inpainting: boolean;
	referenceImages: boolean;
	variations: boolean;
	textRendering: 'none' | 'basic' | 'good' | 'excellent';
	maxReferenceImages: number;
}

export interface ImageModel {
	id: string;
	name: string;
	displayName: string;
	provider: ImageProvider;
	description: string;
	capabilities: ImageModelCapabilities;
	aspectRatios: string[];
	resolutions: string[];
	maxOutputSize: string;
	pricePerImage: number;
	deprecated?: boolean;
	deprecationDate?: string;
}

export interface ImageAspectRatio {
	value: string;
	label: string;
	width: number;
	height: number;
	supportedBy: ImageProvider[];
}

export interface ImageStylePreset {
	id: string;
	name: string;
	description?: string;
	supportedBy: ImageProvider[];
}

// ============================================================================
// Video Model Types
// ============================================================================

export interface VideoModelCapabilities {
	generation: boolean;
	imageToVideo: boolean;
	frameBridging: boolean;
	extension: boolean;
	nativeAudio: boolean;
	remix: boolean;
}

export interface VideoModel {
	id: string;
	name: string;
	displayName: string;
	provider: VideoProvider;
	description: string;
	capabilities: VideoModelCapabilities;
	durations: number[];
	maxDuration: number;
	aspectRatios: string[];
	resolutions: string[];
	pricePerSecond: number;
	deprecated?: boolean;
}

// ============================================================================
// TTS Model Types
// ============================================================================

export interface TTSVoice {
	id: string;
	name: string;
	language: string;
	gender?: 'male' | 'female' | 'neutral';
	description?: string;
	previewUrl?: string;
}

export interface TTSModelCapabilities {
	streaming: boolean;
	ssml: boolean;
	customVoices: boolean;
	steerability: boolean;
	multiSpeaker: boolean;
	emotionControl: boolean;
}

export interface TTSModel {
	id: string;
	name: string;
	displayName: string;
	provider: TTSProvider;
	description: string;
	capabilities: TTSModelCapabilities;
	voices: TTSVoice[];
	outputFormats: string[];
	speedRange: { min: number; max: number };
	maxInputLength: number;
	pricePerMillion: number;
}

// ============================================================================
// STT Model Types
// ============================================================================

export interface STTModelCapabilities {
	realtime: boolean;
	diarization: boolean;
	translation: boolean;
	timestamps: boolean;
	punctuation: boolean;
	profanityFilter: boolean;
	customVocabulary: boolean;
}

export interface STTModel {
	id: string;
	name: string;
	displayName: string;
	provider: STTProvider;
	description: string;
	capabilities: STTModelCapabilities;
	inputFormats: string[];
	languages: string[];
	maxFileSizeMB: number;
	pricePerMinute: number;
}

// ============================================================================
// Model Definitions - Google
// ============================================================================

export const GOOGLE_IMAGE_MODELS: ImageModel[] = [
	{
		id: 'gemini-2.5-flash-image',
		name: 'gemini-2.5-flash-image',
		displayName: 'Nano Banana',
		provider: 'google-gemini',
		description: 'Fast iteration, editing, multi-image composition, character consistency',
		capabilities: {
			generation: true,
			editing: true,
			inpainting: false,
			referenceImages: true,
			variations: false,
			textRendering: 'basic',
			maxReferenceImages: 14
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3', '21:9', '5:4', '4:5'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '1344x768',
		pricePerImage: 0.039
	},
	{
		id: 'gemini-3-pro-image-preview',
		name: 'gemini-3-pro-image-preview',
		displayName: 'Nano Banana Pro',
		provider: 'google-gemini',
		description: 'Higher quality generation with enhanced editing capabilities',
		capabilities: {
			generation: true,
			editing: true,
			inpainting: false,
			referenceImages: true,
			variations: false,
			textRendering: 'good',
			maxReferenceImages: 14
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3', '21:9', '5:4', '4:5'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.06
	},
	{
		id: 'imagen-4.0-generate-001',
		name: 'imagen-4.0-generate-001',
		displayName: 'Imagen 4',
		provider: 'google-imagen',
		description: 'Highest quality, photorealistic images, excellent text rendering',
		capabilities: {
			generation: true,
			editing: true,
			inpainting: true,
			referenceImages: true,
			variations: false,
			textRendering: 'excellent',
			maxReferenceImages: 4
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.04
	},
	{
		id: 'imagen-4.0-fast-generate-001',
		name: 'imagen-4.0-fast-generate-001',
		displayName: 'Imagen 4 Fast',
		provider: 'google-imagen',
		description: 'Speed-optimized Imagen 4 for faster generation',
		capabilities: {
			generation: true,
			editing: true,
			inpainting: true,
			referenceImages: true,
			variations: false,
			textRendering: 'excellent',
			maxReferenceImages: 4
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.02
	},
	{
		id: 'imagen-4.0-ultra-generate-001',
		name: 'imagen-4.0-ultra-generate-001',
		displayName: 'Imagen 4 Ultra',
		provider: 'google-imagen',
		description: 'Quality-optimized Imagen 4 for best results',
		capabilities: {
			generation: true,
			editing: true,
			inpainting: true,
			referenceImages: true,
			variations: false,
			textRendering: 'excellent',
			maxReferenceImages: 4
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.06
	}
];

export const GOOGLE_VIDEO_MODELS: VideoModel[] = [
	{
		id: 'veo-3.1-generate-preview',
		name: 'veo-3.1-generate-preview',
		displayName: 'Veo 3.1',
		provider: 'google-veo',
		description: 'State-of-the-art video with native synchronized audio',
		capabilities: {
			generation: true,
			imageToVideo: true,
			frameBridging: true,
			extension: true,
			nativeAudio: true,
			remix: false
		},
		durations: [4, 6, 8],
		maxDuration: 60,
		aspectRatios: ['16:9', '9:16'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.40
	},
	{
		id: 'veo-3.1-fast-generate-preview',
		name: 'veo-3.1-fast-generate-preview',
		displayName: 'Veo 3.1 Fast',
		provider: 'google-veo',
		description: 'Speed-optimized Veo 3.1 for faster generation',
		capabilities: {
			generation: true,
			imageToVideo: true,
			frameBridging: true,
			extension: true,
			nativeAudio: true,
			remix: false
		},
		durations: [4, 6, 8],
		maxDuration: 60,
		aspectRatios: ['16:9', '9:16'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.15
	},
	{
		id: 'veo-3-generate-preview',
		name: 'veo-3-generate-preview',
		displayName: 'Veo 3',
		provider: 'google-veo',
		description: 'High-quality video generation with audio support',
		capabilities: {
			generation: true,
			imageToVideo: true,
			frameBridging: true,
			extension: true,
			nativeAudio: true,
			remix: false
		},
		durations: [4, 6, 8],
		maxDuration: 60,
		aspectRatios: ['16:9', '9:16'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.35
	},
	{
		id: 'veo-2-generate-001',
		name: 'veo-2-generate-001',
		displayName: 'Veo 2',
		provider: 'google-veo',
		description: 'Silent animations and stylized outputs',
		capabilities: {
			generation: true,
			imageToVideo: true,
			frameBridging: false,
			extension: false,
			nativeAudio: false,
			remix: false
		},
		durations: [4, 6, 8],
		maxDuration: 8,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.20
	}
];

export const GOOGLE_TTS_MODELS: TTSModel[] = [
	{
		id: 'gemini-2.5-flash-tts',
		name: 'gemini-2.5-flash-tts',
		displayName: 'Gemini TTS',
		provider: 'google-tts',
		description: 'Natural language style control, emotion, multi-speaker',
		capabilities: {
			streaming: true,
			ssml: true,
			customVoices: false,
			steerability: true,
			multiSpeaker: true,
			emotionControl: true
		},
		voices: [], // Dynamically loaded
		outputFormats: ['mp3', 'wav', 'ogg', 'opus'],
		speedRange: { min: 0.5, max: 2.0 },
		maxInputLength: 5000,
		pricePerMillion: 15.0
	},
	{
		id: 'chirp-3-hd',
		name: 'chirp-3-hd',
		displayName: 'Chirp 3 HD',
		provider: 'google-tts',
		description: 'Low-latency, real-time communication optimized',
		capabilities: {
			streaming: true,
			ssml: true,
			customVoices: true,
			steerability: false,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: [], // Dynamically loaded (30 styles, 80+ locales)
		outputFormats: ['mp3', 'wav', 'ogg', 'linear16'],
		speedRange: { min: 0.25, max: 4.0 },
		maxInputLength: 5000,
		pricePerMillion: 16.0
	},
	{
		id: 'neural2',
		name: 'neural2',
		displayName: 'Neural2',
		provider: 'google-tts',
		description: 'High-quality natural speech, no training required',
		capabilities: {
			streaming: true,
			ssml: true,
			customVoices: false,
			steerability: false,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: [], // Dynamically loaded
		outputFormats: ['mp3', 'wav', 'ogg', 'linear16'],
		speedRange: { min: 0.25, max: 4.0 },
		maxInputLength: 5000,
		pricePerMillion: 16.0
	},
	{
		id: 'wavenet',
		name: 'wavenet',
		displayName: 'WaveNet',
		provider: 'google-tts',
		description: 'Premium voices with natural emphasis and inflection',
		capabilities: {
			streaming: true,
			ssml: true,
			customVoices: false,
			steerability: false,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: [], // Dynamically loaded
		outputFormats: ['mp3', 'wav', 'ogg', 'linear16'],
		speedRange: { min: 0.25, max: 4.0 },
		maxInputLength: 5000,
		pricePerMillion: 16.0
	},
	{
		id: 'standard',
		name: 'standard',
		displayName: 'Standard',
		provider: 'google-tts',
		description: 'Cost-effective traditional synthetic voices',
		capabilities: {
			streaming: true,
			ssml: true,
			customVoices: false,
			steerability: false,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: [], // Dynamically loaded (300+ voices, 50+ languages)
		outputFormats: ['mp3', 'wav', 'ogg', 'linear16'],
		speedRange: { min: 0.25, max: 4.0 },
		maxInputLength: 5000,
		pricePerMillion: 4.0
	}
];

export const GOOGLE_STT_MODELS: STTModel[] = [
	{
		id: 'gemini-2.5-flash',
		name: 'gemini-2.5-flash',
		displayName: 'Gemini Audio',
		provider: 'google-stt',
		description: 'Long-form audio, diarization, context-aware transcription',
		capabilities: {
			realtime: false,
			diarization: true,
			translation: true,
			timestamps: true,
			punctuation: true,
			profanityFilter: true,
			customVocabulary: false
		},
		inputFormats: ['wav', 'mp3', 'aiff', 'aac', 'ogg', 'flac'],
		languages: ['en', 'es', 'fr', 'de', 'it', 'pt', 'zh', 'ja', 'ko', 'ar', 'hi', 'ru'],
		maxFileSizeMB: 2000,
		pricePerMinute: 0.006
	},
	{
		id: 'chirp-3',
		name: 'chirp_3',
		displayName: 'Chirp 3',
		provider: 'google-stt',
		description: 'Multilingual, noisy environments, latest accuracy',
		capabilities: {
			realtime: true,
			diarization: true,
			translation: false,
			timestamps: true,
			punctuation: true,
			profanityFilter: true,
			customVocabulary: true
		},
		inputFormats: ['wav', 'mp3', 'flac', 'ogg', 'webm'],
		languages: ['100+ languages'],
		maxFileSizeMB: 480,
		pricePerMinute: 0.016
	},
	{
		id: 'latest-long',
		name: 'latest_long',
		displayName: 'Latest Long',
		provider: 'google-stt',
		description: 'Long-form content, media, conversations',
		capabilities: {
			realtime: true,
			diarization: true,
			translation: false,
			timestamps: true,
			punctuation: true,
			profanityFilter: true,
			customVocabulary: true
		},
		inputFormats: ['wav', 'mp3', 'flac', 'ogg'],
		languages: ['en', 'es', 'fr', 'de', 'it', 'pt', 'ja', 'ko', 'zh', 'ar', 'hi', 'ru', 'nl'],
		maxFileSizeMB: 480,
		pricePerMinute: 0.016
	},
	{
		id: 'latest-short',
		name: 'latest_short',
		displayName: 'Latest Short',
		provider: 'google-stt',
		description: 'Voice commands, short utterances',
		capabilities: {
			realtime: true,
			diarization: false,
			translation: false,
			timestamps: true,
			punctuation: true,
			profanityFilter: true,
			customVocabulary: true
		},
		inputFormats: ['wav', 'mp3', 'flac', 'ogg'],
		languages: ['en', 'es', 'fr', 'de', 'it', 'pt', 'ja', 'ko', 'zh', 'ar', 'hi', 'ru', 'nl'],
		maxFileSizeMB: 60,
		pricePerMinute: 0.016
	}
];

// ============================================================================
// Model Definitions - OpenAI
// ============================================================================

export const OPENAI_IMAGE_MODELS: ImageModel[] = [
	{
		id: 'gpt-image-1.5',
		name: 'gpt-image-1.5',
		displayName: 'GPT Image 1.5',
		provider: 'openai-gpt-image',
		description: 'Latest model - best text rendering, precise editing, 4x faster',
		capabilities: {
			generation: true,
			editing: true,
			inpainting: true,
			referenceImages: true,
			variations: false,
			textRendering: 'excellent',
			maxReferenceImages: 4
		},
		aspectRatios: ['1:1', '3:2', '2:3'],
		resolutions: ['1K', '2K', '4K'],
		maxOutputSize: '4096x4096',
		pricePerImage: 0.04
	},
	{
		id: 'gpt-image-1',
		name: 'gpt-image-1',
		displayName: 'GPT Image 1',
		provider: 'openai-gpt-image',
		description: 'Multimodal generation and editing with soft mask inpainting',
		capabilities: {
			generation: true,
			editing: true,
			inpainting: true,
			referenceImages: true,
			variations: false,
			textRendering: 'good',
			maxReferenceImages: 4
		},
		aspectRatios: ['1:1', '3:2', '2:3'],
		resolutions: ['1K', '2K', '4K'],
		maxOutputSize: '4096x4096',
		pricePerImage: 0.05
	},
	{
		id: 'gpt-image-1-mini',
		name: 'gpt-image-1-mini',
		displayName: 'GPT Image 1 Mini',
		provider: 'openai-gpt-image',
		description: 'Cost-efficient version for faster, cheaper generation',
		capabilities: {
			generation: true,
			editing: true,
			inpainting: false,
			referenceImages: true,
			variations: false,
			textRendering: 'basic',
			maxReferenceImages: 2
		},
		aspectRatios: ['1:1', '3:2', '2:3'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.02
	},
];

export const OPENAI_VIDEO_MODELS: VideoModel[] = [
	{
		id: 'sora-2',
		name: 'sora-2',
		displayName: 'Sora 2',
		provider: 'openai-sora',
		description: 'Text/image-to-video with synchronized audio',
		capabilities: {
			generation: true,
			imageToVideo: true,
			frameBridging: false,
			extension: false,
			nativeAudio: true,
			remix: true
		},
		durations: [4, 8, 12, 20],
		maxDuration: 90,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p', '4K'],
		pricePerSecond: 0.30
	},
	{
		id: 'sora-2-standard',
		name: 'sora-2-standard',
		displayName: 'Sora 2 Standard',
		provider: 'openai-sora',
		description: 'Cost-effective video generation at 720p',
		capabilities: {
			generation: true,
			imageToVideo: true,
			frameBridging: false,
			extension: false,
			nativeAudio: true,
			remix: false
		},
		durations: [4, 8, 12, 20],
		maxDuration: 60,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p'],
		pricePerSecond: 0.10
	}
];

export const OPENAI_TTS_MODELS: TTSModel[] = [
	{
		id: 'gpt-4o-mini-tts',
		name: 'gpt-4o-mini-tts',
		displayName: 'GPT-4o Mini TTS',
		provider: 'openai-tts',
		description: 'Steerable TTS with custom voice instructions',
		capabilities: {
			streaming: true,
			ssml: false,
			customVoices: true,
			steerability: true,
			multiSpeaker: false,
			emotionControl: true
		},
		voices: [
			{ id: 'alloy', name: 'Alloy', language: 'en', gender: 'neutral' },
			{ id: 'ash', name: 'Ash', language: 'en', gender: 'male' },
			{ id: 'ballad', name: 'Ballad', language: 'en', gender: 'male' },
			{ id: 'coral', name: 'Coral', language: 'en', gender: 'female' },
			{ id: 'echo', name: 'Echo', language: 'en', gender: 'male' },
			{ id: 'fable', name: 'Fable', language: 'en', gender: 'male' },
			{ id: 'onyx', name: 'Onyx', language: 'en', gender: 'male' },
			{ id: 'nova', name: 'Nova', language: 'en', gender: 'female' },
			{ id: 'sage', name: 'Sage', language: 'en', gender: 'female' },
			{ id: 'shimmer', name: 'Shimmer', language: 'en', gender: 'female' },
			{ id: 'verse', name: 'Verse', language: 'en', gender: 'neutral' },
			{ id: 'marin', name: 'Marin', language: 'en', gender: 'female' },
			{ id: 'cedar', name: 'Cedar', language: 'en', gender: 'male' }
		],
		outputFormats: ['mp3', 'opus', 'aac', 'flac', 'wav', 'pcm'],
		speedRange: { min: 0.25, max: 4.0 },
		maxInputLength: 4096,
		pricePerMillion: 12.0
	},
	{
		id: 'tts-1-hd',
		name: 'tts-1-hd',
		displayName: 'TTS-1 HD',
		provider: 'openai-tts',
		description: 'High-quality TTS optimized for quality',
		capabilities: {
			streaming: true,
			ssml: false,
			customVoices: false,
			steerability: false,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: [
			{ id: 'alloy', name: 'Alloy', language: 'en', gender: 'neutral' },
			{ id: 'ash', name: 'Ash', language: 'en', gender: 'male' },
			{ id: 'ballad', name: 'Ballad', language: 'en', gender: 'male' },
			{ id: 'coral', name: 'Coral', language: 'en', gender: 'female' },
			{ id: 'echo', name: 'Echo', language: 'en', gender: 'male' },
			{ id: 'fable', name: 'Fable', language: 'en', gender: 'male' },
			{ id: 'onyx', name: 'Onyx', language: 'en', gender: 'male' },
			{ id: 'nova', name: 'Nova', language: 'en', gender: 'female' },
			{ id: 'sage', name: 'Sage', language: 'en', gender: 'female' },
			{ id: 'shimmer', name: 'Shimmer', language: 'en', gender: 'female' },
			{ id: 'verse', name: 'Verse', language: 'en', gender: 'neutral' },
			{ id: 'marin', name: 'Marin', language: 'en', gender: 'female' },
			{ id: 'cedar', name: 'Cedar', language: 'en', gender: 'male' }
		],
		outputFormats: ['mp3', 'opus', 'aac', 'flac', 'wav', 'pcm'],
		speedRange: { min: 0.25, max: 4.0 },
		maxInputLength: 4096,
		pricePerMillion: 30.0
	},
	{
		id: 'tts-1',
		name: 'tts-1',
		displayName: 'TTS-1',
		provider: 'openai-tts',
		description: 'Fast TTS optimized for real-time use',
		capabilities: {
			streaming: true,
			ssml: false,
			customVoices: false,
			steerability: false,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: [
			{ id: 'alloy', name: 'Alloy', language: 'en', gender: 'neutral' },
			{ id: 'ash', name: 'Ash', language: 'en', gender: 'male' },
			{ id: 'ballad', name: 'Ballad', language: 'en', gender: 'male' },
			{ id: 'coral', name: 'Coral', language: 'en', gender: 'female' },
			{ id: 'echo', name: 'Echo', language: 'en', gender: 'male' },
			{ id: 'fable', name: 'Fable', language: 'en', gender: 'male' },
			{ id: 'onyx', name: 'Onyx', language: 'en', gender: 'male' },
			{ id: 'nova', name: 'Nova', language: 'en', gender: 'female' },
			{ id: 'sage', name: 'Sage', language: 'en', gender: 'female' },
			{ id: 'shimmer', name: 'Shimmer', language: 'en', gender: 'female' },
			{ id: 'verse', name: 'Verse', language: 'en', gender: 'neutral' },
			{ id: 'marin', name: 'Marin', language: 'en', gender: 'female' },
			{ id: 'cedar', name: 'Cedar', language: 'en', gender: 'male' }
		],
		outputFormats: ['mp3', 'opus', 'aac', 'flac', 'wav', 'pcm'],
		speedRange: { min: 0.25, max: 4.0 },
		maxInputLength: 4096,
		pricePerMillion: 15.0
	}
];

export const OPENAI_STT_MODELS: STTModel[] = [
	{
		id: 'gpt-4o-transcribe-diarize',
		name: 'gpt-4o-transcribe-diarize',
		displayName: 'GPT-4o Transcribe + Diarize',
		provider: 'openai-stt',
		description: 'Ultra-fast transcription with speaker identification',
		capabilities: {
			realtime: false,
			diarization: true,
			translation: false,
			timestamps: true,
			punctuation: true,
			profanityFilter: false,
			customVocabulary: false
		},
		inputFormats: ['flac', 'mp3', 'mp4', 'mpeg', 'mpga', 'm4a', 'ogg', 'wav', 'webm'],
		languages: ['100+ languages'],
		maxFileSizeMB: 25,
		pricePerMinute: 0.006
	},
	{
		id: 'gpt-4o-transcribe',
		name: 'gpt-4o-transcribe',
		displayName: 'GPT-4o Transcribe',
		provider: 'openai-stt',
		description: 'Fast transcription (10 min in ~15 seconds)',
		capabilities: {
			realtime: false,
			diarization: false,
			translation: false,
			timestamps: true,
			punctuation: true,
			profanityFilter: false,
			customVocabulary: false
		},
		inputFormats: ['flac', 'mp3', 'mp4', 'mpeg', 'mpga', 'm4a', 'ogg', 'wav', 'webm'],
		languages: ['100+ languages'],
		maxFileSizeMB: 25,
		pricePerMinute: 0.006
	},
	{
		id: 'gpt-4o-mini-transcribe',
		name: 'gpt-4o-mini-transcribe',
		displayName: 'GPT-4o Mini Transcribe',
		provider: 'openai-stt',
		description: 'Cost-efficient transcription',
		capabilities: {
			realtime: false,
			diarization: false,
			translation: false,
			timestamps: true,
			punctuation: true,
			profanityFilter: false,
			customVocabulary: false
		},
		inputFormats: ['flac', 'mp3', 'mp4', 'mpeg', 'mpga', 'm4a', 'ogg', 'wav', 'webm'],
		languages: ['100+ languages'],
		maxFileSizeMB: 25,
		pricePerMinute: 0.003
	},
	{
		id: 'whisper-1',
		name: 'whisper-1',
		displayName: 'Whisper',
		provider: 'openai-stt',
		description: 'General-purpose with translation to English',
		capabilities: {
			realtime: false,
			diarization: false,
			translation: true,
			timestamps: true,
			punctuation: true,
			profanityFilter: false,
			customVocabulary: false
		},
		inputFormats: ['flac', 'mp3', 'mp4', 'mpeg', 'mpga', 'm4a', 'ogg', 'wav', 'webm'],
		languages: ['100+ languages'],
		maxFileSizeMB: 25,
		pricePerMinute: 0.006
	}
];

// ============================================================================
// Combined Model Collections
// ============================================================================

export const ALL_IMAGE_MODELS: ImageModel[] = [...GOOGLE_IMAGE_MODELS, ...OPENAI_IMAGE_MODELS];
export const ALL_VIDEO_MODELS: VideoModel[] = [...GOOGLE_VIDEO_MODELS, ...OPENAI_VIDEO_MODELS];
export const ALL_TTS_MODELS: TTSModel[] = [...GOOGLE_TTS_MODELS, ...OPENAI_TTS_MODELS];
export const ALL_STT_MODELS: STTModel[] = [...GOOGLE_STT_MODELS, ...OPENAI_STT_MODELS];

// ============================================================================
// Style Presets
// ============================================================================

export const IMAGE_STYLE_PRESETS: ImageStylePreset[] = [
	{ id: 'none', name: 'None', supportedBy: ['google-gemini', 'google-imagen', 'openai-gpt-image', 'openai-dalle'] },
	{ id: 'photographic', name: 'Photographic', description: 'Realistic photography', supportedBy: ['google-gemini', 'google-imagen', 'openai-gpt-image', 'openai-dalle'] },
	{ id: 'digital-art', name: 'Digital Art', description: 'Modern digital illustration', supportedBy: ['google-gemini', 'google-imagen', 'openai-gpt-image', 'openai-dalle'] },
	{ id: 'anime', name: 'Anime', description: 'Japanese anime style', supportedBy: ['google-gemini', 'openai-gpt-image'] },
	{ id: 'watercolor', name: 'Watercolor', description: 'Soft watercolor painting', supportedBy: ['google-gemini', 'google-imagen'] },
	{ id: 'oil-painting', name: 'Oil Painting', description: 'Classical oil painting', supportedBy: ['google-gemini', 'google-imagen'] },
	{ id: 'sketch', name: 'Sketch', description: 'Pencil or charcoal sketch', supportedBy: ['google-gemini', 'google-imagen', 'openai-gpt-image'] },
	{ id: 'cinematic', name: 'Cinematic', description: 'Film-like dramatic lighting', supportedBy: ['google-gemini', 'openai-gpt-image', 'openai-dalle'] },
	{ id: 'retro-comic', name: 'Retro Comic', description: 'Vintage comic book style', supportedBy: ['google-imagen'] },
	{ id: 'pixel-art', name: 'Pixel Art', description: 'Retro pixel graphics', supportedBy: ['google-imagen', 'openai-gpt-image'] },
	{ id: 'sumi-e', name: 'Sumi-e', description: 'Japanese ink wash painting', supportedBy: ['google-imagen'] },
	{ id: '3d-render', name: '3D Render', description: 'Photorealistic 3D rendering', supportedBy: ['google-gemini', 'openai-gpt-image'] },
	{ id: 'vivid', name: 'Vivid', description: 'Hyper-realistic and dramatic', supportedBy: ['openai-dalle'] },
	{ id: 'natural', name: 'Natural', description: 'Less hyper-real, more natural', supportedBy: ['openai-dalle'] }
];

// ============================================================================
// Aspect Ratios
// ============================================================================

export const IMAGE_ASPECT_RATIOS: ImageAspectRatio[] = [
	{ value: '1:1', label: 'Square', width: 40, height: 40, supportedBy: ['google-gemini', 'google-imagen', 'openai-gpt-image', 'openai-dalle'] },
	{ value: '16:9', label: 'Landscape', width: 48, height: 27, supportedBy: ['google-gemini', 'google-imagen', 'openai-dalle'] },
	{ value: '9:16', label: 'Portrait', width: 27, height: 48, supportedBy: ['google-gemini', 'google-imagen', 'openai-dalle'] },
	{ value: '4:3', label: 'Standard', width: 44, height: 33, supportedBy: ['google-gemini', 'google-imagen'] },
	{ value: '3:4', label: 'Portrait 3:4', width: 33, height: 44, supportedBy: ['google-gemini', 'google-imagen'] },
	{ value: '3:2', label: 'Photo', width: 45, height: 30, supportedBy: ['google-gemini', 'openai-gpt-image'] },
	{ value: '2:3', label: 'Photo Portrait', width: 30, height: 45, supportedBy: ['google-gemini', 'openai-gpt-image'] },
	{ value: '21:9', label: 'Ultrawide', width: 52, height: 22, supportedBy: ['google-gemini'] },
	{ value: '5:4', label: 'Classic', width: 42, height: 34, supportedBy: ['google-gemini'] },
	{ value: '4:5', label: 'Instagram', width: 34, height: 42, supportedBy: ['google-gemini'] }
];

export const VIDEO_ASPECT_RATIOS: ImageAspectRatio[] = [
	{ value: '16:9', label: 'Landscape', width: 48, height: 27, supportedBy: ['google-veo', 'openai-sora'] },
	{ value: '9:16', label: 'Portrait', width: 27, height: 48, supportedBy: ['google-veo', 'openai-sora'] },
	{ value: '1:1', label: 'Square', width: 40, height: 40, supportedBy: ['google-veo', 'openai-sora'] }
];

// ============================================================================
// Helper Functions
// ============================================================================

export function getImageModel(id: string): ImageModel | undefined {
	return ALL_IMAGE_MODELS.find(m => m.id === id);
}

export function getVideoModel(id: string): VideoModel | undefined {
	return ALL_VIDEO_MODELS.find(m => m.id === id);
}

export function getTTSModel(id: string): TTSModel | undefined {
	return ALL_TTS_MODELS.find(m => m.id === id);
}

export function getSTTModel(id: string): STTModel | undefined {
	return ALL_STT_MODELS.find(m => m.id === id);
}

export function getImageModelsByProvider(provider: ImageProvider): ImageModel[] {
	return ALL_IMAGE_MODELS.filter(m => m.provider === provider && !m.deprecated);
}

export function getVideoModelsByProvider(provider: VideoProvider): VideoModel[] {
	return ALL_VIDEO_MODELS.filter(m => m.provider === provider && !m.deprecated);
}

export function getTTSModelsByProvider(provider: TTSProvider): TTSModel[] {
	return ALL_TTS_MODELS.filter(m => m.provider === provider);
}

export function getSTTModelsByProvider(provider: STTProvider): STTModel[] {
	return ALL_STT_MODELS.filter(m => m.provider === provider);
}

export function getAspectRatiosForModel(modelId: string): ImageAspectRatio[] {
	const model = getImageModel(modelId);
	if (!model) return IMAGE_ASPECT_RATIOS;
	return IMAGE_ASPECT_RATIOS.filter(ar => model.aspectRatios.includes(ar.value));
}

export function getStylePresetsForProvider(provider: ImageProvider): ImageStylePreset[] {
	return IMAGE_STYLE_PRESETS.filter(s => s.supportedBy.includes(provider));
}

export function supportsFeature(modelId: string, feature: keyof ImageModelCapabilities | keyof VideoModelCapabilities): boolean {
	const imageModel = getImageModel(modelId);
	if (imageModel) {
		return Boolean(imageModel.capabilities[feature as keyof ImageModelCapabilities]);
	}
	const videoModel = getVideoModel(modelId);
	if (videoModel) {
		return Boolean(videoModel.capabilities[feature as keyof VideoModelCapabilities]);
	}
	return false;
}

// ============================================================================
// Provider Display Names
// ============================================================================

export const PROVIDER_DISPLAY_NAMES: Record<string, string> = {
	'google-gemini': 'Google Gemini',
	'google-imagen': 'Google Imagen',
	'google-veo': 'Google Veo',
	'google-tts': 'Google Cloud TTS',
	'google-stt': 'Google Cloud STT',
	'openai-gpt-image': 'OpenAI GPT Image',
	'openai-sora': 'OpenAI Sora',
	'openai-tts': 'OpenAI TTS',
	'openai-stt': 'OpenAI Whisper'
};

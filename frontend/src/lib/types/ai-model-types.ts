/**
 * AI Models Type Definitions
 *
 * All AI models powered by fal.ai through a single API key.
 * Includes native fal.ai models and proxied providers (Veo 3.1, Sora 2, Meshy v6).
 */

// ============================================================================
// Provider Types
// ============================================================================
//
// Image/video/3D models are all fal.ai. Speech models (TTS/STT) can also be
// served directly by OpenAI (GPT), so the provider union includes 'openai'.

export type AIProvider = 'fal-ai' | 'openai';

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
	provider: AIProvider;
	description: string;
	capabilities: ImageModelCapabilities;
	aspectRatios: string[];
	resolutions: string[];
	maxOutputSize: string;
	pricePerImage: number;
	deprecated?: boolean;
}

// ============================================================================
// Video Model Types
// ============================================================================

export interface VideoModelCapabilities {
	generation: boolean;
	imageToVideo: boolean;
	nativeAudio: boolean;
}

export interface VideoModel {
	id: string;
	name: string;
	displayName: string;
	provider: AIProvider;
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
// 3D Model Types
// ============================================================================

export type Model3DArtStyle = 'realistic' | 'sculpture';
export type Model3DTopology = 'quad' | 'triangle';

export interface Model3DModelCapabilities {
	textTo3D: boolean;
	imageTo3D: boolean;
	pbr: boolean;
}

export interface Model3DModel {
	id: string;
	name: string;
	displayName: string;
	provider: AIProvider;
	description: string;
	capabilities: Model3DModelCapabilities;
	artStyles: Model3DArtStyle[];
	topologies: Model3DTopology[];
	outputFormats: string[];
	maxPolycount: number;
	pricePerModel: number;
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
	provider: AIProvider;
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
}

export interface STTModel {
	id: string;
	name: string;
	displayName: string;
	provider: AIProvider;
	description: string;
	capabilities: STTModelCapabilities;
	inputFormats: string[];
	languages: string[];
	maxFileSizeMB: number;
	pricePerMinute: number;
}

// ============================================================================
// Dynamic Catalog Types (fetched from fal.ai API)
// ============================================================================

/** A model from the dynamic fal.ai catalog (fetched via /api/v1/catalog/models) */
export interface CatalogModel {
	endpoint_id: string;
	display_name: string;
	category: string;
	description: string;
	status: string;
	thumbnail_url?: string | null;
	tags: string[];
	group_key?: string | null;
	group_label?: string | null;
	tool_type?: string | null;
	output_type?: string | null;
	updated_at?: string | null;
}

/** Category with model count */
export interface CatalogCategory {
	category: string;
	count: number;
	tool_type: string | null;
}

/**
 * AI Model Catalog — Static Data
 *
 * Model lists, voice presets, aspect ratios, style presets, helper functions,
 * and provider display names. All data arrays use types from ai-model-types.ts.
 */

import type {
	ImageModel,
	ImageAspectRatio,
	VideoAspectRatio,
	ImageStylePreset,
	ImageModelCapabilities,
	VideoModel,
	VideoModelCapabilities,
	Model3DModel,
	TTSVoice,
	TTSModel,
	STTModel
} from './ai-model-types';

// ============================================================================
// Image Models — fal.ai
// ============================================================================

export const FAL_IMAGE_MODELS: ImageModel[] = [
	{
		id: 'fal-ai/flux-pro/v1.1-ultra',
		name: 'flux-pro-ultra',
		displayName: 'Flux Pro Ultra',
		provider: 'fal-ai',
		description: 'Highest quality, 4MP raw mode, best for professional work',
		capabilities: {
			generation: true,
			editing: false,
			inpainting: false,
			referenceImages: false,
			variations: false,
			textRendering: 'good',
			maxReferenceImages: 0
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3', '21:9'],
		resolutions: ['1K', '2K', '4K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.06
	},
	{
		id: 'fal-ai/flux-2-flex',
		name: 'flux-2-flex',
		displayName: 'FLUX.2 Flex',
		provider: 'fal-ai',
		description: 'Latest generation Flux with enhanced quality and flexibility',
		capabilities: {
			generation: true,
			editing: false,
			inpainting: false,
			referenceImages: false,
			variations: false,
			textRendering: 'good',
			maxReferenceImages: 0
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.05
	},
	{
		id: 'fal-ai/flux/dev',
		name: 'flux-dev',
		displayName: 'Flux Dev',
		provider: 'fal-ai',
		description: 'Fast, high-quality generation for development and iteration',
		capabilities: {
			generation: true,
			editing: false,
			inpainting: false,
			referenceImages: false,
			variations: false,
			textRendering: 'basic',
			maxReferenceImages: 0
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.025
	},
	{
		id: 'fal-ai/flux/schnell',
		name: 'flux-schnell',
		displayName: 'Flux Schnell',
		provider: 'fal-ai',
		description: 'Fastest Flux model, great for rapid prototyping',
		capabilities: {
			generation: true,
			editing: false,
			inpainting: false,
			referenceImages: false,
			variations: false,
			textRendering: 'basic',
			maxReferenceImages: 0
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.003
	},
	{
		id: 'fal-ai/flux-pro/kontext',
		name: 'flux-kontext',
		displayName: 'Flux Kontext Pro',
		provider: 'fal-ai',
		description: 'Reference image editing and character consistency',
		capabilities: {
			generation: true,
			editing: true,
			inpainting: false,
			referenceImages: true,
			variations: false,
			textRendering: 'good',
			maxReferenceImages: 14
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.04
	},
	{
		id: 'fal-ai/recraft/v4/text-to-image',
		name: 'recraft-v4',
		displayName: 'Recraft V4',
		provider: 'fal-ai',
		description: 'Excellent text rendering, brand-accurate design generation',
		capabilities: {
			generation: true,
			editing: false,
			inpainting: false,
			referenceImages: false,
			variations: false,
			textRendering: 'excellent',
			maxReferenceImages: 0
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.04
	},
	{
		id: 'fal-ai/nano-banana-pro',
		name: 'nano-banana-pro',
		displayName: 'Nano Banana Pro',
		provider: 'fal-ai',
		description: 'Google Gemini-based generation via fal.ai, editing support',
		capabilities: {
			generation: true,
			editing: true,
			inpainting: false,
			referenceImages: true,
			variations: false,
			textRendering: 'good',
			maxReferenceImages: 14
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.04
	},
	{
		id: 'fal-ai/nano-banana-2',
		name: 'nano-banana-2',
		displayName: 'Nano Banana 2',
		provider: 'fal-ai',
		description: 'Google Gemini 3.1 Flash — reasoning-guided generation, 4x faster, excellent text rendering',
		capabilities: {
			generation: true,
			editing: true,
			inpainting: false,
			referenceImages: true,
			variations: false,
			textRendering: 'excellent',
			maxReferenceImages: 14
		},
		aspectRatios: ['1:1', '21:9', '16:9', '3:2', '4:3', '5:4', '4:5', '3:4', '2:3', '9:16'],
		resolutions: ['1K', '2K', '4K'],
		maxOutputSize: '4096x4096',
		pricePerImage: 0.08
	},
	{
		id: 'fal-ai/qwen-image',
		name: 'qwen-image',
		displayName: 'Qwen Image',
		provider: 'fal-ai',
		description: 'Qwen-based image generation with strong CJK text rendering',
		capabilities: {
			generation: true,
			editing: false,
			inpainting: false,
			referenceImages: false,
			variations: false,
			textRendering: 'good',
			maxReferenceImages: 0
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.03
	},
	{
		id: 'xai/grok-imagine-image',
		name: 'grok-imagine-image',
		displayName: 'Grok Imagine Image',
		provider: 'fal-ai',
		description: 'xAI Grok via fal.ai — fast photorealistic generation with editing support',
		capabilities: {
			generation: true,
			editing: true,
			inpainting: false,
			referenceImages: false,
			variations: false,
			textRendering: 'good',
			maxReferenceImages: 0
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.02
	},
	{
		id: 'fal-ai/recraft/v4/pro/text-to-image',
		name: 'recraft-v4-pro',
		displayName: 'Recraft V4 Pro',
		provider: 'fal-ai',
		description: 'Premium Recraft tier — superior text rendering and design quality',
		capabilities: {
			generation: true,
			editing: false,
			inpainting: false,
			referenceImages: false,
			variations: false,
			textRendering: 'excellent',
			maxReferenceImages: 0
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.25
	},
	{
		id: 'fal-ai/bitdance',
		name: 'bitdance',
		displayName: 'BitDance',
		provider: 'fal-ai',
		description: 'Autoregressive LLM-based — fast, cheap photorealistic generation',
		capabilities: {
			generation: true,
			editing: false,
			inpainting: false,
			referenceImages: false,
			variations: false,
			textRendering: 'basic',
			maxReferenceImages: 0
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4'],
		resolutions: ['1K', '2K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.01
	},
	{
		id: 'imagineart/imagineart-1.5-preview/text-to-image',
		name: 'imagineart-1-5',
		displayName: 'ImagineArt 1.5',
		provider: 'fal-ai',
		description: 'Professional-grade visuals with best-in-class text rendering',
		capabilities: {
			generation: true,
			editing: false,
			inpainting: false,
			referenceImages: false,
			variations: false,
			textRendering: 'excellent',
			maxReferenceImages: 0
		},
		aspectRatios: ['1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3', '3:1', '1:3'],
		resolutions: ['1K', '2K', '4K'],
		maxOutputSize: '2048x2048',
		pricePerImage: 0.03
	}
];

// ============================================================================
// Video Models — fal.ai
// ============================================================================

export const FAL_VIDEO_MODELS: VideoModel[] = [
	// Kling O3
	{
		id: 'fal-ai/kling-video/o3/pro/text-to-video',
		name: 'kling-o3-pro',
		displayName: 'Kling O3 Pro',
		provider: 'fal-ai',
		description: 'Newest Kling model, highest quality text-to-video',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: false },
		durations: [5, 10],
		maxDuration: 10,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.14
	},
	{
		id: 'fal-ai/kling-video/o3/standard/text-to-video',
		name: 'kling-o3-standard',
		displayName: 'Kling O3 Standard',
		provider: 'fal-ai',
		description: 'Kling O3 balanced quality/cost text-to-video',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: false },
		durations: [5, 10],
		maxDuration: 10,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.07
	},
	// Kling 3.0
	{
		id: 'fal-ai/kling-video/v3/pro/text-to-video',
		name: 'kling-v3-pro',
		displayName: 'Kling 3.0 Pro',
		provider: 'fal-ai',
		description: 'Kling 3.0 Pro quality text-to-video',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: false },
		durations: [5, 10],
		maxDuration: 10,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.10
	},
	{
		id: 'fal-ai/kling-video/v3/standard/text-to-video',
		name: 'kling-v3-standard',
		displayName: 'Kling 3.0 Standard',
		provider: 'fal-ai',
		description: 'Kling 3.0 Standard text-to-video',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: false },
		durations: [5, 10],
		maxDuration: 10,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.05
	},
	// Google Veo 3.1 (proxied via fal.ai)
	{
		id: 'fal-ai/veo3.1',
		name: 'veo-3-1',
		displayName: 'Veo 3.1',
		provider: 'fal-ai',
		description: 'Google Veo 3.1 via fal.ai — state-of-the-art with native audio',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: true },
		durations: [4, 6, 8],
		maxDuration: 8,
		aspectRatios: ['16:9', '9:16'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.40
	},
	{
		id: 'fal-ai/veo3.1/fast',
		name: 'veo-3-1-fast',
		displayName: 'Veo 3.1 Fast',
		provider: 'fal-ai',
		description: 'Speed-optimized Veo 3.1 via fal.ai',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: true },
		durations: [4, 6, 8],
		maxDuration: 8,
		aspectRatios: ['16:9', '9:16'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.15
	},
	// OpenAI Sora 2 (proxied via fal.ai)
	{
		id: 'fal-ai/sora-2/text-to-video',
		name: 'sora-2',
		displayName: 'Sora 2',
		provider: 'fal-ai',
		description: 'OpenAI Sora 2 via fal.ai — cinematic quality with audio',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: true },
		durations: [4, 8, 12, 20],
		maxDuration: 20,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.30
	},
	{
		id: 'fal-ai/sora-2/text-to-video/pro',
		name: 'sora-2-pro',
		displayName: 'Sora 2 Pro',
		provider: 'fal-ai',
		description: 'Sora 2 Pro quality tier via fal.ai',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: true },
		durations: [4, 8, 12, 20],
		maxDuration: 20,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.50
	},
	// xAI Grok Imagine Video (proxied via fal.ai)
	{
		id: 'xai/grok-imagine-video/text-to-video',
		name: 'grok-video',
		displayName: 'Grok Imagine Video',
		provider: 'fal-ai',
		description: 'xAI Grok via fal.ai — cinematic video generation with audio',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: true },
		durations: [4, 8],
		maxDuration: 8,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.06
	},
	// MiniMax Hailuo-02 (proxied via fal.ai)
	{
		id: 'fal-ai/minimax/hailuo-02/standard/text-to-video',
		name: 'hailuo-02',
		displayName: 'Hailuo-02',
		provider: 'fal-ai',
		description: 'MiniMax Hailuo-02 via fal.ai — advanced 768p video generation',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: false },
		durations: [6, 10],
		maxDuration: 10,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['768p'],
		pricePerSecond: 0.045
	},
	// PixVerse v5
	{
		id: 'fal-ai/pixverse/v5/text-to-video',
		name: 'pixverse-v5',
		displayName: 'PixVerse v5',
		provider: 'fal-ai',
		description: 'PixVerse v5 — stylized video with anime and multi-clip support',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: true },
		durations: [4, 8],
		maxDuration: 8,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.05
	},
	// Vidu Q3 Turbo
	{
		id: 'fal-ai/vidu/q3/text-to-video/turbo',
		name: 'vidu-q3-turbo',
		displayName: 'Vidu Q3 Turbo',
		provider: 'fal-ai',
		description: 'Budget-friendly fast video generation via fal.ai',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: false },
		durations: [4, 8],
		maxDuration: 8,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.035
	},
	// NVIDIA Cosmos Predict 2.5 (text-to-video)
	{
		id: 'fal-ai/cosmos-predict-2.5/distilled/text-to-video',
		name: 'cosmos-predict-2-5',
		displayName: 'Cosmos Predict 2.5',
		provider: 'fal-ai',
		description: 'NVIDIA Cosmos 2B distilled — fast world-model video generation',
		capabilities: { generation: true, imageToVideo: false, nativeAudio: false },
		durations: [6],
		maxDuration: 6,
		aspectRatios: ['16:9'],
		resolutions: ['720p'],
		pricePerSecond: 0.014
	},
	// Image-to-Video models
	{
		id: 'fal-ai/kling-video/o3/pro/image-to-video',
		name: 'kling-o3-pro-i2v',
		displayName: 'Kling O3 Pro (I2V)',
		provider: 'fal-ai',
		description: 'Kling O3 Pro image-to-video animation',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: false },
		durations: [5, 10],
		maxDuration: 10,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.14
	},
	{
		id: 'fal-ai/kling-video/o3/standard/image-to-video',
		name: 'kling-o3-standard-i2v',
		displayName: 'Kling O3 Standard (I2V)',
		provider: 'fal-ai',
		description: 'Kling O3 Standard image-to-video animation',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: false },
		durations: [5, 10],
		maxDuration: 10,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.07
	},
	{
		id: 'fal-ai/kling-video/v3/pro/image-to-video',
		name: 'kling-v3-pro-i2v',
		displayName: 'Kling 3.0 Pro (I2V)',
		provider: 'fal-ai',
		description: 'Kling 3.0 Pro image-to-video animation',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: false },
		durations: [5, 10],
		maxDuration: 10,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.10
	},
	{
		id: 'fal-ai/kling-video/v3/standard/image-to-video',
		name: 'kling-v3-standard-i2v',
		displayName: 'Kling 3.0 Standard (I2V)',
		provider: 'fal-ai',
		description: 'Kling 3.0 Standard image-to-video animation',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: false },
		durations: [5, 10],
		maxDuration: 10,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.05
	},
	{
		id: 'fal-ai/veo3.1/image-to-video',
		name: 'veo-3-1-i2v',
		displayName: 'Veo 3.1 (I2V)',
		provider: 'fal-ai',
		description: 'Google Veo 3.1 image-to-video via fal.ai',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: true },
		durations: [4, 6, 8],
		maxDuration: 8,
		aspectRatios: ['16:9', '9:16'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.40
	},
	{
		id: 'fal-ai/sora-2/image-to-video',
		name: 'sora-2-i2v',
		displayName: 'Sora 2 (I2V)',
		provider: 'fal-ai',
		description: 'OpenAI Sora 2 image-to-video via fal.ai',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: true },
		durations: [4, 8, 12, 20],
		maxDuration: 20,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.30
	},
	{
		id: 'fal-ai/sora-2/image-to-video/pro',
		name: 'sora-2-pro-i2v',
		displayName: 'Sora 2 Pro (I2V)',
		provider: 'fal-ai',
		description: 'OpenAI Sora 2 Pro image-to-video via fal.ai',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: true },
		durations: [4, 8, 12, 20],
		maxDuration: 20,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.50
	},
	{
		id: 'xai/grok-imagine-video/image-to-video',
		name: 'grok-video-i2v',
		displayName: 'Grok Imagine Video (I2V)',
		provider: 'fal-ai',
		description: 'xAI Grok image-to-video via fal.ai',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: true },
		durations: [4, 8],
		maxDuration: 8,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.06
	},
	{
		id: 'fal-ai/veo3.1/fast/image-to-video',
		name: 'veo-3-1-fast-i2v',
		displayName: 'Veo 3.1 Fast (I2V)',
		provider: 'fal-ai',
		description: 'Speed-optimized Veo 3.1 image-to-video via fal.ai',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: true },
		durations: [4, 6, 8],
		maxDuration: 8,
		aspectRatios: ['16:9', '9:16'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.15
	},
	{
		id: 'fal-ai/minimax/hailuo-02/standard/image-to-video',
		name: 'hailuo-02-i2v',
		displayName: 'Hailuo-02 (I2V)',
		provider: 'fal-ai',
		description: 'MiniMax Hailuo-02 image-to-video via fal.ai',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: false },
		durations: [6, 10],
		maxDuration: 10,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['768p'],
		pricePerSecond: 0.045
	},
	{
		id: 'fal-ai/pixverse/v5/image-to-video',
		name: 'pixverse-v5-i2v',
		displayName: 'PixVerse v5 (I2V)',
		provider: 'fal-ai',
		description: 'PixVerse v5 image-to-video with style support',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: true },
		durations: [4, 8],
		maxDuration: 8,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.05
	},
	{
		id: 'fal-ai/wan/v2.2-a14b/image-to-video',
		name: 'wan-2-2-i2v',
		displayName: 'Wan 2.2 (I2V)',
		provider: 'fal-ai',
		description: 'Alibaba Wan 2.2 open-source image-to-video via fal.ai',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: false },
		durations: [4, 8],
		maxDuration: 8,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.05
	},
	{
		id: 'fal-ai/vidu/q3/image-to-video/turbo',
		name: 'vidu-q3-turbo-i2v',
		displayName: 'Vidu Q3 Turbo (I2V)',
		provider: 'fal-ai',
		description: 'Budget-friendly fast image-to-video via fal.ai',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: false },
		durations: [4, 8],
		maxDuration: 8,
		aspectRatios: ['16:9', '9:16', '1:1'],
		resolutions: ['720p', '1080p'],
		pricePerSecond: 0.035
	},
	// NVIDIA Cosmos Predict 2.5 (image-to-video)
	{
		id: 'fal-ai/cosmos-predict-2.5/image-to-video',
		name: 'cosmos-predict-2-5-i2v',
		displayName: 'Cosmos Predict 2.5 (I2V)',
		provider: 'fal-ai',
		description: 'NVIDIA Cosmos 2B — world-model image-to-video generation',
		capabilities: { generation: false, imageToVideo: true, nativeAudio: false },
		durations: [6],
		maxDuration: 6,
		aspectRatios: ['16:9'],
		resolutions: ['720p'],
		pricePerSecond: 0.034
	}
];

// ============================================================================
// 3D Models — fal.ai
// ============================================================================

export const FAL_3D_MODELS: Model3DModel[] = [
	{
		id: 'fal-ai/meshy/text-to-3d',
		name: 'meshy-v6',
		displayName: 'Meshy v6',
		provider: 'fal-ai',
		description: 'Meshy v6 via fal.ai — best quality 3D generation',
		capabilities: { textTo3D: true, imageTo3D: true, pbr: true },
		artStyles: ['realistic', 'sculpture'],
		topologies: ['quad', 'triangle'],
		outputFormats: ['glb', 'fbx', 'obj', 'usdz'],
		maxPolycount: 300000,
		pricePerModel: 0.20
	},
	{
		id: 'fal-ai/hunyuan-3d/v2/turbo',
		name: 'hunyuan-3d-turbo',
		displayName: 'Hunyuan-3D v2 Turbo',
		provider: 'fal-ai',
		description: 'Fast open-source 3D generation via fal.ai',
		capabilities: { textTo3D: true, imageTo3D: true, pbr: false },
		artStyles: ['realistic'],
		topologies: ['triangle'],
		outputFormats: ['glb'],
		maxPolycount: 100000,
		pricePerModel: 0.05
	}
];

// ============================================================================
// TTS Voice Presets
// ============================================================================

const KOKORO_VOICES: TTSVoice[] = [
	{ id: 'af_heart', name: 'Heart', language: 'en-US', gender: 'female', description: 'Default warm voice' },
	{ id: 'af_alloy', name: 'Alloy', language: 'en-US', gender: 'female' },
	{ id: 'af_aoede', name: 'Aoede', language: 'en-US', gender: 'female' },
	{ id: 'af_bella', name: 'Bella', language: 'en-US', gender: 'female' },
	{ id: 'af_jessica', name: 'Jessica', language: 'en-US', gender: 'female' },
	{ id: 'af_kore', name: 'Kore', language: 'en-US', gender: 'female' },
	{ id: 'af_nicole', name: 'Nicole', language: 'en-US', gender: 'female' },
	{ id: 'af_nova', name: 'Nova', language: 'en-US', gender: 'female' },
	{ id: 'af_river', name: 'River', language: 'en-US', gender: 'female' },
	{ id: 'af_sarah', name: 'Sarah', language: 'en-US', gender: 'female' },
	{ id: 'af_sky', name: 'Sky', language: 'en-US', gender: 'female' },
	{ id: 'am_adam', name: 'Adam', language: 'en-US', gender: 'male' },
	{ id: 'am_echo', name: 'Echo', language: 'en-US', gender: 'male' },
	{ id: 'am_eric', name: 'Eric', language: 'en-US', gender: 'male' },
	{ id: 'am_liam', name: 'Liam', language: 'en-US', gender: 'male' },
	{ id: 'am_michael', name: 'Michael', language: 'en-US', gender: 'male' },
	{ id: 'am_onyx', name: 'Onyx', language: 'en-US', gender: 'male' },
	{ id: 'am_puck', name: 'Puck', language: 'en-US', gender: 'male' },
	{ id: 'am_santa', name: 'Santa', language: 'en-US', gender: 'male' }
];

const MINIMAX_VOICES: TTSVoice[] = [
	{ id: 'Wise_Woman', name: 'Wise Woman', language: 'en', gender: 'female', description: 'Default voice' },
	{ id: 'English_Trustworthy_Female', name: 'Trustworthy Female', language: 'en', gender: 'female' },
	{ id: 'English_Trustworthy_Male', name: 'Trustworthy Male', language: 'en', gender: 'male' },
	{ id: 'English_Gentle_Female', name: 'Gentle Female', language: 'en', gender: 'female' },
	{ id: 'English_Friendly_Male', name: 'Friendly Male', language: 'en', gender: 'male' },
	{ id: 'English_Lively_Female', name: 'Lively Female', language: 'en', gender: 'female' }
];

const ELEVENLABS_VOICES: TTSVoice[] = [
	{ id: 'Aria', name: 'Aria', language: 'en', gender: 'female' },
	{ id: 'Sarah', name: 'Sarah', language: 'en', gender: 'female' },
	{ id: 'Laura', name: 'Laura', language: 'en', gender: 'female' },
	{ id: 'Charlotte', name: 'Charlotte', language: 'en', gender: 'female' },
	{ id: 'Alice', name: 'Alice', language: 'en', gender: 'female' },
	{ id: 'Matilda', name: 'Matilda', language: 'en', gender: 'female' },
	{ id: 'Jessica', name: 'Jessica', language: 'en', gender: 'female' },
	{ id: 'Lily', name: 'Lily', language: 'en', gender: 'female' },
	{ id: 'Rachel', name: 'Rachel', language: 'en', gender: 'female', description: 'Default voice' },
	{ id: 'River', name: 'River', language: 'en', gender: 'female' },
	{ id: 'Roger', name: 'Roger', language: 'en', gender: 'male' },
	{ id: 'Charlie', name: 'Charlie', language: 'en', gender: 'male' },
	{ id: 'George', name: 'George', language: 'en', gender: 'male' },
	{ id: 'Callum', name: 'Callum', language: 'en', gender: 'male' },
	{ id: 'Liam', name: 'Liam', language: 'en', gender: 'male' },
	{ id: 'Will', name: 'Will', language: 'en', gender: 'male' },
	{ id: 'Eric', name: 'Eric', language: 'en', gender: 'male' },
	{ id: 'Chris', name: 'Chris', language: 'en', gender: 'male' },
	{ id: 'Brian', name: 'Brian', language: 'en', gender: 'male' },
	{ id: 'Daniel', name: 'Daniel', language: 'en', gender: 'male' },
	{ id: 'Bill', name: 'Bill', language: 'en', gender: 'male' }
];

const ORPHEUS_VOICES: TTSVoice[] = [
	{ id: 'tara', name: 'Tara', language: 'en', gender: 'female' },
	{ id: 'leah', name: 'Leah', language: 'en', gender: 'female' },
	{ id: 'jess', name: 'Jess', language: 'en', gender: 'female' },
	{ id: 'mia', name: 'Mia', language: 'en', gender: 'female' },
	{ id: 'zoe', name: 'Zoe', language: 'en', gender: 'female' },
	{ id: 'leo', name: 'Leo', language: 'en', gender: 'male' },
	{ id: 'dan', name: 'Dan', language: 'en', gender: 'male' },
	{ id: 'zac', name: 'Zac', language: 'en', gender: 'male' }
];

const VIBEVOICE_VOICES: TTSVoice[] = [
	{ id: 'Frank [EN]', name: 'Frank', language: 'en', gender: 'male', description: 'Default voice' },
	{ id: 'Carter [EN]', name: 'Carter', language: 'en', gender: 'male' },
	{ id: 'Luna [EN]', name: 'Luna', language: 'en', gender: 'female' },
	{ id: 'Sage [EN]', name: 'Sage', language: 'en', gender: 'neutral' }
];

// OpenAI voice set — shared across gpt-4o-mini-tts / tts-1 / tts-1-hd
const OPENAI_VOICES: TTSVoice[] = [
	{ id: 'alloy', name: 'Alloy', language: 'en', gender: 'neutral', description: 'Default voice' },
	{ id: 'ash', name: 'Ash', language: 'en', gender: 'male' },
	{ id: 'ballad', name: 'Ballad', language: 'en', gender: 'male' },
	{ id: 'coral', name: 'Coral', language: 'en', gender: 'female' },
	{ id: 'echo', name: 'Echo', language: 'en', gender: 'male' },
	{ id: 'fable', name: 'Fable', language: 'en', gender: 'neutral' },
	{ id: 'onyx', name: 'Onyx', language: 'en', gender: 'male' },
	{ id: 'nova', name: 'Nova', language: 'en', gender: 'female' },
	{ id: 'sage', name: 'Sage', language: 'en', gender: 'neutral' },
	{ id: 'shimmer', name: 'Shimmer', language: 'en', gender: 'female' },
	{ id: 'verse', name: 'Verse', language: 'en', gender: 'male' }
];

// ============================================================================
// TTS Models — fal.ai
// ============================================================================

export const FAL_TTS_MODELS: TTSModel[] = [
	{
		id: 'fal-ai/kokoro/american-english',
		name: 'kokoro',
		displayName: 'Kokoro',
		provider: 'fal-ai',
		description: 'Natural-sounding TTS with 19 voices, $0.02/1K chars',
		capabilities: {
			streaming: false,
			ssml: false,
			customVoices: false,
			steerability: false,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: KOKORO_VOICES,
		outputFormats: ['wav'],
		speedRange: { min: 0.5, max: 2.0 },
		maxInputLength: 10000,
		pricePerMillion: 20.0
	},
	{
		id: 'fal-ai/minimax/speech-2.8-hd',
		name: 'minimax-speech-2-8-hd',
		displayName: 'MiniMax Speech 2.8 HD',
		provider: 'fal-ai',
		description: 'High-definition multi-language TTS with emotion control',
		capabilities: {
			streaming: false,
			ssml: false,
			customVoices: false,
			steerability: true,
			multiSpeaker: false,
			emotionControl: true
		},
		voices: MINIMAX_VOICES,
		outputFormats: ['mp3', 'wav'],
		speedRange: { min: 0.5, max: 2.0 },
		maxInputLength: 10000,
		pricePerMillion: 50.0
	},
	{
		id: 'fal-ai/minimax/speech-02-hd',
		name: 'minimax-speech-02-hd',
		displayName: 'MiniMax Speech 02 HD',
		provider: 'fal-ai',
		description: 'Latest MiniMax HD speech synthesis',
		capabilities: {
			streaming: false,
			ssml: false,
			customVoices: false,
			steerability: true,
			multiSpeaker: false,
			emotionControl: true
		},
		voices: MINIMAX_VOICES,
		outputFormats: ['mp3', 'wav'],
		speedRange: { min: 0.5, max: 2.0 },
		maxInputLength: 10000,
		pricePerMillion: 50.0
	},
	{
		id: 'fal-ai/chatterbox/text-to-speech',
		name: 'chatterbox',
		displayName: 'Chatterbox',
		provider: 'fal-ai',
		description: 'Resemble AI Chatterbox — expressive, emotional TTS',
		capabilities: {
			streaming: false,
			ssml: false,
			customVoices: false,
			steerability: false,
			multiSpeaker: false,
			emotionControl: true
		},
		voices: [
			{ id: 'default', name: 'Default', language: 'en', gender: 'neutral' }
		],
		outputFormats: ['wav'],
		speedRange: { min: 0.5, max: 2.0 },
		maxInputLength: 5000,
		pricePerMillion: 30.0
	},
	{
		id: 'fal-ai/dia-tts/voice-clone',
		name: 'dia-tts',
		displayName: 'Dia TTS',
		provider: 'fal-ai',
		description: 'Nari Labs Dia — voice cloning and multi-speaker dialogue TTS',
		capabilities: {
			streaming: false,
			ssml: false,
			customVoices: true,
			steerability: false,
			multiSpeaker: true,
			emotionControl: false
		},
		voices: [
			{ id: 'default', name: 'Default', language: 'en', gender: 'neutral' }
		],
		outputFormats: ['wav'],
		speedRange: { min: 0.5, max: 2.0 },
		maxInputLength: 10000,
		pricePerMillion: 40.0
	},
	// ElevenLabs TTS (proxied via fal.ai)
	{
		id: 'fal-ai/elevenlabs/tts/eleven-v3',
		name: 'elevenlabs-v3',
		displayName: 'ElevenLabs v3',
		provider: 'fal-ai',
		description: 'ElevenLabs flagship v3 — 29+ languages, streaming, 21 voices',
		capabilities: {
			streaming: true,
			ssml: false,
			customVoices: false,
			steerability: true,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: ELEVENLABS_VOICES,
		outputFormats: ['mp3'],
		speedRange: { min: 0.7, max: 1.2 },
		maxInputLength: 5000,
		pricePerMillion: 100.0
	},
	{
		id: 'fal-ai/elevenlabs/tts/multilingual-v2',
		name: 'elevenlabs-multilingual-v2',
		displayName: 'ElevenLabs Multilingual v2',
		provider: 'fal-ai',
		description: 'ElevenLabs 29-language TTS — best accent accuracy and stability',
		capabilities: {
			streaming: true,
			ssml: false,
			customVoices: false,
			steerability: true,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: ELEVENLABS_VOICES,
		outputFormats: ['mp3'],
		speedRange: { min: 0.7, max: 1.2 },
		maxInputLength: 5000,
		pricePerMillion: 100.0
	},
	{
		id: 'fal-ai/elevenlabs/tts/turbo-v2.5',
		name: 'elevenlabs-turbo-v2-5',
		displayName: 'ElevenLabs Turbo v2.5',
		provider: 'fal-ai',
		description: 'ElevenLabs low-latency TTS — 32 languages, ideal for real-time apps',
		capabilities: {
			streaming: true,
			ssml: false,
			customVoices: false,
			steerability: true,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: ELEVENLABS_VOICES,
		outputFormats: ['mp3'],
		speedRange: { min: 0.7, max: 1.2 },
		maxInputLength: 5000,
		pricePerMillion: 50.0
	},
	{
		id: 'fal-ai/f5-tts',
		name: 'f5-tts',
		displayName: 'F5 TTS',
		provider: 'fal-ai',
		description: 'Zero-shot voice cloning from a single audio sample, multi-language',
		capabilities: {
			streaming: false,
			ssml: false,
			customVoices: true,
			steerability: false,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: [
			{ id: 'default', name: 'Default (clone from reference)', language: 'en', gender: 'neutral' }
		],
		outputFormats: ['wav'],
		speedRange: { min: 0.5, max: 2.0 },
		maxInputLength: 10000,
		pricePerMillion: 50.0
	},
	{
		id: 'fal-ai/orpheus-tts',
		name: 'orpheus-tts',
		displayName: 'Orpheus TTS',
		provider: 'fal-ai',
		description: 'Llama-based Speech-LLM — 8 emotive tags, inline expression markup',
		capabilities: {
			streaming: false,
			ssml: false,
			customVoices: false,
			steerability: false,
			multiSpeaker: false,
			emotionControl: true
		},
		voices: ORPHEUS_VOICES,
		outputFormats: ['wav'],
		speedRange: { min: 0.5, max: 2.0 },
		maxInputLength: 10000,
		pricePerMillion: 50.0
	},
	{
		id: 'fal-ai/vibevoice',
		name: 'vibevoice',
		displayName: 'VibeVoice 1.5B',
		provider: 'fal-ai',
		description: 'Microsoft VibeVoice — multi-speaker, long-form, expressive TTS',
		capabilities: {
			streaming: false,
			ssml: false,
			customVoices: false,
			steerability: false,
			multiSpeaker: true,
			emotionControl: false
		},
		voices: VIBEVOICE_VOICES,
		outputFormats: ['wav'],
		speedRange: { min: 0.5, max: 2.0 },
		maxInputLength: 10000,
		pricePerMillion: 40.0
	}
];

// ============================================================================
// TTS Models — OpenAI (GPT)
// ============================================================================

export const OPENAI_TTS_MODELS: TTSModel[] = [
	{
		id: 'openai/gpt-4o-mini-tts',
		name: 'gpt-4o-mini-tts',
		displayName: 'GPT-4o mini TTS',
		provider: 'openai',
		description: 'OpenAI steerable TTS — natural voices with instruction-guided delivery',
		capabilities: {
			streaming: true,
			ssml: false,
			customVoices: false,
			steerability: true,
			multiSpeaker: false,
			emotionControl: true
		},
		voices: OPENAI_VOICES,
		outputFormats: ['mp3', 'opus', 'aac', 'flac', 'wav', 'pcm'],
		speedRange: { min: 0.25, max: 4.0 },
		maxInputLength: 4096,
		pricePerMillion: 15.0
	},
	{
		id: 'openai/tts-1',
		name: 'tts-1',
		displayName: 'OpenAI TTS-1',
		provider: 'openai',
		description: 'OpenAI low-latency TTS optimized for real-time playback',
		capabilities: {
			streaming: true,
			ssml: false,
			customVoices: false,
			steerability: false,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: OPENAI_VOICES,
		outputFormats: ['mp3', 'opus', 'aac', 'flac', 'wav', 'pcm'],
		speedRange: { min: 0.25, max: 4.0 },
		maxInputLength: 4096,
		pricePerMillion: 15.0
	},
	{
		id: 'openai/tts-1-hd',
		name: 'tts-1-hd',
		displayName: 'OpenAI TTS-1 HD',
		provider: 'openai',
		description: 'OpenAI high-definition TTS for quality-first speech synthesis',
		capabilities: {
			streaming: true,
			ssml: false,
			customVoices: false,
			steerability: false,
			multiSpeaker: false,
			emotionControl: false
		},
		voices: OPENAI_VOICES,
		outputFormats: ['mp3', 'opus', 'aac', 'flac', 'wav', 'pcm'],
		speedRange: { min: 0.25, max: 4.0 },
		maxInputLength: 4096,
		pricePerMillion: 30.0
	}
];

// ============================================================================
// STT Models — fal.ai
// ============================================================================

export const FAL_STT_MODELS: STTModel[] = [
	{
		id: 'fal-ai/whisper',
		name: 'whisper-large-v3',
		displayName: 'Whisper Large v3',
		provider: 'fal-ai',
		description: 'OpenAI Whisper via fal.ai — transcription + translation + diarization',
		capabilities: {
			realtime: false,
			diarization: true,
			translation: true,
			timestamps: true,
			punctuation: true
		},
		inputFormats: ['wav', 'mp3', 'mp4', 'flac', 'ogg', 'webm', 'm4a'],
		languages: ['100+ languages'],
		maxFileSizeMB: 100,
		pricePerMinute: 0.005
	},
	// ElevenLabs Scribe (proxied via fal.ai)
	{
		id: 'fal-ai/elevenlabs/speech-to-text',
		name: 'elevenlabs-scribe-v1',
		displayName: 'ElevenLabs Scribe v1',
		provider: 'fal-ai',
		description: 'ElevenLabs Scribe v1 via fal.ai — 99 languages, diarization, audio event tagging',
		capabilities: {
			realtime: false,
			diarization: true,
			translation: false,
			timestamps: true,
			punctuation: true
		},
		inputFormats: ['mp3', 'ogg', 'wav', 'm4a', 'aac'],
		languages: ['99 languages'],
		maxFileSizeMB: 100,
		pricePerMinute: 0.03
	},
	{
		id: 'fal-ai/elevenlabs/speech-to-text/scribe-v2',
		name: 'elevenlabs-scribe-v2',
		displayName: 'ElevenLabs Scribe v2',
		provider: 'fal-ai',
		description: 'ElevenLabs Scribe v2 via fal.ai — improved accuracy, entity detection, keyterm biasing',
		capabilities: {
			realtime: false,
			diarization: true,
			translation: false,
			timestamps: true,
			punctuation: true
		},
		inputFormats: ['mp3', 'ogg', 'wav', 'm4a', 'aac'],
		languages: ['99 languages'],
		maxFileSizeMB: 100,
		pricePerMinute: 0.008
	},
	{
		id: 'fal-ai/speech-to-text',
		name: 'fal-speech-to-text',
		displayName: 'fal Speech-to-Text',
		provider: 'fal-ai',
		description: 'fal.ai native real-time STT with streaming and punctuation',
		capabilities: {
			realtime: true,
			diarization: false,
			translation: false,
			timestamps: true,
			punctuation: true
		},
		inputFormats: ['mp3', 'wav', 'webm', 'ogg', 'flac', 'm4a'],
		languages: ['multi-language'],
		maxFileSizeMB: 100,
		pricePerMinute: 0.048
	}
];

// ============================================================================
// STT Models — OpenAI (GPT)
// ============================================================================

export const OPENAI_STT_MODELS: STTModel[] = [
	{
		id: 'openai/gpt-4o-transcribe',
		name: 'gpt-4o-transcribe',
		displayName: 'GPT-4o Transcribe',
		provider: 'openai',
		description: 'OpenAI GPT-4o speech-to-text — high-accuracy transcription',
		capabilities: {
			realtime: true,
			diarization: false,
			translation: false,
			timestamps: false,
			punctuation: true
		},
		inputFormats: ['mp3', 'mp4', 'mpeg', 'mpga', 'm4a', 'wav', 'webm'],
		languages: ['99 languages'],
		maxFileSizeMB: 25,
		pricePerMinute: 0.006
	},
	{
		id: 'openai/gpt-4o-mini-transcribe',
		name: 'gpt-4o-mini-transcribe',
		displayName: 'GPT-4o mini Transcribe',
		provider: 'openai',
		description: 'OpenAI GPT-4o mini speech-to-text — fast, affordable transcription',
		capabilities: {
			realtime: true,
			diarization: false,
			translation: false,
			timestamps: false,
			punctuation: true
		},
		inputFormats: ['mp3', 'mp4', 'mpeg', 'mpga', 'm4a', 'wav', 'webm'],
		languages: ['99 languages'],
		maxFileSizeMB: 25,
		pricePerMinute: 0.003
	},
	{
		id: 'openai/whisper-1',
		name: 'whisper-1',
		displayName: 'OpenAI Whisper',
		provider: 'openai',
		description: 'OpenAI Whisper — 99+ language transcription and translation',
		capabilities: {
			realtime: false,
			diarization: false,
			translation: true,
			timestamps: true,
			punctuation: true
		},
		inputFormats: ['mp3', 'mp4', 'mpeg', 'mpga', 'm4a', 'wav', 'webm'],
		languages: ['99 languages'],
		maxFileSizeMB: 25,
		pricePerMinute: 0.006
	}
];

// ============================================================================
// Combined Model Collections
// ============================================================================

export const ALL_IMAGE_MODELS: ImageModel[] = [...FAL_IMAGE_MODELS];
export const ALL_VIDEO_MODELS: VideoModel[] = [...FAL_VIDEO_MODELS];
export const ALL_3D_MODELS: Model3DModel[] = [...FAL_3D_MODELS];
export const ALL_TTS_MODELS: TTSModel[] = [...FAL_TTS_MODELS, ...OPENAI_TTS_MODELS];
export const ALL_STT_MODELS: STTModel[] = [...FAL_STT_MODELS, ...OPENAI_STT_MODELS];

// Convenience subsets — Image
export const TEXT_TO_IMAGE_MODELS = FAL_IMAGE_MODELS.filter(m => m.capabilities.generation);
export const IMAGE_EDITING_MODELS = FAL_IMAGE_MODELS.filter(m => m.capabilities.editing);

// Convenience subsets — Video
export const TEXT_TO_VIDEO_MODELS = FAL_VIDEO_MODELS.filter(m => m.capabilities.generation);
export const IMAGE_TO_VIDEO_MODELS = FAL_VIDEO_MODELS.filter(m => m.capabilities.imageToVideo);

// Convenience subsets — 3D
export const TEXT_TO_3D_MODELS = FAL_3D_MODELS.filter(m => m.capabilities.textTo3D);
export const IMAGE_TO_3D_MODELS = FAL_3D_MODELS.filter(m => m.capabilities.imageTo3D);

// ============================================================================
// Style Presets (provider-agnostic)
// ============================================================================

export const IMAGE_STYLE_PRESETS: ImageStylePreset[] = [
	{ id: 'none', name: 'None' },
	{ id: 'photographic', name: 'Photographic', description: 'Realistic photography' },
	{ id: 'digital-art', name: 'Digital Art', description: 'Modern digital illustration' },
	{ id: 'anime', name: 'Anime', description: 'Japanese anime style' },
	{ id: 'watercolor', name: 'Watercolor', description: 'Soft watercolor painting' },
	{ id: 'oil-painting', name: 'Oil Painting', description: 'Classical oil painting' },
	{ id: 'sketch', name: 'Sketch', description: 'Pencil or charcoal sketch' },
	{ id: 'cinematic', name: 'Cinematic', description: 'Film-like dramatic lighting' },
	{ id: 'pixel-art', name: 'Pixel Art', description: 'Retro pixel graphics' },
	{ id: '3d-render', name: '3D Render', description: 'Photorealistic 3D rendering' }
];

// ============================================================================
// Aspect Ratios (provider-agnostic)
// ============================================================================

export const IMAGE_ASPECT_RATIOS: ImageAspectRatio[] = [
	{ value: '1:1', label: 'Square', width: 40, height: 40 },
	{ value: '16:9', label: 'Landscape', width: 48, height: 27 },
	{ value: '9:16', label: 'Portrait', width: 27, height: 48 },
	{ value: '4:3', label: 'Standard', width: 44, height: 33 },
	{ value: '3:4', label: 'Portrait 3:4', width: 33, height: 44 },
	{ value: '3:2', label: 'Photo', width: 45, height: 30 },
	{ value: '2:3', label: 'Photo Portrait', width: 30, height: 45 },
	{ value: '21:9', label: 'Ultrawide', width: 52, height: 22 },
	{ value: '5:4', label: 'Classic', width: 42, height: 34 },
	{ value: '4:5', label: 'Instagram', width: 34, height: 42 }
];

export const VIDEO_ASPECT_RATIOS: VideoAspectRatio[] = [
	{ value: '16:9', label: 'Landscape', width: 48, height: 27 },
	{ value: '9:16', label: 'Portrait', width: 27, height: 48 },
	{ value: '1:1', label: 'Square', width: 40, height: 40 }
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

export function get3DModel(id: string): Model3DModel | undefined {
	return ALL_3D_MODELS.find(m => m.id === id);
}

export function getTTSModel(id: string): TTSModel | undefined {
	return ALL_TTS_MODELS.find(m => m.id === id);
}

export function getSTTModel(id: string): STTModel | undefined {
	return ALL_STT_MODELS.find(m => m.id === id);
}

export function getAspectRatiosForModel(modelId: string): ImageAspectRatio[] {
	const model = getImageModel(modelId);
	if (!model) return IMAGE_ASPECT_RATIOS;
	return IMAGE_ASPECT_RATIOS.filter(ar => model.aspectRatios.includes(ar.value));
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
	'fal-ai': 'fal.ai'
};

// ============================================================================
// Dynamic Catalog Display Data
// ============================================================================

/** Human-readable labels for fal.ai categories */
export const CATEGORY_LABELS: Record<string, string> = {
	'text-to-image': 'Text \u2192 Image',
	'image-to-image': 'Image \u2192 Image',
	'text-to-video': 'Text \u2192 Video',
	'image-to-video': 'Image \u2192 Video',
	'video-to-video': 'Video \u2192 Video',
	'audio-to-video': 'Audio \u2192 Video',
	'text-to-3d': 'Text \u2192 3D',
	'image-to-3d': 'Image \u2192 3D',
	'3d-to-3d': '3D \u2192 3D',
	'text-to-speech': 'Text \u2192 Speech',
	'audio-to-audio': 'Audio \u2192 Audio',
};

/** Maps tool_type to a display-friendly icon */
export const OUTPUT_TYPE_ICONS: Record<string, string> = {
	'image': '\uD83D\uDDBC\uFE0F',
	'video': '\uD83C\uDFAC',
	'3d': '\uD83E\uDDCA',
	'audio': '\uD83D\uDD0A',
};

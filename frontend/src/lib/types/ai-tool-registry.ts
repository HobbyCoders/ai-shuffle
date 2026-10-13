/**
 * AI Tool Registry — Declarative configuration for all AI tools
 *
 * This registry defines the complete structure of AI tool categories,
 * their sub-tools, available models, and configurable settings.
 * It eliminates the repetitive per-tool state management by providing
 * a single source of truth that drives the settings UI.
 */

import {
	TEXT_TO_IMAGE_MODELS,
	IMAGE_EDITING_MODELS,
	TEXT_TO_VIDEO_MODELS,
	IMAGE_TO_VIDEO_MODELS,
	TEXT_TO_3D_MODELS,
	IMAGE_TO_3D_MODELS,
	ALL_TTS_MODELS,
	ALL_STT_MODELS,
	type ImageModel,
	type VideoModel,
	type Model3DModel,
	type TTSModel,
	type STTModel,
} from './ai-models';

// ============================================================================
// Core Types
// ============================================================================

/** Defines a configurable setting for a tool (aspect ratio, resolution, etc.) */
export interface ToolSetting {
	key: string;
	label: string;
	type: 'pills' | 'select' | 'slider';
	/** For pills/select: function to get options from the current model */
	getOptions?: (model: AnyModel) => string[];
	/** For slider: range configuration */
	sliderConfig?: {
		min: number;
		max: number;
		step: number;
		format: (v: number) => string;
	};
	/** Backend setting key for persistence */
	settingKey: string;
}

/** Defines a single tool within a category (e.g., "Text to Image" within "Image") */
export interface ToolDefinition {
	id: string;
	label: string;
	description: string;
	/** The backend settings key for model selection */
	modelSettingKey: string;
	/** The API endpoint for updating the model */
	modelEndpoint: string;
	/** The API endpoint for updating tool settings */
	settingsEndpoint?: string;
	/** Default model ID if none configured */
	defaultModel: string;
	/** Function returning available models for this tool */
	getModels: () => AnyModel[];
	/** Configurable settings for this tool */
	settings: ToolSetting[];
	/** Which capability badges to show */
	getCapabilities?: (model: AnyModel) => CapabilityBadge[];
	/** Price formatting */
	formatPrice: (model: AnyModel) => string;
	/** The catalog tool_type for linking to the model browser */
	catalogToolType: string;
}

/** A category groups related tools (e.g., "Image" contains text-to-image and image-editing) */
export interface ToolCategory {
	id: string;
	label: string;
	icon: string;
	description: string;
	/** Accent color class for the category */
	accent: 'blue' | 'purple' | 'green' | 'amber' | 'rose';
	tools: ToolDefinition[];
}

/** Capability badge displayed on a model */
export interface CapabilityBadge {
	label: string;
	color: 'green' | 'blue' | 'purple' | 'amber';
}

/** Union type for all model types */
export type AnyModel = ImageModel | VideoModel | Model3DModel | TTSModel | STTModel;

// ============================================================================
// Helper: Extract capabilities from models
// ============================================================================

function imageCapabilities(model: AnyModel): CapabilityBadge[] {
	const m = model as ImageModel;
	const badges: CapabilityBadge[] = [];
	if (m.capabilities?.editing) badges.push({ label: 'Editing', color: 'blue' });
	if (m.capabilities?.referenceImages) {
		badges.push({ label: `Ref Images (${m.capabilities.maxReferenceImages})`, color: 'blue' });
	}
	if (m.capabilities?.textRendering && m.capabilities.textRendering !== 'none') {
		badges.push({ label: `Text: ${m.capabilities.textRendering}`, color: 'purple' });
	}
	if (m.capabilities?.inpainting) badges.push({ label: 'Inpainting', color: 'green' });
	return badges;
}

function videoCapabilities(model: AnyModel): CapabilityBadge[] {
	const m = model as VideoModel;
	const badges: CapabilityBadge[] = [];
	if (m.capabilities?.nativeAudio) badges.push({ label: 'Native Audio', color: 'green' });
	return badges;
}

function model3dCapabilities(model: AnyModel): CapabilityBadge[] {
	const m = model as Model3DModel;
	const badges: CapabilityBadge[] = [];
	if (m.capabilities?.pbr) badges.push({ label: 'PBR Materials', color: 'green' });
	if (m.outputFormats?.length) {
		badges.push({ label: `Formats: ${m.outputFormats.join(', ')}`, color: 'blue' });
	}
	return badges;
}

function ttsCapabilities(model: AnyModel): CapabilityBadge[] {
	const m = model as TTSModel;
	const badges: CapabilityBadge[] = [];
	if (m.capabilities?.streaming) badges.push({ label: 'Streaming', color: 'green' });
	if (m.capabilities?.emotionControl) badges.push({ label: 'Emotions', color: 'purple' });
	if (m.capabilities?.multiSpeaker) badges.push({ label: 'Multi-Speaker', color: 'blue' });
	return badges;
}

function sttCapabilities(model: AnyModel): CapabilityBadge[] {
	const m = model as STTModel;
	const badges: CapabilityBadge[] = [];
	if (m.capabilities?.realtime) badges.push({ label: 'Realtime', color: 'green' });
	if (m.capabilities?.diarization) badges.push({ label: 'Diarization', color: 'blue' });
	if (m.capabilities?.translation) badges.push({ label: 'Translation', color: 'purple' });
	return badges;
}

// ============================================================================
// The Registry — Single source of truth
// ============================================================================

/** Per-image price; the ChatGPT subscription is covered by the plan. */
function formatImagePrice(m: AnyModel): string {
	const image = m as ImageModel;
	return image.provider === 'chatgpt' ? 'ChatGPT plan' : `$${image.pricePerImage.toFixed(3)}/img`;
}

export const AI_TOOL_CATEGORIES: ToolCategory[] = [
	{
		id: 'image',
		label: 'Image',
		icon: 'image',
		description: 'Generate and edit images with AI',
		accent: 'blue',
		tools: [
			{
				id: 'image_generation',
				label: 'Text to Image',
				description: 'Generate images from text descriptions',
				modelSettingKey: 'image_model',
				modelEndpoint: '/settings/integrations/image/model',
				settingsEndpoint: '/settings/integrations/image/settings',
				defaultModel: 'fal-ai/flux-pro/v1.1-ultra',
				getModels: () => TEXT_TO_IMAGE_MODELS,
				catalogToolType: 'image_generation',
				formatPrice: formatImagePrice,
				getCapabilities: imageCapabilities,
				settings: [
					{
						key: 'aspect_ratio',
						label: 'Aspect Ratio',
						type: 'pills',
						getOptions: (m) => (m as ImageModel).aspectRatios,
						settingKey: 'image_aspect_ratio',
					},
					{
						key: 'resolution',
						label: 'Resolution',
						type: 'pills',
						getOptions: (m) => (m as ImageModel).resolutions,
						settingKey: 'image_resolution',
					},
				],
			},
			{
				id: 'image_editing',
				label: 'Image Editing',
				description: 'Edit, transform, and enhance existing images',
				modelSettingKey: 'image_edit_model',
				modelEndpoint: '/settings/integrations/image/edit-model',
				defaultModel: 'fal-ai/flux-pro/kontext',
				getModels: () => IMAGE_EDITING_MODELS,
				catalogToolType: 'image_editing',
				formatPrice: formatImagePrice,
				getCapabilities: imageCapabilities,
				settings: [],
			},
		],
	},
	{
		id: 'video',
		label: 'Video',
		icon: 'video',
		description: 'Generate and animate videos with AI',
		accent: 'purple',
		tools: [
			{
				id: 'video_generation',
				label: 'Text to Video',
				description: 'Generate videos from text descriptions',
				modelSettingKey: 'video_model',
				modelEndpoint: '/settings/integrations/video/model',
				settingsEndpoint: '/settings/integrations/video/settings',
				defaultModel: 'fal-ai/kling-video/o3/pro/text-to-video',
				getModels: () => TEXT_TO_VIDEO_MODELS,
				catalogToolType: 'video_generation',
				formatPrice: (m) => `$${(m as VideoModel).pricePerSecond.toFixed(3)}/sec`,
				getCapabilities: videoCapabilities,
				settings: [
					{
						key: 'aspect_ratio',
						label: 'Aspect Ratio',
						type: 'pills',
						getOptions: (m) => (m as VideoModel).aspectRatios,
						settingKey: 'video_aspect_ratio',
					},
					{
						key: 'duration',
						label: 'Duration',
						type: 'pills',
						getOptions: (m) => (m as VideoModel).durations.map(String),
						settingKey: 'video_duration',
					},
					{
						key: 'resolution',
						label: 'Resolution',
						type: 'pills',
						getOptions: (m) => (m as VideoModel).resolutions,
						settingKey: 'video_resolution',
					},
				],
			},
			{
				id: 'image_to_video',
				label: 'Image to Video',
				description: 'Animate still images into video',
				modelSettingKey: 'i2v_model',
				modelEndpoint: '/settings/integrations/video/i2v-model',
				defaultModel: 'fal-ai/kling-video/o3/pro/image-to-video',
				getModels: () => IMAGE_TO_VIDEO_MODELS,
				catalogToolType: 'image_to_video',
				formatPrice: (m) => `$${(m as VideoModel).pricePerSecond.toFixed(3)}/sec`,
				getCapabilities: videoCapabilities,
				settings: [],
			},
		],
	},
	{
		id: '3d',
		label: '3D',
		icon: '3d',
		description: 'Generate 3D models and objects',
		accent: 'green',
		tools: [
			{
				id: 'text_to_3d',
				label: 'Text to 3D',
				description: 'Generate 3D models from text descriptions',
				modelSettingKey: 'model3d_model',
				modelEndpoint: '/settings/integrations/3d/model',
				defaultModel: 'fal-ai/meshy/text-to-3d',
				getModels: () => TEXT_TO_3D_MODELS,
				catalogToolType: 'text_to_3d',
				formatPrice: (m) => `$${(m as Model3DModel).pricePerModel.toFixed(2)}/model`,
				getCapabilities: model3dCapabilities,
				settings: [],
			},
			{
				id: 'image_to_3d',
				label: 'Image to 3D',
				description: 'Generate 3D models from reference images',
				modelSettingKey: 'image_to_3d_model',
				modelEndpoint: '/settings/integrations/3d/i2-3d-model',
				defaultModel: 'fal-ai/hunyuan-3d/v2/turbo',
				getModels: () => IMAGE_TO_3D_MODELS,
				catalogToolType: 'image_to_3d',
				formatPrice: (m) => `$${(m as Model3DModel).pricePerModel.toFixed(2)}/model`,
				getCapabilities: model3dCapabilities,
				settings: [],
			},
		],
	},
	{
		id: 'audio',
		label: 'Audio',
		icon: 'audio',
		description: 'Text-to-speech and speech-to-text',
		accent: 'amber',
		tools: [
			{
				id: 'tts',
				label: 'Text to Speech',
				description: 'Convert text to natural-sounding speech',
				modelSettingKey: 'tts_model',
				modelEndpoint: '/settings/integrations/audio/tts',
				settingsEndpoint: '/settings/integrations/tts',
				defaultModel: 'fal-ai/kokoro/american-english',
				getModels: () => ALL_TTS_MODELS,
				catalogToolType: 'tts',
				formatPrice: (m) => {
					const costPer1k = (m as TTSModel).pricePerMillion / 1000;
					return `$${costPer1k.toFixed(2)}/1K chars`;
				},
				getCapabilities: ttsCapabilities,
				settings: [
					{
						key: 'voice',
						label: 'Voice',
						type: 'select',
						getOptions: (m) => (m as TTSModel).voices.map(v => v.id),
						settingKey: 'tts_voice',
					},
					{
						key: 'speed',
						label: 'Speed',
						type: 'slider',
						sliderConfig: {
							min: 0.5,
							max: 2.0,
							step: 0.05,
							format: (v: number) => `${v.toFixed(2)}x`,
						},
						settingKey: 'tts_speed',
					},
					{
						key: 'output_format',
						label: 'Output Format',
						type: 'select',
						getOptions: (m) => (m as TTSModel).outputFormats,
						settingKey: 'tts_output_format',
					},
				],
			},
			{
				id: 'stt',
				label: 'Speech to Text',
				description: 'Transcribe audio to text',
				modelSettingKey: 'stt_model',
				modelEndpoint: '/settings/integrations/audio/stt',
				defaultModel: 'fal-ai/whisper',
				getModels: () => ALL_STT_MODELS,
				catalogToolType: 'stt',
				formatPrice: (m) => `$${(m as STTModel).pricePerMinute.toFixed(3)}/min`,
				getCapabilities: sttCapabilities,
				settings: [],
			},
		],
	},
	{
		id: 'audio_gen',
		label: 'Audio Generation',
		icon: 'audio',
		description: 'Generate music & sound, and transform audio',
		accent: 'rose',
		tools: [
			{
				id: 'text_to_audio',
				label: 'Text to Audio',
				description: 'Generate music and sound effects from a text prompt',
				modelSettingKey: 'audio_model',
				modelEndpoint: '/settings/integrations/audio/generation-model',
				defaultModel: 'fal-ai/lyria2',
				// Gallery-only: pick any fal text-to-audio model; params come from
				// the model's OpenAPI schema (no curated list to maintain).
				getModels: () => [],
				catalogToolType: 'audio_generation',
				formatPrice: () => '',
				settings: [],
			},
			{
				id: 'audio_to_audio',
				label: 'Audio to Audio',
				description: 'Transform audio — remix, style transfer, restoration, stems',
				modelSettingKey: 'audio_to_audio_model',
				modelEndpoint: '/settings/integrations/audio/audio-to-audio-model',
				defaultModel: '',
				getModels: () => [],
				catalogToolType: 'audio_to_audio',
				formatPrice: () => '',
				settings: [],
			},
		],
	},
];

// ============================================================================
// Utility functions
// ============================================================================

/** Get a flat list of all tool definitions */
function getAllTools(): ToolDefinition[] {
	return AI_TOOL_CATEGORIES.flatMap(c => c.tools);
}

/** Build the default model selections map from registry defaults */
export function getDefaultModelSelections(): Record<string, string> {
	const defaults: Record<string, string> = {};
	for (const tool of getAllTools()) {
		defaults[tool.modelSettingKey] = tool.defaultModel;
	}
	return defaults;
}

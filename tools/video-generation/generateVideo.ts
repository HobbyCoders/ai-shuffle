/**
 * Video Generation Tool - Unified Provider Interface
 *
 * Generate videos using AI from multiple providers (Google Veo, OpenAI Sora, etc.)
 * The provider and model are selected based on environment variables or explicit input.
 * Videos are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   VIDEO_PROVIDER - Provider ID (e.g., "google-veo", "openai-sora")
 *   VIDEO_API_KEY - API key for the selected provider
 *   VIDEO_MODEL - Model to use (e.g., "veo-3.1-fast-generate-preview", "sora-2")
 *
 *   Legacy (backwards compatible):
 *   GEMINI_API_KEY - Google AI API key (used if VIDEO_API_KEY not set)
 *   VEO_MODEL - Veo model (used if VIDEO_MODEL not set for Google provider)
 *   OPENAI_API_KEY - OpenAI API key (used for Sora if VIDEO_API_KEY not set)
 *
 * Usage:
 *   import { generateVideo } from '/opt/ai-tools/dist/video-generation/generateVideo.js';
 *
 *   // Use default provider from settings
 *   const result = await generateVideo({
 *     prompt: 'A cat playing with a ball of yarn'
 *   });
 *
 *   // Or explicitly specify provider/model
 *   const result = await generateVideo({
 *     prompt: 'A cat playing with a ball of yarn',
 *     provider: 'openai-sora',
 *     model: 'sora-2-pro'
 *   });
 *
 *   if (result.success) {
 *     // result.video_url contains the URL to access the video
 *     // result.file_path contains the local file path
 *   }
 */

import { registry } from '../providers/registry.js';
import { VideoResult, ProviderCredentials } from '../providers/types.js';

// Re-export VideoResponse for backwards compatibility
export { VideoResponse } from './shared.js';

export interface GenerateVideoInput {
  /**
   * The text prompt describing the video to generate.
   * Be descriptive! Include action, scene, style, mood details.
   * Max ~1,024 tokens (approximately 4,000 characters).
   *
   * @example "A golden retriever running through a field of sunflowers at sunset"
   * @example "Aerial drone shot of a city skyline transitioning from day to night"
   */
  prompt: string;

  /**
   * Override the default video provider.
   * Available: "google-veo", "openai-sora"
   * If not specified, uses VIDEO_PROVIDER env var or defaults to "google-veo"
   */
  provider?: string;

  /**
   * Override the default model for the selected provider.
   * Google Veo: "veo-3.1-fast-generate-preview", "veo-3.1-generate-preview"
   * OpenAI Sora: "sora-2", "sora-2-pro"
   * If not specified, uses VIDEO_MODEL/VEO_MODEL env var or provider default
   */
  model?: string;

  /**
   * Aspect ratio of the generated video.
   * @default "16:9"
   */
  aspect_ratio?: '16:9' | '9:16' | '1:1';

  /**
   * Duration of the generated video in seconds.
   * Google Veo: 4, 6, or 8 seconds
   * OpenAI Sora: 4, 8, or 12 seconds
   * @default 8
   */
  duration?: number;

  /**
   * Resolution of the generated video.
   * Note: Higher resolutions may have duration restrictions.
   * @default "720p"
   */
  resolution?: '720p' | '1080p';

  /**
   * Elements to exclude from the video generation.
   * @example "blurry, low quality, text, watermark"
   */
  negative_prompt?: string;

  /**
   * Seed for reproducibility. Same seed + same inputs may produce similar results.
   * Note: Not guaranteed to be deterministic.
   */
  seed?: number;

  /**
   * Control generation of people in the video.
   * @default "allow_adult"
   */
  person_generation?: 'allow_all' | 'allow_adult';
}

export type GenerateVideoResponse = VideoResult;

/**
 * Determine the provider ID to use based on input and environment
 */
function getProviderId(inputProvider?: string): string {
  // Explicit input takes priority
  if (inputProvider) {
    return inputProvider;
  }

  // Check environment variables
  if (process.env.VIDEO_PROVIDER) {
    return process.env.VIDEO_PROVIDER;
  }

  // Legacy: If OPENAI_API_KEY is set but not GEMINI_API_KEY, default to Sora
  if (process.env.OPENAI_API_KEY && !process.env.GEMINI_API_KEY) {
    return 'openai-sora';
  }

  // Default to Google Veo
  return 'google-veo';
}

/**
 * Get API key for the specified provider
 */
function getApiKey(providerId: string): string {
  // Check provider-specific env var first
  if (process.env.VIDEO_API_KEY) {
    return process.env.VIDEO_API_KEY;
  }

  // Legacy fallbacks based on provider
  if (providerId === 'google-veo') {
    return process.env.GEMINI_API_KEY || '';
  }

  if (providerId === 'openai-sora') {
    return process.env.OPENAI_API_KEY || '';
  }

  return '';
}

/**
 * Get model ID for the specified provider
 */
function getModelId(providerId: string, inputModel?: string): string {
  // Explicit input takes priority
  if (inputModel) {
    return inputModel;
  }

  // Check environment variables
  if (process.env.VIDEO_MODEL) {
    return process.env.VIDEO_MODEL;
  }

  // Legacy fallbacks
  if (providerId === 'google-veo' && process.env.VEO_MODEL) {
    return process.env.VEO_MODEL;
  }

  // Provider defaults
  const provider = registry.getVideoProvider(providerId);
  if (provider && provider.models.length > 0) {
    return provider.models[0].id;
  }

  // Ultimate fallbacks
  if (providerId === 'google-veo') {
    return 'veo-3.1-fast-generate-preview';
  }

  if (providerId === 'openai-sora') {
    return 'sora-2';
  }

  return '';
}

/**
 * Generate a video from a text prompt.
 *
 * Uses the configured provider (Google Veo, OpenAI Sora, etc.) or allows
 * explicit override via the provider/model parameters.
 *
 * The video is saved to disk and a URL is returned for display in the chat UI.
 * Video generation is asynchronous - this function polls until complete.
 *
 * @param input - The generation parameters
 * @returns The generated video URL and file path, or error details
 *
 * @example
 * ```typescript
 * // Use default provider
 * const result = await generateVideo({
 *   prompt: 'A butterfly landing on a flower, macro shot, cinematic',
 *   duration: 8,
 *   aspect_ratio: '16:9'
 * });
 *
 * // Use specific provider
 * const result = await generateVideo({
 *   prompt: 'A butterfly landing on a flower',
 *   provider: 'openai-sora',
 *   model: 'sora-2-pro'
 * });
 *
 * if (result.success) {
 *   console.log('Video URL:', result.video_url);
 *   console.log('Saved to:', result.file_path);
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export async function generateVideo(input: GenerateVideoInput): Promise<GenerateVideoResponse> {
  // Determine provider
  const providerId = getProviderId(input.provider);
  const provider = registry.getVideoProvider(providerId);

  if (!provider) {
    const availableProviders = registry.listVideoProviders().map(p => p.id);
    return {
      success: false,
      error: `Video provider '${providerId}' not found. Available providers: ${availableProviders.join(', ')}`
    };
  }

  // Get API key
  const apiKey = getApiKey(providerId);
  if (!apiKey) {
    return {
      success: false,
      error: `No API key configured for ${provider.name}. Set VIDEO_API_KEY or provider-specific key.`
    };
  }

  // Get model
  const modelId = getModelId(providerId, input.model);
  const modelInfo = provider.models.find(m => m.id === modelId);

  if (!modelInfo) {
    const availableModels = provider.models.map(m => m.id);
    return {
      success: false,
      error: `Model '${modelId}' not found for ${provider.name}. Available models: ${availableModels.join(', ')}`
    };
  }

  // Build credentials
  const credentials: ProviderCredentials = { apiKey };

  // Delegate to provider
  const result = await provider.generate(
    {
      prompt: input.prompt,
      aspect_ratio: input.aspect_ratio,
      duration: input.duration,
      resolution: input.resolution,
      negative_prompt: input.negative_prompt,
      seed: input.seed,
      person_generation: input.person_generation
    },
    credentials,
    modelId
  );

  // Add provider metadata to the result
  if (result.success) {
    const capabilities = modelInfo.capabilities.map(c => c.toString());
    const supportsExtend = capabilities.includes('video-extend');

    result.provider_metadata = {
      ...result.provider_metadata,
      provider_used: providerId,
      model_used: modelId,
      capabilities: capabilities,
      can_extend: supportsExtend,
      extend_with: supportsExtend ? providerId : 'google-veo (Veo only supports extend)'
    };
  }

  return result;
}

export default generateVideo;

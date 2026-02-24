/**
 * Video Generation Tool - fal.ai Provider
 *
 * Generate videos using AI via fal.ai (Kling O3, Veo 3.1, Sora 2, etc.)
 * Videos are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   FAL_KEY - fal.ai API key
 *   VIDEO_MODEL - Model to use (e.g., "fal-ai/kling-video/v2.1/master/text-to-video")
 *
 * Usage:
 *   import { generateVideo } from '/opt/ai-tools/dist/video-generation/generateVideo.js';
 *
 *   const result = await generateVideo({
 *     prompt: 'A cat playing with a ball of yarn'
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
   * Currently only "fal-ai" is supported.
   */
  provider?: string;

  /**
   * Override the default model.
   * fal.ai models include Kling, Veo, Sora variants.
   * If not specified, uses VIDEO_MODEL env var or first registered fal.ai model.
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
 * Determine the provider ID — always fal.ai
 */
function getProviderId(_inputProvider?: string): string {
  return 'fal-ai';
}

/**
 * Get API key from FAL_KEY environment variable
 */
function getApiKey(_providerId: string): string {
  return process.env.FAL_KEY || '';
}

/**
 * Get model ID for the provider
 */
function getModelId(providerId: string, inputModel?: string): string {
  if (inputModel) {
    return inputModel;
  }

  if (process.env.VIDEO_MODEL) {
    return process.env.VIDEO_MODEL;
  }

  // Provider defaults from registry
  const provider = registry.getVideoProvider(providerId);
  if (provider && provider.models.length > 0) {
    return provider.models[0].id;
  }

  return '';
}

/**
 * Generate a video from a text prompt.
 *
 * Uses fal.ai as the video generation provider. Supports Kling, Veo, and
 * Sora models through a single API key.
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
 * // Use specific model
 * const result = await generateVideo({
 *   prompt: 'A butterfly landing on a flower',
 *   model: 'fal-ai/kling-video/v2.1/master/text-to-video'
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
      error: `No API key configured for ${provider.name}. Set FAL_KEY environment variable.`
    };
  }

  // Get model
  const modelId = getModelId(providerId, input.model);
  const modelInfo = provider.models.find(m => m.id === modelId);

  // Build credentials
  const credentials: ProviderCredentials = { apiKey };

  // Apply env var defaults when caller doesn't specify
  const aspect_ratio = input.aspect_ratio || (process.env.VIDEO_ASPECT_RATIO as any) || undefined;
  const duration = input.duration || (process.env.VIDEO_DURATION ? parseInt(process.env.VIDEO_DURATION) : undefined);
  const resolution = input.resolution || (process.env.VIDEO_RESOLUTION as any) || undefined;

  // Delegate to provider — works even with models not in the hardcoded list
  // since fal.subscribe() accepts any valid endpoint_id
  const result = await provider.generate(
    {
      prompt: input.prompt,
      aspect_ratio,
      duration,
      resolution,
      negative_prompt: input.negative_prompt,
      seed: input.seed,
      person_generation: input.person_generation
    },
    credentials,
    modelId
  );

  // Add provider metadata to the result
  if (result.success) {
    const capabilities = modelInfo
      ? modelInfo.capabilities.map(c => c.toString())
      : ['text-to-video'];
    const supportsExtend = capabilities.includes('video-extend');

    result.provider_metadata = {
      ...result.provider_metadata,
      provider_used: providerId,
      model_used: modelId,
      capabilities: capabilities,
      can_extend: supportsExtend,
      extend_with: supportsExtend ? providerId : undefined
    };
  }

  return result;
}

export default generateVideo;

/**
 * Image-to-Video Tool - fal.ai Provider
 *
 * Animate a still image into a video using AI via fal.ai.
 * Videos are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   FAL_KEY - fal.ai API key
 *   VIDEO_MODEL - Model to use (optional, defaults to first i2v-capable fal.ai model)
 *
 * Usage:
 *   import { imageToVideo } from '/opt/ai-tools/dist/video-generation/imageToVideo.js';
 *
 *   const result = await imageToVideo({
 *     image_path: '/path/to/image.png',
 *     prompt: 'The character starts walking forward slowly'
 *   });
 *
 *   if (result.success) {
 *     // result.video_url contains the URL to access the video
 *     // result.file_path contains the local file path
 *   }
 */

import { registry } from '../providers/registry.js';
import { VideoResult, ProviderCredentials } from '../providers/types.js';
import { parseModelParamsEnv } from '../providers/shared/fal-helpers.js';

// Re-export VideoResponse for backwards compatibility
export { VideoResponse } from './shared.js';

export interface ImageToVideoInput {
  /**
   * Path to the source image file to animate.
   * Supported formats: JPEG, PNG, GIF, WebP.
   * For best quality, use 720p (1280x720) or higher resolution.
   * Aspect ratio should be 16:9 or 9:16.
   */
  image_path: string;

  /**
   * Text prompt describing the motion/animation to apply.
   * Describe what should happen in the video, not just the scene.
   * Max 1,024 tokens.
   *
   * @example "The dog starts running towards the camera"
   * @example "Clouds slowly drift across the sky, trees sway gently"
   */
  prompt: string;

  /**
   * Aspect ratio of the generated video.
   * Should match your input image for best results.
   * @default "16:9"
   */
  aspect_ratio?: '16:9' | '9:16';

  /**
   * Duration of the generated video in seconds.
   * @default 8
   */
  duration?: 4 | 6 | 8;

  /**
   * Resolution of the generated video.
   * @default "720p"
   */
  resolution?: '720p' | '1080p';

  /**
   * Elements to exclude from the video generation.
   * @example "blurry, shaky camera, text, watermark"
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

  /**
   * Override the default model for image-to-video.
   * If not specified, uses VIDEO_MODEL env var or first i2v-capable fal.ai model.
   */
  model?: string;
}

export type ImageToVideoResponse = VideoResult;

/**
 * Convert a still image into an animated video using fal.ai.
 *
 * The video is saved to disk and a URL is returned for display in the chat UI.
 * Video generation is asynchronous - this function polls until complete.
 *
 * @param input - The generation parameters including source image
 * @returns The generated video URL and file path, or error details
 *
 * @example
 * ```typescript
 * const result = await imageToVideo({
 *   image_path: '/workspace/photo.jpg',
 *   prompt: 'The person in the photo smiles and waves at the camera',
 *   duration: 4,
 *   aspect_ratio: '16:9'
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
export async function imageToVideo(input: ImageToVideoInput): Promise<ImageToVideoResponse> {
  // Get API key from environment
  const apiKey = process.env.FAL_KEY;
  if (!apiKey) {
    return {
      success: false,
      error: 'FAL_KEY environment variable not set. Video generation is not configured.'
    };
  }

  // Get provider from registry
  const provider = registry.getVideoProvider('fal-ai');
  if (!provider) {
    return {
      success: false,
      error: 'fal-ai video provider not found in registry.'
    };
  }

  if (!provider.imageToVideo) {
    return {
      success: false,
      error: 'fal-ai video provider does not support image-to-video.'
    };
  }

  // Validate input
  if (!input.image_path) {
    return {
      success: false,
      error: 'image_path is required'
    };
  }

  if (!input.prompt || input.prompt.trim().length === 0) {
    return {
      success: false,
      error: 'Prompt cannot be empty. Describe the motion/animation to apply.'
    };
  }

  if (input.prompt.length > 4000) {
    return {
      success: false,
      error: 'Prompt is too long. Maximum ~1,024 tokens (approximately 4,000 characters).'
    };
  }

  // Get model - find first i2v-capable model
  let modelId = input.model || process.env.VIDEO_MODEL || '';
  if (!modelId) {
    const i2vModel = provider.models.find(m => m.capabilities.includes('image-to-video'));
    modelId = i2vModel?.id || provider.models[0]?.id || '';
  }

  if (!modelId) {
    return {
      success: false,
      error: 'No image-to-video model found for fal-ai provider.'
    };
  }

  // Build credentials
  const credentials: ProviderCredentials = { apiKey };

  // Apply env var defaults when caller doesn't specify
  const aspect_ratio = input.aspect_ratio || (process.env.VIDEO_ASPECT_RATIO as any) || undefined;
  const duration = input.duration || (process.env.VIDEO_DURATION ? parseInt(process.env.VIDEO_DURATION) : undefined);
  const resolution = input.resolution || (process.env.VIDEO_RESOLUTION as any) || undefined;

  try {
    const result = await provider.imageToVideo(
      {
        prompt: input.prompt.trim(),
        image_path: input.image_path,
        aspect_ratio,
        duration,
        resolution,
        negative_prompt: input.negative_prompt,
        seed: input.seed,
        person_generation: input.person_generation,
        extra_params: parseModelParamsEnv('I2V_MODEL_PARAMS')
      },
      credentials,
      modelId
    );

    // Add provider metadata
    if (result.success) {
      result.provider_metadata = {
        ...result.provider_metadata,
        provider_used: 'fal-ai',
        model_used: modelId
      };
    }

    return result;

  } catch (error) {
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error occurred'
    };
  }
}

export default imageToVideo;

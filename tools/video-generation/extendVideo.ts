/**
 * Video Extension Tool - fal.ai Provider
 *
 * Extend an existing video's duration using AI via fal.ai.
 * Takes a video file path and generates additional frames.
 *
 * Environment Variables:
 *   FAL_KEY - fal.ai API key
 *   VIDEO_MODEL - Default model (optional)
 */

import { readFileSync, existsSync } from 'fs';
import { registry } from '../providers/registry.js';
import { VideoResult, ProviderCredentials } from '../providers/types.js';

export interface ExtendVideoInput {
  /**
   * Path to the video file to extend.
   * Supported formats: MP4, WebM.
   */
  video_path: string;

  /**
   * Optional prompt to guide the continuation.
   * Describe what should happen next in the video.
   */
  prompt?: string;

  /**
   * Override the default model for video extension.
   * Kling models support video extension natively.
   */
  model?: string;

  /**
   * Elements to exclude from the generated extension.
   */
  negative_prompt?: string;

  /**
   * Seed for reproducibility.
   */
  seed?: number;

  /**
   * Control generation of people.
   * @default "allow_adult"
   */
  person_generation?: 'allow_all' | 'allow_adult';
}

export type ExtendVideoResponse = VideoResult;

/**
 * Convert a video file to a data URI for upload to fal.ai.
 */
function videoToDataUri(videoPath: string): string | null {
  try {
    if (!existsSync(videoPath)) return null;
    const buffer = readFileSync(videoPath);
    const ext = videoPath.toLowerCase().split('.').pop();
    const mimeTypes: Record<string, string> = {
      'mp4': 'video/mp4',
      'webm': 'video/webm',
      'mov': 'video/quicktime',
    };
    const mimeType = mimeTypes[ext || ''] || 'video/mp4';
    return `data:${mimeType};base64,${buffer.toString('base64')}`;
  } catch {
    return null;
  }
}

/**
 * Extend an existing video by generating additional frames.
 *
 * Uses fal.ai's video extension capability (e.g., Kling extend endpoints).
 * The extended video is saved to disk and a URL is returned for display.
 *
 * @param input - The extension parameters including source video
 * @returns The extended video URL and file path, or error details
 */
export async function extendVideo(input: ExtendVideoInput): Promise<ExtendVideoResponse> {
  // Get API key
  const apiKey = process.env.FAL_KEY;
  if (!apiKey) {
    return {
      success: false,
      error: 'FAL_KEY environment variable not set. Video extension is not configured.'
    };
  }

  // Get provider
  const provider = registry.getVideoProvider('fal-ai');
  if (!provider) {
    return {
      success: false,
      error: 'fal-ai video provider not found in registry.'
    };
  }

  if (!provider.extend) {
    return {
      success: false,
      error: 'fal-ai video provider does not support video extension.'
    };
  }

  // Validate input
  if (!input.video_path) {
    return {
      success: false,
      error: 'video_path is required'
    };
  }

  // Convert video to data URI
  const videoDataUri = videoToDataUri(input.video_path);
  if (!videoDataUri) {
    return {
      success: false,
      error: `Video file not found or unreadable: ${input.video_path}`
    };
  }

  // Determine model — prefer Kling extend endpoints
  let modelId = input.model || process.env.VIDEO_MODEL || '';
  if (!modelId) {
    // Default to Kling O3 Standard extend endpoint
    modelId = 'fal-ai/kling-video/o3/standard/extend';
  }

  // Build credentials
  const credentials: ProviderCredentials = { apiKey };

  try {
    const result = await provider.extend(
      {
        video_reference: videoDataUri,
        prompt: input.prompt,
        negative_prompt: input.negative_prompt,
        seed: input.seed,
        person_generation: input.person_generation,
      },
      credentials,
      modelId
    );

    // Add provider metadata
    if (result.success) {
      result.provider_metadata = {
        ...result.provider_metadata,
        provider_used: 'fal-ai',
        model_used: modelId,
        capabilities: ['video-extend'],
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

export default extendVideo;

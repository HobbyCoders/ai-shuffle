/**
 * OpenAI Sora Video Provider
 *
 * Adapter for OpenAI's Sora video generation API.
 * Supports text-to-video and image-to-video generation.
 *
 * API Reference: https://platform.openai.com/docs/api-reference/videos
 */

import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';
import {
  VideoProvider,
  VideoResult,
  UnifiedVideoInput,
  UnifiedI2VInput,
  ProviderCredentials,
} from '../types.js';

// ============================================================================
// Helpers
// ============================================================================

function getGeneratedVideosDir(): string {
  if (process.env.GENERATED_VIDEOS_DIR) {
    return process.env.GENERATED_VIDEOS_DIR;
  }
  return join(process.cwd(), 'generated-videos');
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function handleApiError(response: Response, errorData: any): VideoResult {
  const errorMsg = errorData?.error?.message || `HTTP ${response.status}`;

  if (response.status === 400) {
    const errorStr = JSON.stringify(errorData);
    if (errorStr.includes('safety') || errorStr.toLowerCase().includes('content_policy')) {
      return {
        success: false,
        error: 'Video generation was blocked by content policy. Please try a different prompt.'
      };
    }
  }

  if (response.status === 401) {
    return { success: false, error: 'API key is invalid or expired.' };
  }

  if (response.status === 403) {
    return { success: false, error: 'API key does not have access to Sora. Ensure you have a paid tier (Tier 1+).' };
  }

  if (response.status === 429) {
    return { success: false, error: 'Rate limit exceeded. Please wait a moment and try again.' };
  }

  return { success: false, error: `API error: ${errorMsg}` };
}

/**
 * Poll for video generation completion
 */
async function pollForCompletion(
  videoId: string,
  apiKey: string,
  maxPollTime: number = 10 * 60 * 1000,  // 10 minutes for Sora
  pollInterval: number = 10000  // 10 seconds
): Promise<{ success: true; videoId: string } | { success: false; error: string }> {
  const startTime = Date.now();

  while (Date.now() - startTime < maxPollTime) {
    await sleep(pollInterval);

    const pollResponse = await fetch(
      `https://api.openai.com/v1/videos/${videoId}`,
      {
        method: 'GET',
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json'
        }
      }
    );

    if (!pollResponse.ok) {
      const errorData = await pollResponse.json().catch(() => ({}));
      const errorMsg = (errorData as any)?.error?.message || `HTTP ${pollResponse.status}`;
      return { success: false, error: `Polling error: ${errorMsg}` };
    }

    const pollResult = await pollResponse.json() as any;

    if (pollResult.status === 'completed') {
      return { success: true, videoId };
    }

    if (pollResult.status === 'failed') {
      const failReason = pollResult.error?.message || pollResult.failure_reason || 'Unknown error';
      return { success: false, error: `Video generation failed: ${failReason}` };
    }

    // Log progress if available
    if (pollResult.progress !== undefined) {
      // Progress is a percentage
    }
  }

  return { success: false, error: 'Video generation timed out. Please try again.' };
}

/**
 * Download video content from OpenAI
 */
async function downloadVideo(videoId: string, apiKey: string): Promise<Buffer | { error: string }> {
  const response = await fetch(
    `https://api.openai.com/v1/videos/${videoId}/content`,
    {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${apiKey}`
      }
    }
  );

  if (!response.ok) {
    return { error: `Failed to download video: HTTP ${response.status}` };
  }

  return Buffer.from(await response.arrayBuffer());
}

function saveVideo(videoBuffer: Buffer, prefix: string, duration: number): VideoResult {
  const timestamp = Date.now();
  const randomSuffix = Math.random().toString(36).substring(2, 8);
  const filename = `${prefix}-${timestamp}-${randomSuffix}.mp4`;

  const outputDir = getGeneratedVideosDir();

  if (!existsSync(outputDir)) {
    mkdirSync(outputDir, { recursive: true });
  }

  const filePath = join(outputDir, filename);
  writeFileSync(filePath, videoBuffer);

  const encodedPath = encodeURIComponent(filePath);
  return {
    success: true,
    video_url: `/api/generated-videos/by-path?path=${encodedPath}`,
    file_path: filePath,
    filename: filename,
    mime_type: 'video/mp4',
    duration_seconds: duration
  };
}

/**
 * Convert unified aspect ratio to OpenAI size format
 */
function aspectRatioToSize(aspectRatio: string = '16:9', resolution: string = '720p'): string {
  // OpenAI uses WxH format
  const is1080 = resolution === '1080p';

  switch (aspectRatio) {
    case '16:9':
      return is1080 ? '1920x1080' : '1280x720';
    case '9:16':
      return is1080 ? '1080x1920' : '720x1280';
    case '1:1':
      return is1080 ? '1080x1080' : '720x720';
    default:
      return '1280x720';
  }
}

/**
 * Convert unified duration to OpenAI seconds format
 */
function normalizeSeconds(duration: number = 8): string {
  // Sora supports 4, 8, or 12 seconds
  if (duration <= 4) return '4';
  if (duration <= 8) return '8';
  return '12';
}

// ============================================================================
// Provider Implementation
// ============================================================================

export const openaiSoraProvider: VideoProvider = {
  id: 'openai-sora',
  name: 'OpenAI Sora',
  models: [
    {
      id: 'sora-2',
      name: 'Sora 2',
      description: 'Fast video generation with good quality - ~$0.10/second',
      capabilities: ['text-to-video', 'image-to-video'],
      pricing: { unit: 'second', price: 0.10 },
      constraints: {
        minDuration: 4,
        maxDuration: 12,
        supportedResolutions: ['720p', '1080p'],
        supportedAspectRatios: ['16:9', '9:16', '1:1']
      }
    },
    {
      id: 'sora-2-pro',
      name: 'Sora 2 Pro',
      description: 'High quality video generation with better details - ~$0.40/second',
      capabilities: ['text-to-video', 'image-to-video'],
      pricing: { unit: 'second', price: 0.40 },
      constraints: {
        minDuration: 4,
        maxDuration: 12,
        supportedResolutions: ['720p', '1080p'],
        supportedAspectRatios: ['16:9', '9:16', '1:1']
      }
    }
  ],

  async generate(
    input: UnifiedVideoInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<VideoResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.prompt || input.prompt.trim().length === 0) {
      return { success: false, error: 'Prompt cannot be empty' };
    }

    if (input.prompt.length > 4000) {
      return { success: false, error: 'Prompt is too long. Maximum approximately 4,000 characters.' };
    }

    const prompt = input.prompt.trim();
    const duration = input.duration || 8;
    const size = aspectRatioToSize(input.aspect_ratio, input.resolution);
    const seconds = normalizeSeconds(duration);

    try {
      // Create video generation request
      const response = await fetch('https://api.openai.com/v1/videos', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          model: model,
          prompt: prompt,
          size: size,
          seconds: seconds
        })
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const createResult = await response.json() as any;
      const videoId = createResult.id;

      if (!videoId) {
        return { success: false, error: 'Failed to start video generation: no video ID returned.' };
      }

      // Poll for completion
      const pollResult = await pollForCompletion(videoId, apiKey);
      if (!pollResult.success) {
        return { success: false, error: pollResult.error };
      }

      // Download the video
      const videoBuffer = await downloadVideo(pollResult.videoId, apiKey);
      if ('error' in videoBuffer) {
        return { success: false, error: videoBuffer.error };
      }

      return saveVideo(videoBuffer, 'sora', parseInt(seconds));

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async imageToVideo(
    input: UnifiedI2VInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<VideoResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.image_path) {
      return { success: false, error: 'Image path is required' };
    }

    if (!existsSync(input.image_path)) {
      return { success: false, error: `Image file not found: ${input.image_path}` };
    }

    const duration = input.duration || 8;
    const size = aspectRatioToSize(input.aspect_ratio, input.resolution);
    const seconds = normalizeSeconds(duration);

    try {
      // Read the image file
      const imageBuffer = readFileSync(input.image_path);

      // Determine mime type
      const ext = input.image_path.toLowerCase().split('.').pop();
      const mimeTypes: Record<string, string> = {
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
        'webp': 'image/webp'
      };
      const mimeType = mimeTypes[ext || ''] || 'image/jpeg';

      // Create FormData for multipart upload
      // Sora API requires multipart/form-data with input_reference as a file field
      const formData = new FormData();

      // Add required fields
      formData.append('model', model);
      formData.append('prompt', input.prompt || 'Animate this image smoothly');
      formData.append('size', size);
      formData.append('seconds', seconds);

      // Add the image as input_reference file field
      const imageBlob = new Blob([imageBuffer], { type: mimeType });
      const imageFilename = `input_frame.${ext || 'jpg'}`;
      formData.append('input_reference', imageBlob, imageFilename);

      const response = await fetch('https://api.openai.com/v1/videos', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${apiKey}`
          // Don't set Content-Type - let fetch handle it for FormData
        },
        body: formData
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const createResult = await response.json() as any;
      const videoId = createResult.id;

      if (!videoId) {
        return { success: false, error: 'Failed to start video generation: no video ID returned.' };
      }

      // Poll for completion
      const pollResult = await pollForCompletion(videoId, apiKey);
      if (!pollResult.success) {
        return { success: false, error: pollResult.error };
      }

      // Download the video
      const videoBuffer = await downloadVideo(pollResult.videoId, apiKey);
      if ('error' in videoBuffer) {
        return { success: false, error: videoBuffer.error };
      }

      return saveVideo(videoBuffer, 'sora-i2v', parseInt(seconds));

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  // Note: Sora doesn't support video extension like Veo does
  // extend is not implemented for this provider

  // Note: Sora doesn't support frame bridging like Veo does
  // bridgeFrames is not implemented for this provider

  async validateCredentials(credentials: ProviderCredentials): Promise<{ valid: boolean; error?: string }> {
    try {
      // Test with models endpoint
      const response = await fetch('https://api.openai.com/v1/models', {
        method: 'GET',
        headers: {
          'Authorization': `Bearer ${credentials.apiKey}`
        }
      });

      if (response.status === 401) {
        return { valid: false, error: 'Invalid API key' };
      }

      if (response.status === 403) {
        return { valid: false, error: 'API key does not have sufficient permissions' };
      }

      if (!response.ok) {
        return { valid: false, error: `API returned status ${response.status}` };
      }

      // Check if Sora models are accessible
      const data = await response.json() as any;
      const models = data.data || [];
      const hasSora = models.some((m: any) =>
        m.id?.includes('sora') || m.id?.includes('video')
      );

      if (!hasSora) {
        return {
          valid: true,
          error: 'Warning: Sora models may not be available on your account tier. Requires Tier 1+.'
        };
      }

      return { valid: true };
    } catch (error) {
      return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
    }
  }
};

export default openaiSoraProvider;

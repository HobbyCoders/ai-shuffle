/**
 * Google Veo Video Provider
 *
 * Adapter for Google's Veo video generation API.
 * Supports text-to-video, image-to-video, video extension, and frame bridging.
 */

import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';
import {
  VideoProvider,
  VideoResult,
  UnifiedVideoInput,
  UnifiedI2VInput,
  UnifiedExtendInput,
  UnifiedBridgeInput,
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

function imageToBase64(imagePath: string): { base64: string; mimeType: string } | { error: string } {
  try {
    if (!existsSync(imagePath)) {
      return { error: `Image file not found: ${imagePath}` };
    }

    const imageBuffer = readFileSync(imagePath);
    const base64 = imageBuffer.toString('base64');

    const ext = imagePath.toLowerCase().split('.').pop();
    const mimeTypes: Record<string, string> = {
      'jpg': 'image/jpeg',
      'jpeg': 'image/jpeg',
      'png': 'image/png',
      'gif': 'image/gif',
      'webp': 'image/webp',
    };

    const mimeType = mimeTypes[ext || ''] || 'image/jpeg';
    return { base64, mimeType };
  } catch (error) {
    return { error: error instanceof Error ? error.message : 'Failed to read image file' };
  }
}

function handleApiError(response: Response, errorData: any): VideoResult {
  const errorMsg = errorData?.error?.message || `HTTP ${response.status}`;

  if (response.status === 400) {
    const errorStr = JSON.stringify(errorData);
    if (errorStr.includes('SAFETY') || errorStr.toLowerCase().includes('blocked')) {
      return {
        success: false,
        error: 'Video generation was blocked by safety filters. Please try a different prompt.'
      };
    }
  }

  if (response.status === 401 || response.status === 403) {
    return { success: false, error: 'API key is invalid or expired.' };
  }

  if (response.status === 429) {
    return { success: false, error: 'Rate limit exceeded. Please wait a moment and try again.' };
  }

  return { success: false, error: `API error: ${errorMsg}` };
}

async function pollForCompletion(
  operationName: string,
  apiKey: string,
  maxPollTime: number = 6 * 60 * 1000,
  pollInterval: number = 5000
): Promise<{ success: true; videoUri: string } | { success: false; error: string }> {
  const startTime = Date.now();

  while (Date.now() - startTime < maxPollTime) {
    await sleep(pollInterval);

    const pollResponse = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/${operationName}?key=${apiKey}`,
      {
        method: 'GET',
        headers: { 'Content-Type': 'application/json' },
      }
    );

    if (!pollResponse.ok) {
      const errorData = await pollResponse.json().catch(() => ({}));
      const errorMsg = (errorData as any)?.error?.message || `HTTP ${pollResponse.status}`;
      return { success: false, error: `Polling error: ${errorMsg}` };
    }

    const pollResult = await pollResponse.json() as any;

    if (pollResult.done) {
      if (pollResult.error) {
        return {
          success: false,
          error: `Video generation failed: ${pollResult.error.message || 'Unknown error'}`
        };
      }

      const generatedSamples = pollResult.response?.generateVideoResponse?.generatedSamples
        || pollResult.response?.generatedSamples
        || [];

      if (generatedSamples.length === 0) {
        return {
          success: false,
          error: 'No video was generated. The model may have refused the request.'
        };
      }

      const videoUri = generatedSamples[0]?.video?.uri;
      if (!videoUri) {
        return { success: false, error: 'Video URI not found in response.' };
      }

      return { success: true, videoUri };
    }
  }

  return { success: false, error: 'Video generation timed out. Please try again.' };
}

async function downloadVideo(videoUri: string, apiKey: string): Promise<Buffer | { error: string }> {
  let downloadUrl = videoUri;
  if (videoUri.includes('generativelanguage.googleapis.com')) {
    downloadUrl = videoUri.includes('?')
      ? `${videoUri}&key=${apiKey}`
      : `${videoUri}?key=${apiKey}`;
  }

  const videoResponse = await fetch(downloadUrl);
  if (!videoResponse.ok) {
    return { error: `Failed to download video: HTTP ${videoResponse.status}` };
  }
  return Buffer.from(await videoResponse.arrayBuffer());
}

function saveVideo(videoBuffer: Buffer, prefix: string, duration: number, sourceVideoUri?: string): VideoResult {
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
    duration_seconds: duration,
    ...(sourceVideoUri && { source_video_uri: sourceVideoUri })
  };
}

// ============================================================================
// Provider Implementation
// ============================================================================

export const googleVeoProvider: VideoProvider = {
  id: 'google-veo',
  name: 'Google Veo',
  models: [
    // Veo 3 models with native audio generation
    {
      id: 'veo-3-fast-generate-preview',
      name: 'Veo 3 Fast',
      description: 'Fast video with native audio (dialogue, effects, music) - ~$0.40/second',
      capabilities: ['text-to-video', 'image-to-video', 'video-with-audio'],
      pricing: { unit: 'second', price: 0.40 },
      constraints: {
        minDuration: 4,
        maxDuration: 8,
        supportedResolutions: ['720p', '1080p'],
        supportedAspectRatios: ['16:9', '9:16']
      }
    },
    {
      id: 'veo-3-generate-preview',
      name: 'Veo 3',
      description: 'Highest quality video with native audio - ~$0.75/second',
      capabilities: ['text-to-video', 'image-to-video', 'video-with-audio'],
      pricing: { unit: 'second', price: 0.75 },
      constraints: {
        minDuration: 4,
        maxDuration: 8,
        supportedResolutions: ['720p', '1080p'],
        supportedAspectRatios: ['16:9', '9:16']
      }
    },
    // Veo 3.1 models without audio
    {
      id: 'veo-3.1-fast-generate-preview',
      name: 'Veo 3.1 Fast',
      description: 'Fast video generation with lower latency - ~$0.15/second',
      capabilities: ['text-to-video', 'image-to-video', 'video-extend', 'frame-bridge'],
      pricing: { unit: 'second', price: 0.15 },
      constraints: {
        minDuration: 4,
        maxDuration: 8,
        supportedResolutions: ['720p', '1080p'],
        supportedAspectRatios: ['16:9', '9:16']
      }
    },
    {
      id: 'veo-3.1-generate-preview',
      name: 'Veo 3.1',
      description: 'High quality video generation - ~$0.40/second',
      capabilities: ['text-to-video', 'image-to-video', 'video-extend', 'frame-bridge'],
      pricing: { unit: 'second', price: 0.40 },
      constraints: {
        minDuration: 4,
        maxDuration: 8,
        supportedResolutions: ['720p', '1080p'],
        supportedAspectRatios: ['16:9', '9:16']
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
      return { success: false, error: 'Prompt is too long. Maximum ~1,024 tokens (approximately 4,000 characters).' };
    }

    const duration = input.duration || 8;
    const resolution = input.resolution || '720p';

    // Validate resolution/duration combination
    if (resolution === '1080p' && duration !== 8) {
      return { success: false, error: '1080p resolution is only available for 8-second videos.' };
    }

    const prompt = input.prompt.trim();
    const aspectRatio = input.aspect_ratio || '16:9';

    try {
      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:predictLongRunning?key=${apiKey}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            instances: [{ prompt }],
            parameters: {
              aspectRatio,
              durationSeconds: duration,
              resolution,
              ...(input.negative_prompt && { negativePrompt: input.negative_prompt }),
              ...(input.seed !== undefined && { seed: input.seed }),
              ...(input.person_generation && { personGeneration: input.person_generation })
            }
          })
        }
      );

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const startResult = await response.json() as any;
      const operationName = startResult.name;

      if (!operationName) {
        return { success: false, error: 'Failed to start video generation: no operation name returned.' };
      }

      // Poll for completion
      const pollResult = await pollForCompletion(operationName, apiKey);
      if (!pollResult.success) {
        return { success: false, error: pollResult.error };
      }

      // Download the video
      const videoBuffer = await downloadVideo(pollResult.videoUri, apiKey);
      if ('error' in videoBuffer) {
        return { success: false, error: videoBuffer.error };
      }

      return saveVideo(videoBuffer, 'video', duration, pollResult.videoUri);

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

    // Load the image
    const imageData = imageToBase64(input.image_path);
    if ('error' in imageData) {
      return { success: false, error: imageData.error };
    }

    const duration = input.duration || 8;
    const resolution = input.resolution || '720p';
    const aspectRatio = input.aspect_ratio || '16:9';

    try {
      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:predictLongRunning?key=${apiKey}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            instances: [{
              prompt: input.prompt || '',
              image: {
                bytesBase64Encoded: imageData.base64,
                mimeType: imageData.mimeType
              }
            }],
            parameters: {
              aspectRatio,
              durationSeconds: duration,
              resolution,
              ...(input.negative_prompt && { negativePrompt: input.negative_prompt }),
              ...(input.seed !== undefined && { seed: input.seed }),
              ...(input.person_generation && { personGeneration: input.person_generation })
            }
          })
        }
      );

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const startResult = await response.json() as any;
      const operationName = startResult.name;

      if (!operationName) {
        return { success: false, error: 'Failed to start video generation: no operation name returned.' };
      }

      const pollResult = await pollForCompletion(operationName, apiKey);
      if (!pollResult.success) {
        return { success: false, error: pollResult.error };
      }

      const videoBuffer = await downloadVideo(pollResult.videoUri, apiKey);
      if ('error' in videoBuffer) {
        return { success: false, error: videoBuffer.error };
      }

      return saveVideo(videoBuffer, 'i2v', duration, pollResult.videoUri);

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async extend(
    input: UnifiedExtendInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<VideoResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.video_reference) {
      return { success: false, error: 'Video reference URI is required' };
    }

    if (!input.video_reference.includes('generativelanguage.googleapis.com')) {
      return {
        success: false,
        error: 'Can only extend videos generated by Veo. Use the source_video_uri from a previous generation.'
      };
    }

    try {
      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:predictLongRunning?key=${apiKey}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            instances: [{
              video: { uri: input.video_reference },
              ...(input.prompt && { prompt: input.prompt })
            }],
            parameters: {
              ...(input.negative_prompt && { negativePrompt: input.negative_prompt }),
              ...(input.seed !== undefined && { seed: input.seed }),
              ...(input.person_generation && { personGeneration: input.person_generation })
            }
          })
        }
      );

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const startResult = await response.json() as any;
      const operationName = startResult.name;

      if (!operationName) {
        return { success: false, error: 'Failed to start video extension: no operation name returned.' };
      }

      const pollResult = await pollForCompletion(operationName, apiKey);
      if (!pollResult.success) {
        return { success: false, error: pollResult.error };
      }

      const videoBuffer = await downloadVideo(pollResult.videoUri, apiKey);
      if ('error' in videoBuffer) {
        return { success: false, error: videoBuffer.error };
      }

      // Extended videos are approximately 7 seconds
      return saveVideo(videoBuffer, 'extended', 7, pollResult.videoUri);

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async bridgeFrames(
    input: UnifiedBridgeInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<VideoResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.start_image) {
      return { success: false, error: 'Start image is required' };
    }

    if (!input.end_image) {
      return { success: false, error: 'End image is required' };
    }

    // Load both images
    const startImageData = imageToBase64(input.start_image);
    if ('error' in startImageData) {
      return { success: false, error: `Start image: ${startImageData.error}` };
    }

    const endImageData = imageToBase64(input.end_image);
    if ('error' in endImageData) {
      return { success: false, error: `End image: ${endImageData.error}` };
    }

    const duration = input.duration || 8;
    const resolution = input.resolution || '720p';
    const aspectRatio = input.aspect_ratio || '16:9';

    try {
      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:predictLongRunning?key=${apiKey}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            instances: [{
              image: {
                bytesBase64Encoded: startImageData.base64,
                mimeType: startImageData.mimeType
              },
              lastFrame: {
                bytesBase64Encoded: endImageData.base64,
                mimeType: endImageData.mimeType
              },
              ...(input.prompt && { prompt: input.prompt })
            }],
            parameters: {
              aspectRatio,
              durationSeconds: duration,
              resolution,
              ...(input.negative_prompt && { negativePrompt: input.negative_prompt }),
              ...(input.seed !== undefined && { seed: input.seed }),
              ...(input.person_generation && { personGeneration: input.person_generation })
            }
          })
        }
      );

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const startResult = await response.json() as any;
      const operationName = startResult.name;

      if (!operationName) {
        return { success: false, error: 'Failed to start frame bridging: no operation name returned.' };
      }

      const pollResult = await pollForCompletion(operationName, apiKey);
      if (!pollResult.success) {
        return { success: false, error: pollResult.error };
      }

      const videoBuffer = await downloadVideo(pollResult.videoUri, apiKey);
      if ('error' in videoBuffer) {
        return { success: false, error: videoBuffer.error };
      }

      return saveVideo(videoBuffer, 'bridge', duration, pollResult.videoUri);

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async validateCredentials(credentials: ProviderCredentials): Promise<{ valid: boolean; error?: string }> {
    try {
      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models?key=${credentials.apiKey}`
      );

      if (response.status === 400 && (await response.text()).includes('API_KEY_INVALID')) {
        return { valid: false, error: 'Invalid API key' };
      }

      if (response.status === 403) {
        return { valid: false, error: 'API key does not have permission. Enable the Generative Language API.' };
      }

      return { valid: response.ok };
    } catch (error) {
      return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
    }
  }
};

export default googleVeoProvider;

/**
 * fal.ai Video Provider
 *
 * Adapter for fal.ai's video generation API.
 * Supports Kling 3.0 for text-to-video and image-to-video through a single API key.
 */

import { fal } from '@fal-ai/client';
import { readFileSync, existsSync } from 'fs';
import {
  VideoProvider,
  VideoResult,
  UnifiedVideoInput,
  UnifiedI2VInput,
  ProviderCredentials,
} from '../types.js';
import {
  downloadToFile,
  handleFalError,
  getGeneratedVideosDir,
} from '../shared/fal-helpers.js';

// ============================================================================
// Helpers
// ============================================================================

function imageToDataUri(imagePath: string): string | null {
  try {
    if (!existsSync(imagePath)) return null;
    const buffer = readFileSync(imagePath);
    const ext = imagePath.toLowerCase().split('.').pop();
    const mimeTypes: Record<string, string> = {
      'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
      'gif': 'image/gif', 'webp': 'image/webp',
    };
    const mimeType = mimeTypes[ext || ''] || 'image/jpeg';
    return `data:${mimeType};base64,${buffer.toString('base64')}`;
  } catch {
    return null;
  }
}

// ============================================================================
// Provider Implementation
// ============================================================================

export const falVideoProvider: VideoProvider = {
  id: 'fal-ai',
  name: 'fal.ai (Kling)',
  models: [
    {
      id: 'fal-ai/kling-video/v3/text-to-video',
      name: 'Kling 3.0 (Text)',
      description: 'High-quality video from text prompts - ~$0.10/sec',
      capabilities: ['text-to-video'],
      pricing: { unit: 'second', price: 0.10 },
      constraints: {
        minDuration: 5,
        maxDuration: 10,
        supportedAspectRatios: ['16:9', '9:16', '1:1'],
      }
    },
    {
      id: 'fal-ai/kling-video/v3/image-to-video',
      name: 'Kling 3.0 (Image)',
      description: 'Animate images into video - ~$0.10/sec',
      capabilities: ['image-to-video'],
      pricing: { unit: 'second', price: 0.10 },
      constraints: {
        minDuration: 5,
        maxDuration: 10,
        supportedAspectRatios: ['16:9', '9:16', '1:1'],
      }
    },
  ],

  async generate(
    input: UnifiedVideoInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<VideoResult> {
    if (!input.prompt || input.prompt.trim().length === 0) {
      return { success: false, error: 'Prompt cannot be empty' };
    }

    try {
      fal.config({ credentials: credentials.apiKey });

      // Kling duration is a string enum: "5" or "10"
      const duration = input.duration || 5;
      const durationStr = String(duration <= 5 ? 5 : 10);

      const falInput: Record<string, any> = {
        prompt: input.prompt.trim(),
        duration: durationStr,
        aspect_ratio: input.aspect_ratio || '16:9',
      };

      if (input.negative_prompt) {
        falInput.negative_prompt = input.negative_prompt;
      }

      const result = await fal.subscribe(model, {
        input: falInput,
      }) as any;

      // Kling returns: { video: { url: "https://..." } }
      const videoData = result.data?.video || result.video;
      if (!videoData?.url) {
        return { success: false, error: 'No video was generated. Please try a different prompt.' };
      }

      // Download video to local disk
      const outputDir = getGeneratedVideosDir();
      const downloaded = await downloadToFile(videoData.url, outputDir, 'fal-video', 'mp4');

      if ('error' in downloaded) {
        return { success: false, error: downloaded.error };
      }

      const encodedPath = encodeURIComponent(downloaded.file_path);
      return {
        success: true,
        video_url: `/api/generated-videos/by-path?path=${encodedPath}`,
        file_path: downloaded.file_path,
        filename: downloaded.filename,
        mime_type: 'video/mp4',
        duration_seconds: parseInt(durationStr),
      };

    } catch (error) {
      return { success: false, error: handleFalError(error) };
    }
  },

  async imageToVideo(
    input: UnifiedI2VInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<VideoResult> {
    if (!input.image_path) {
      return { success: false, error: 'Image path is required' };
    }

    // Convert local image to data URI for fal.ai upload
    const dataUri = imageToDataUri(input.image_path);
    if (!dataUri) {
      return { success: false, error: `Image file not found: ${input.image_path}` };
    }

    try {
      fal.config({ credentials: credentials.apiKey });

      const duration = input.duration || 5;
      const durationStr = String(duration <= 5 ? 5 : 10);

      // Use the image-to-video model variant
      const i2vModel = model.includes('image-to-video')
        ? model
        : model.replace('text-to-video', 'image-to-video');

      const falInput: Record<string, any> = {
        prompt: input.prompt || '',
        image_url: dataUri,
        duration: durationStr,
        aspect_ratio: input.aspect_ratio || '16:9',
      };

      if (input.negative_prompt) {
        falInput.negative_prompt = input.negative_prompt;
      }

      const result = await fal.subscribe(i2vModel, {
        input: falInput,
      }) as any;

      const videoData = result.data?.video || result.video;
      if (!videoData?.url) {
        return { success: false, error: 'No video was generated. Please try a different prompt.' };
      }

      const outputDir = getGeneratedVideosDir();
      const downloaded = await downloadToFile(videoData.url, outputDir, 'fal-i2v', 'mp4');

      if ('error' in downloaded) {
        return { success: false, error: downloaded.error };
      }

      const encodedPath = encodeURIComponent(downloaded.file_path);
      return {
        success: true,
        video_url: `/api/generated-videos/by-path?path=${encodedPath}`,
        file_path: downloaded.file_path,
        filename: downloaded.filename,
        mime_type: 'video/mp4',
        duration_seconds: parseInt(durationStr),
      };

    } catch (error) {
      return { success: false, error: handleFalError(error) };
    }
  },

  async validateCredentials(credentials: ProviderCredentials): Promise<{ valid: boolean; error?: string }> {
    try {
      fal.config({ credentials: credentials.apiKey });
      await fal.queue.status('fal-ai/kling-video/v3/text-to-video', {
        requestId: 'test-validation',
      }).catch((e: any) => {
        if (e?.status === 401 || e?.status === 403) {
          throw new Error('Invalid API key');
        }
        return null;
      });
      return { valid: true };
    } catch (error) {
      if (error instanceof Error && error.message.includes('Invalid API key')) {
        return { valid: false, error: 'Invalid fal.ai API key' };
      }
      return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
    }
  }
};

export default falVideoProvider;

/**
 * fal.ai Video Provider
 *
 * Adapter for fal.ai's video generation API.
 * Supports Kling, Veo, Sora, and more through a single API key.
 */

import { fal } from '@fal-ai/client';
import { readFileSync, existsSync } from 'fs';
import {
  VideoProvider,
  VideoResult,
  UnifiedVideoInput,
  UnifiedI2VInput,
  UnifiedExtendInput,
  ProviderCredentials,
} from '../types.js';
import {
  downloadToFile,
  handleFalError,
  getGeneratedVideosDir,
  applyExtraParams,
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
  name: 'fal.ai',
  models: [
    // Kling O3 (newest)
    {
      id: 'fal-ai/kling-video/o3/pro/text-to-video',
      name: 'Kling O3 Pro',
      description: 'Premium quality, cinematic visuals with fluid motion',
      capabilities: ['text-to-video'],
      pricing: { unit: 'second', price: 0.12 },
      constraints: { minDuration: 5, maxDuration: 10, supportedAspectRatios: ['16:9', '9:16', '1:1'] }
    },
    {
      id: 'fal-ai/kling-video/o3/standard/text-to-video',
      name: 'Kling O3 Standard',
      description: 'High quality text-to-video with native audio',
      capabilities: ['text-to-video'],
      pricing: { unit: 'second', price: 0.08 },
      constraints: { minDuration: 5, maxDuration: 10, supportedAspectRatios: ['16:9', '9:16', '1:1'] }
    },
    // Kling v3
    {
      id: 'fal-ai/kling-video/v3/pro/text-to-video',
      name: 'Kling 3.0 Pro',
      description: 'Cinematic visuals with native audio generation',
      capabilities: ['text-to-video'],
      pricing: { unit: 'second', price: 0.10 },
      constraints: { minDuration: 5, maxDuration: 10, supportedAspectRatios: ['16:9', '9:16', '1:1'] }
    },
    {
      id: 'fal-ai/kling-video/v3/standard/text-to-video',
      name: 'Kling 3.0 Standard',
      description: 'Good quality video from text prompts',
      capabilities: ['text-to-video'],
      pricing: { unit: 'second', price: 0.06 },
      constraints: { minDuration: 5, maxDuration: 10, supportedAspectRatios: ['16:9', '9:16', '1:1'] }
    },
    // Google Veo 3.1 (proxied through fal.ai)
    {
      id: 'fal-ai/veo3.1',
      name: 'Google Veo 3.1',
      description: 'Most advanced AI video — with synchronized audio',
      capabilities: ['text-to-video', 'video-with-audio'],
      pricing: { unit: 'second', price: 0.15 },
      constraints: { minDuration: 4, maxDuration: 8, supportedAspectRatios: ['16:9', '9:16'] }
    },
    {
      id: 'fal-ai/veo3.1/fast',
      name: 'Google Veo 3.1 Fast',
      description: 'Faster, cost-effective variant of Veo 3.1',
      capabilities: ['text-to-video', 'video-with-audio'],
      pricing: { unit: 'second', price: 0.10 },
      constraints: { minDuration: 4, maxDuration: 8, supportedAspectRatios: ['16:9', '9:16'] }
    },
    // OpenAI Sora 2 (proxied through fal.ai)
    {
      id: 'fal-ai/sora-2/text-to-video',
      name: 'Sora 2',
      description: 'OpenAI\'s state-of-the-art text-to-video with audio',
      capabilities: ['text-to-video'],
      pricing: { unit: 'second', price: 0.12 },
      constraints: { minDuration: 5, maxDuration: 20, supportedAspectRatios: ['16:9', '9:16', '1:1'] }
    },
    {
      id: 'fal-ai/sora-2/text-to-video/pro',
      name: 'Sora 2 Pro',
      description: 'Premium Sora 2 with higher quality',
      capabilities: ['text-to-video'],
      pricing: { unit: 'second', price: 0.18 },
      constraints: { minDuration: 5, maxDuration: 20, supportedAspectRatios: ['16:9', '9:16', '1:1'] }
    },
    // Image-to-Video models
    {
      id: 'fal-ai/kling-video/o3/pro/image-to-video',
      name: 'Kling O3 Pro (Image)',
      description: 'Premium image-to-video with fluid motion',
      capabilities: ['image-to-video'],
      pricing: { unit: 'second', price: 0.12 },
      constraints: { minDuration: 5, maxDuration: 10, supportedAspectRatios: ['16:9', '9:16', '1:1'] }
    },
    {
      id: 'fal-ai/kling-video/o3/standard/image-to-video',
      name: 'Kling O3 Standard (Image)',
      description: 'High quality image-to-video animation',
      capabilities: ['image-to-video'],
      pricing: { unit: 'second', price: 0.08 },
      constraints: { minDuration: 5, maxDuration: 10, supportedAspectRatios: ['16:9', '9:16', '1:1'] }
    },
    {
      id: 'fal-ai/kling-video/v3/pro/image-to-video',
      name: 'Kling 3.0 Pro (Image)',
      description: 'Animate images with cinematic quality',
      capabilities: ['image-to-video'],
      pricing: { unit: 'second', price: 0.10 },
      constraints: { minDuration: 5, maxDuration: 10, supportedAspectRatios: ['16:9', '9:16', '1:1'] }
    },
    {
      id: 'fal-ai/kling-video/v3/standard/image-to-video',
      name: 'Kling 3.0 Standard (Image)',
      description: 'Good quality image animation',
      capabilities: ['image-to-video'],
      pricing: { unit: 'second', price: 0.06 },
      constraints: { minDuration: 5, maxDuration: 10, supportedAspectRatios: ['16:9', '9:16', '1:1'] }
    },
    {
      id: 'fal-ai/veo3.1/image-to-video',
      name: 'Google Veo 3.1 (Image)',
      description: 'Image-to-video with Google\'s Veo 3.1',
      capabilities: ['image-to-video'],
      pricing: { unit: 'second', price: 0.15 },
      constraints: { minDuration: 4, maxDuration: 8, supportedAspectRatios: ['16:9', '9:16'] }
    },
    {
      id: 'fal-ai/sora-2/image-to-video',
      name: 'Sora 2 (Image)',
      description: 'OpenAI Sora 2 image-to-video',
      capabilities: ['image-to-video'],
      pricing: { unit: 'second', price: 0.12 },
      constraints: { minDuration: 5, maxDuration: 20, supportedAspectRatios: ['16:9', '9:16', '1:1'] }
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

      const duration = input.duration || 5;
      const falInput: Record<string, any> = {
        prompt: input.prompt.trim(),
        aspect_ratio: input.aspect_ratio || '16:9',
      };

      // Kling models use string duration
      if (model.includes('kling-video')) {
        falInput.duration = String(duration <= 5 ? 5 : 10);
      } else {
        falInput.duration = duration;
      }

      if (input.negative_prompt) {
        falInput.negative_prompt = input.negative_prompt;
      }

      const result = await fal.subscribe(model, {
        input: applyExtraParams(falInput, input.extra_params),
      }) as any;

      // Different models return video in different shapes
      const videoData = result.data?.video || result.video;
      const videoUrl = videoData?.url || result.data?.video_url || result.video_url;

      if (!videoUrl) {
        return { success: false, error: 'No video was generated. Please try a different prompt.' };
      }

      const outputDir = getGeneratedVideosDir();
      const downloaded = await downloadToFile(videoUrl, outputDir, 'fal-video', 'mp4');

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
        duration_seconds: duration,
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

    const dataUri = imageToDataUri(input.image_path);
    if (!dataUri) {
      return { success: false, error: `Image file not found: ${input.image_path}` };
    }

    try {
      fal.config({ credentials: credentials.apiKey });

      const duration = input.duration || 5;

      // Use the image-to-video model variant if available
      let i2vModel = model;
      if (!model.includes('image-to-video')) {
        i2vModel = model.replace('text-to-video', 'image-to-video');
      }

      const falInput: Record<string, any> = {
        prompt: input.prompt || '',
        image_url: dataUri,
        aspect_ratio: input.aspect_ratio || '16:9',
      };

      if (model.includes('kling-video')) {
        falInput.duration = String(duration <= 5 ? 5 : 10);
      } else {
        falInput.duration = duration;
      }

      if (input.negative_prompt) {
        falInput.negative_prompt = input.negative_prompt;
      }

      const result = await fal.subscribe(i2vModel, {
        input: applyExtraParams(falInput, input.extra_params),
      }) as any;

      const videoData = result.data?.video || result.video;
      const videoUrl = videoData?.url || result.data?.video_url || result.video_url;

      if (!videoUrl) {
        return { success: false, error: 'No video was generated. Please try a different prompt.' };
      }

      const outputDir = getGeneratedVideosDir();
      const downloaded = await downloadToFile(videoUrl, outputDir, 'fal-i2v', 'mp4');

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
        duration_seconds: duration,
      };

    } catch (error) {
      return { success: false, error: handleFalError(error) };
    }
  },

  async extend(
    input: UnifiedExtendInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<VideoResult> {
    if (!input.video_reference) {
      return { success: false, error: 'Video reference (file path or data URI) is required' };
    }

    try {
      fal.config({ credentials: credentials.apiKey });

      const falInput: Record<string, any> = {
        video_url: input.video_reference,
      };

      if (input.prompt) {
        falInput.prompt = input.prompt.trim();
      }
      if (input.negative_prompt) {
        falInput.negative_prompt = input.negative_prompt;
      }

      const result = await fal.subscribe(model, {
        input: applyExtraParams(falInput, input.extra_params),
      }) as any;

      const videoData = result.data?.video || result.video;
      const videoUrl = videoData?.url || result.data?.video_url || result.video_url;

      if (!videoUrl) {
        return { success: false, error: 'No extended video was generated. Please try again.' };
      }

      const outputDir = getGeneratedVideosDir();
      const downloaded = await downloadToFile(videoUrl, outputDir, 'fal-extend', 'mp4');

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
      };

    } catch (error) {
      return { success: false, error: handleFalError(error) };
    }
  },

  async validateCredentials(credentials: ProviderCredentials): Promise<{ valid: boolean; error?: string }> {
    try {
      fal.config({ credentials: credentials.apiKey });
      await fal.queue.status('fal-ai/kling-video/v3/standard/text-to-video', {
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

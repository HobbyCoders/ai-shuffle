/**
 * fal.ai Image Provider
 *
 * Adapter for fal.ai's image generation API.
 * Supports Flux Pro, Flux Dev, and Flux Schnell models through a single API key.
 */

import { fal } from '@fal-ai/client';
import {
  ImageProvider,
  ImageResult,
  UnifiedImageInput,
  ProviderCredentials,
  GeneratedImage,
} from '../types.js';
import {
  configureFal,
  downloadToFile,
  handleFalError,
  getGeneratedImagesDir,
} from '../shared/fal-helpers.js';

// ============================================================================
// Helpers
// ============================================================================

/**
 * Map our aspect ratio format to fal.ai's image_size enum.
 * fal.ai uses named presets rather than ratio strings.
 */
function mapImageSize(aspectRatio?: string, imageSize?: string): string | { width: number; height: number } {
  // If a specific size is requested, map to pixel dimensions
  if (imageSize) {
    const baseSize = aspectRatio === '1:1' ? { w: 1024, h: 1024 }
      : aspectRatio === '16:9' ? { w: 1024, h: 576 }
      : aspectRatio === '9:16' ? { w: 576, h: 1024 }
      : aspectRatio === '4:3' ? { w: 1024, h: 768 }
      : aspectRatio === '3:4' ? { w: 768, h: 1024 }
      : { w: 1024, h: 1024 };

    const multiplier = imageSize === '4K' ? 4 : imageSize === '2K' ? 2 : 1;
    return { width: baseSize.w * multiplier, height: baseSize.h * multiplier };
  }

  // Map aspect ratio to fal.ai presets
  switch (aspectRatio) {
    case '16:9': return 'landscape_16_9';
    case '9:16': return 'portrait_16_9';
    case '4:3': return 'landscape_4_3';
    case '3:4': return 'portrait_4_3';
    case '1:1':
    default: return 'square_hd';
  }
}

function getExtensionFromContentType(contentType?: string): string {
  if (!contentType) return 'png';
  if (contentType.includes('jpeg') || contentType.includes('jpg')) return 'jpg';
  if (contentType.includes('webp')) return 'webp';
  return 'png';
}

// ============================================================================
// Provider Implementation
// ============================================================================

export const falImageProvider: ImageProvider = {
  id: 'fal-ai',
  name: 'fal.ai (Flux)',
  models: [
    {
      id: 'fal-ai/flux-pro/v1.1-ultra',
      name: 'Flux Pro 1.1 Ultra',
      description: 'Highest quality, best prompt adherence, up to 2K - ~$0.06/image',
      capabilities: ['text-to-image'],
      pricing: { unit: 'image', price: 0.06 }
    },
    {
      id: 'fal-ai/flux/dev',
      name: 'Flux Dev',
      description: 'Fast, high quality generation - ~$0.025/image',
      capabilities: ['text-to-image'],
      pricing: { unit: 'image', price: 0.025 }
    },
    {
      id: 'fal-ai/flux/schnell',
      name: 'Flux Schnell',
      description: 'Fastest generation, 1-4 steps - ~$0.003/image',
      capabilities: ['text-to-image'],
      pricing: { unit: 'image', price: 0.003 }
    },
  ],

  async generate(
    input: UnifiedImageInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<ImageResult> {
    // Validate input
    if (!input.prompt || input.prompt.trim().length === 0) {
      return { success: false, error: 'Prompt cannot be empty' };
    }

    try {
      // Configure fal client with the provided key
      fal.config({ credentials: credentials.apiKey });

      const falInput: Record<string, any> = {
        prompt: input.prompt.trim(),
        image_size: mapImageSize(input.aspect_ratio, input.image_size),
        num_images: input.number_of_images || 1,
        output_format: 'png',
        enable_safety_checker: true,
      };

      if (input.seed !== undefined) {
        falInput.seed = input.seed;
      }

      const result = await fal.subscribe(model, {
        input: falInput,
      }) as any;

      // fal.ai returns: { images: [{ url, width, height, content_type }], seed }
      const images = result.data?.images || result.images || [];

      if (images.length === 0) {
        return { success: false, error: 'No image was generated. Please try a different prompt.' };
      }

      // Download images to local disk
      const outputDir = getGeneratedImagesDir();
      const generatedImages: GeneratedImage[] = [];

      for (const img of images) {
        const ext = getExtensionFromContentType(img.content_type);
        const downloaded = await downloadToFile(img.url, outputDir, 'fal-image', ext);

        if ('error' in downloaded) {
          return { success: false, error: downloaded.error };
        }

        const encodedPath = encodeURIComponent(downloaded.file_path);
        generatedImages.push({
          image_url: `/api/generated-images/by-path?path=${encodedPath}`,
          file_path: downloaded.file_path,
          filename: downloaded.filename,
          mime_type: img.content_type || 'image/png',
        });
      }

      const firstImage = generatedImages[0];
      return {
        success: true,
        image_url: firstImage.image_url,
        file_path: firstImage.file_path,
        filename: firstImage.filename,
        mime_type: firstImage.mime_type,
        ...(generatedImages.length > 1 && { images: generatedImages }),
      };

    } catch (error) {
      return { success: false, error: handleFalError(error) };
    }
  },

  async validateCredentials(credentials: ProviderCredentials): Promise<{ valid: boolean; error?: string }> {
    try {
      fal.config({ credentials: credentials.apiKey });

      // Make a lightweight queue status check to validate the key
      // fal.ai doesn't have a dedicated validation endpoint, so we check
      // if we can access the API without actually generating anything
      const result = await fal.queue.status('fal-ai/flux/schnell', {
        requestId: 'test-validation',
      }).catch((e: any) => {
        // A 401/403 means invalid key; a 404 means valid key but no such request (expected)
        if (e?.status === 401 || e?.status === 403) {
          throw new Error('Invalid API key');
        }
        // 404 or 422 is fine — means the key works but request doesn't exist
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

export default falImageProvider;

/**
 * fal.ai Image Provider
 *
 * Adapter for fal.ai's image generation API.
 * Supports Flux, Recraft, Nano Banana, Qwen, and more through a single API key.
 */

import { fal } from '@fal-ai/client';
import {
  ImageProvider,
  ImageResult,
  UnifiedImageInput,
  UnifiedEditInput,
  UnifiedReferenceInput,
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
 */
function mapImageSize(aspectRatio?: string, imageSize?: string): string | { width: number; height: number } {
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

import { readFileSync, existsSync } from 'fs';

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

export const falImageProvider: ImageProvider = {
  id: 'fal-ai',
  name: 'fal.ai',
  models: [
    // Flux models (Black Forest Labs)
    {
      id: 'fal-ai/flux-pro/v1.1-ultra',
      name: 'Flux Pro 1.1 Ultra',
      description: 'Highest quality, best prompt adherence, up to 2K resolution',
      capabilities: ['text-to-image'],
      pricing: { unit: 'image', price: 0.06 }
    },
    {
      id: 'fal-ai/flux-2-flex',
      name: 'FLUX.2 Flex',
      description: 'Latest gen with adjustable guidance and enhanced typography',
      capabilities: ['text-to-image'],
      pricing: { unit: 'image', price: 0.05 }
    },
    {
      id: 'fal-ai/flux/dev',
      name: 'Flux Dev',
      description: 'Fast, high quality generation',
      capabilities: ['text-to-image'],
      pricing: { unit: 'image', price: 0.025 }
    },
    {
      id: 'fal-ai/flux/schnell',
      name: 'Flux Schnell',
      description: 'Fastest generation, 1-4 steps',
      capabilities: ['text-to-image'],
      pricing: { unit: 'image', price: 0.003 }
    },
    // Flux Kontext (editing + reference)
    {
      id: 'fal-ai/flux-pro/kontext',
      name: 'Flux Kontext Pro',
      description: 'Text + reference images — targeted edits, scene transformations, style consistency',
      capabilities: ['text-to-image', 'image-edit', 'image-reference'],
      pricing: { unit: 'image', price: 0.04 }
    },
    // Recraft
    {
      id: 'fal-ai/recraft/v4/text-to-image',
      name: 'Recraft V4',
      description: 'Professional design and marketing, vector-quality output',
      capabilities: ['text-to-image'],
      pricing: { unit: 'image', price: 0.04 }
    },
    // Google Nano Banana (proxied through fal.ai)
    {
      id: 'fal-ai/nano-banana-pro',
      name: 'Nano Banana Pro (Google)',
      description: 'Google\'s image generation with enhanced typography',
      capabilities: ['text-to-image', 'image-edit'],
      pricing: { unit: 'image', price: 0.04 }
    },
    // Qwen
    {
      id: 'fal-ai/qwen-image',
      name: 'Qwen Image',
      description: 'Complex text rendering and precise editing capabilities',
      capabilities: ['text-to-image'],
      pricing: { unit: 'image', price: 0.03 }
    },
  ],

  async generate(
    input: UnifiedImageInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<ImageResult> {
    if (!input.prompt || input.prompt.trim().length === 0) {
      return { success: false, error: 'Prompt cannot be empty' };
    }

    try {
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

      const images = result.data?.images || result.images || [];

      if (images.length === 0) {
        return { success: false, error: 'No image was generated. Please try a different prompt.' };
      }

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

  async edit(
    input: UnifiedEditInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<ImageResult> {
    if (!input.source_image) {
      return { success: false, error: 'Source image path is required for editing' };
    }

    const dataUri = imageToDataUri(input.source_image);
    if (!dataUri) {
      return { success: false, error: `Source image not found: ${input.source_image}` };
    }

    try {
      fal.config({ credentials: credentials.apiKey });

      // Use Nano Banana Pro edit or Flux Kontext for editing
      const editModel = model.includes('kontext')
        ? model
        : model.includes('nano-banana')
          ? 'fal-ai/nano-banana-pro/edit'
          : 'fal-ai/flux-pro/kontext'; // Default to Kontext for editing

      const falInput: Record<string, any> = {
        prompt: (input.prompt || '').trim(),
        image_url: dataUri,
      };

      if (input.seed !== undefined) {
        falInput.seed = input.seed;
      }

      const result = await fal.subscribe(editModel, {
        input: falInput,
      }) as any;

      const images = result.data?.images || result.images || [];
      if (images.length === 0) {
        return { success: false, error: 'No edited image was generated.' };
      }

      const outputDir = getGeneratedImagesDir();
      const img = images[0];
      const ext = getExtensionFromContentType(img.content_type);
      const downloaded = await downloadToFile(img.url, outputDir, 'fal-edit', ext);

      if ('error' in downloaded) {
        return { success: false, error: downloaded.error };
      }

      const encodedPath = encodeURIComponent(downloaded.file_path);
      return {
        success: true,
        image_url: `/api/generated-images/by-path?path=${encodedPath}`,
        file_path: downloaded.file_path,
        filename: downloaded.filename,
        mime_type: img.content_type || 'image/png',
      };
    } catch (error) {
      return { success: false, error: handleFalError(error) };
    }
  },

  async generateWithReference(
    input: UnifiedReferenceInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<ImageResult> {
    if (!input.reference_images || input.reference_images.length === 0) {
      return { success: false, error: 'At least one reference image is required' };
    }

    try {
      fal.config({ credentials: credentials.apiKey });

      // Use Flux Kontext for reference-based generation
      const kontextModel = 'fal-ai/flux-pro/kontext';

      // Convert first reference image to data URI
      const refDataUri = imageToDataUri(input.reference_images[0].path);
      if (!refDataUri) {
        return { success: false, error: `Reference image not found: ${input.reference_images[0].path}` };
      }

      const falInput: Record<string, any> = {
        prompt: input.prompt.trim(),
        image_url: refDataUri,
      };

      if (input.seed !== undefined) {
        falInput.seed = input.seed;
      }

      const result = await fal.subscribe(kontextModel, {
        input: falInput as any,
      }) as any;

      const images = result.data?.images || result.images || [];
      if (images.length === 0) {
        return { success: false, error: 'No image was generated with reference.' };
      }

      const outputDir = getGeneratedImagesDir();
      const generatedImages: GeneratedImage[] = [];

      for (const img of images) {
        const ext = getExtensionFromContentType(img.content_type);
        const downloaded = await downloadToFile(img.url, outputDir, 'fal-ref', ext);

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

      await fal.queue.status('fal-ai/flux/schnell', {
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

export default falImageProvider;

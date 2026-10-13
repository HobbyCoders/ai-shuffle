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
  applyExtraParams,
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
// Edit routing
// ============================================================================

/** The input field a fal.ai edit endpoint reads its source image from. */
export type EditImageField = 'image_url' | 'image_urls';

export interface EditRoute {
  endpoint: string;
  field: EditImageField;
}

/**
 * Endpoint and image field for the edit families listed above, or null for any
 * other fal.ai endpoint. Nano Banana edit endpoints take a list in `image_urls`
 * (sending `image_url` fails with "422 body.image_urls: Field required");
 * Kontext takes a single `image_url`.
 */
export function knownEditRoute(model: string): EditRoute | null {
  if (model.includes('kontext')) return { endpoint: model, field: 'image_url' };
  if (model === 'fal-ai/nano-banana-2' || model === 'fal-ai/nano-banana-2/edit') {
    return { endpoint: 'fal-ai/nano-banana-2/edit', field: 'image_urls' };
  }
  if (model.includes('nano-banana')) return { endpoint: 'fal-ai/nano-banana-pro/edit', field: 'image_urls' };
  return null;
}

/** The source-image field declared by a fal.ai endpoint's OpenAPI input schema. */
export function editImageFieldFromOpenapi(openapi: any): EditImageField | null {
  const schemas = openapi?.components?.schemas;
  if (!schemas || typeof schemas !== 'object') return null;
  for (const [name, schema] of Object.entries<any>(schemas)) {
    if (!name.endsWith('Input')) continue;
    const props = schema?.properties || {};
    if ('image_urls' in props) return 'image_urls';
    if ('image_url' in props) return 'image_url';
  }
  return null;
}

/** The request body for an edit, with the source image in the field the endpoint reads. */
export function buildEditInput(field: EditImageField, prompt: string, dataUri: string): Record<string, any> {
  return field === 'image_urls'
    ? { prompt, image_urls: [dataUri] }
    : { prompt, image_url: dataUri };
}

const FAL_MODELS_API = 'https://api.fal.ai/v1/models';

function configuredEditModelNote(): string {
  const configured = process.env.IMAGE_EDIT_MODEL || process.env.IMAGE_MODEL;
  return configured
    ? ` The configured image edit model is '${configured}'; omit model to use it.`
    : ' Omit model to use the configured one.';
}

function configuredImageModelNote(): string {
  const configured = process.env.IMAGE_MODEL;
  return configured
    ? ` The configured image model is '${configured}'; omit model to use it.`
    : ' Omit model to use the configured one.';
}

/**
 * The published OpenAPI document of a fal.ai catalog endpoint, or a plain
 * refusal: a model fal.ai does not know (for example another provider's id)
 * is never sent to fal.ai to fail there with an unrelated error.
 */
async function catalogOpenapi(model: string, note: string, action: string): Promise<{ openapi: any } | { error: string }> {
  try {
    const url = `${FAL_MODELS_API}?endpoint_id=${encodeURIComponent(model)}&expand=openapi-3.0`;
    const response = await fetch(url);
    if (!response.ok) {
      return { error: `Could not read the fal.ai input schema for '${model}' (HTTP ${response.status}). No ${action} was attempted.` };
    }
    const body: any = await response.json();
    const entry = (body?.models || []).find((m: any) => m?.endpoint_id === model);
    if (!entry) {
      return { error: `'${model}' is not a fal.ai model, and image ${action} here runs on fal.ai.${note}` };
    }
    return { openapi: entry.openapi };
  } catch (error) {
    return { error: `Could not read the fal.ai input schema for '${model}': ${error instanceof Error ? error.message : 'request failed'}. No ${action} was attempted.` };
  }
}

/**
 * Route an edit to a fal.ai catalog endpoint (any model picked in Settings,
 * e.g. 'openai/gpt-image-2.5/flare/edit') by reading its published input
 * schema. A model fal.ai does not know is refused plainly instead of being
 * sent to the wrong endpoint.
 */
export async function catalogEditRoute(model: string): Promise<EditRoute | { error: string }> {
  const found = await catalogOpenapi(model, configuredEditModelNote(), 'editing');
  if ('error' in found) return found;
  const field = editImageFieldFromOpenapi(found.openapi);
  if (!field) {
    return { error: `fal.ai model '${model}' does not take a source image, so it cannot edit one.${configuredEditModelNote()}` };
  }
  return { endpoint: model, field };
}

/** The source-image fields a fal.ai endpoint's input schema requires. */
export function requiredImageFieldsFromOpenapi(openapi: any): string[] {
  const schemas = openapi?.components?.schemas;
  if (!schemas || typeof schemas !== 'object') return [];
  for (const [name, schema] of Object.entries<any>(schemas)) {
    if (!name.endsWith('Input')) continue;
    const required: unknown = schema?.required;
    return Array.isArray(required) ? required.filter((f) => f === 'image_url' || f === 'image_urls') : [];
  }
  return [];
}

/**
 * Check a model outside the built-in list before text-to-image generation:
 * it must be a fal.ai catalog endpoint, and not one that needs a source
 * image (an edit endpoint called without one fails with a 422).
 */
export async function catalogGenerateCheck(model: string): Promise<{ error: string } | null> {
  const found = await catalogOpenapi(model, configuredImageModelNote(), 'generation');
  if ('error' in found) return found;
  if (requiredImageFieldsFromOpenapi(found.openapi).length) {
    return { error: `fal.ai model '${model}' edits an existing image and needs a source image; use editImage or generateWithReference for it.${configuredImageModelNote()}` };
  }
  return null;
}

/**
 * Endpoint and input for reference-based generation. The built-in families
 * keep their endpoints; any other model (a catalog pick in Settings) goes to
 * its own endpoint with the image field its input schema declares, and is
 * refused plainly when it takes no image, never swapped for Kontext.
 */
export async function referenceRoute(
  model: string,
  prompt: string,
  refs: string[],
): Promise<{ endpoint: string; input: Record<string, any> } | { error: string }> {
  const multi = refs.length > 1;
  if (model === 'fal-ai/nano-banana-2' || model === 'fal-ai/nano-banana-2/edit') {
    return { endpoint: 'fal-ai/nano-banana-2/edit', input: { prompt, image_urls: refs } };
  }
  if (model.includes('nano-banana')) {
    return { endpoint: 'fal-ai/nano-banana-pro/edit', input: { prompt, image_urls: refs } };
  }
  if (model.includes('kontext')) {
    // Kontext has a dedicated multi-image endpoint
    return multi
      ? { endpoint: 'fal-ai/flux-pro/kontext/multi', input: { prompt, image_urls: refs } }
      : { endpoint: model, input: { prompt, image_url: refs[0] } };
  }
  const found = await catalogOpenapi(model, configuredEditModelNote(), 'generation from references');
  if ('error' in found) return found;
  const field = editImageFieldFromOpenapi(found.openapi);
  if (!field) {
    return { error: `fal.ai model '${model}' does not take a reference image.${configuredEditModelNote()}` };
  }
  if (field === 'image_url' && multi) {
    return { error: `fal.ai model '${model}' takes one reference image; ${refs.length} were given.` };
  }
  return { endpoint: model, input: field === 'image_urls' ? { prompt, image_urls: refs } : { prompt, image_url: refs[0] } };
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
    {
      id: 'fal-ai/nano-banana-2',
      name: 'Nano Banana 2 (Google)',
      description: 'Gemini 3.1 Flash — reasoning-guided, 4x faster, excellent text rendering',
      capabilities: ['text-to-image', 'image-edit'],
      pricing: { unit: 'image', price: 0.08 }
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
        input: applyExtraParams(falInput, input.extra_params),
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

      // Route to the selected model's edit endpoint, with the source image in
      // the field that endpoint reads. Any other fal.ai model (a catalog pick
      // in Settings) is used as-is, never silently swapped for Kontext.
      const route = knownEditRoute(model) ?? await catalogEditRoute(model);
      if ('error' in route) {
        return { success: false, error: route.error };
      }
      const editModel = route.endpoint;
      const falInput = buildEditInput(route.field, (input.prompt || '').trim(), dataUri);

      if (input.seed !== undefined) {
        falInput.seed = input.seed;
      }

      const result = await fal.subscribe(editModel, {
        input: applyExtraParams(falInput, input.extra_params),
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

      // Convert every reference image to data URIs up front
      const refDataUris: string[] = [];
      for (const ref of input.reference_images) {
        const dataUri = imageToDataUri(ref.path);
        if (!dataUri) {
          return { success: false, error: `Reference image not found: ${ref.path}` };
        }
        refDataUris.push(dataUri);
      }

      // Route to the selected model's reference endpoint; respecting the
      // caller's model is required so the configured edit model is invoked.
      const route = await referenceRoute(model, input.prompt.trim(), refDataUris);
      if ('error' in route) {
        return { success: false, error: route.error };
      }
      const referenceModel = route.endpoint;
      const falInput: Record<string, any> = route.input;

      if (input.seed !== undefined) {
        falInput.seed = input.seed;
      }

      const result = await fal.subscribe(referenceModel, {
        input: applyExtraParams(falInput, input.extra_params) as any,
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

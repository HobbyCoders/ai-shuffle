/**
 * Image Editing Tool - fal.ai Provider
 *
 * Edit existing images using AI via fal.ai:
 * - Flux Kontext Pro: fal-ai/flux-pro/kontext (targeted edits, scene transforms)
 * - Nano Banana 2 Edit: fal-ai/nano-banana-2/edit (Gemini 3.1 Flash, fast editing)
 * - Nano Banana Pro Edit: fal-ai/nano-banana-pro/edit (Google-based editing via fal.ai)
 *
 * Images are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   FAL_KEY - fal.ai API key
 *   IMAGE_EDIT_MODEL - Dedicated edit model (e.g., "fal-ai/nano-banana-2/edit", "fal-ai/flux-pro/kontext")
 *   IMAGE_MODEL - Fallback model (e.g., "fal-ai/flux-pro/kontext", "fal-ai/nano-banana-pro")
 *
 * Usage:
 *   import { editImage } from '/opt/ai-tools/dist/image-generation/editImage.js';
 *
 *   const result = await editImage({
 *     prompt: 'Add a rainbow in the sky',
 *     image_path: '/path/to/existing/image.png'
 *   });
 *
 *   if (result.success) {
 *     // result.image_url contains the URL to access the edited image
 *     // result.file_path contains the local file path
 *   }
 */

import { registry } from '../providers/registry.js';
import { ImageResult, ProviderCredentials } from '../providers/types.js';
import { parseModelParamsEnv } from '../providers/shared/fal-helpers.js';

export interface EditImageInput {
  /**
   * The editing instruction describing what changes to make to the image.
   * Be specific! Describe what to add, remove, change, or modify.
   * Max 10,000 characters.
   *
   * @example "Add a rainbow in the sky"
   * @example "Change the background to a beach sunset"
   * @example "Remove the person on the left"
   * @example "Make it look like a watercolor painting"
   */
  prompt: string;

  /**
   * Path to the image file to edit.
   * Supported formats: PNG, JPEG, GIF, WebP
   *
   * @example "/workspace/my-project/generated-images/image-123.png"
   */
  image_path: string;

  /**
   * Override the default image provider.
   * Currently only "fal-ai" is supported.
   */
  provider?: string;

  /**
   * Override the default model.
   * fal.ai: "fal-ai/flux-pro/kontext", "fal-ai/nano-banana-2/edit", "fal-ai/nano-banana-pro"
   * If not specified, uses IMAGE_EDIT_MODEL or IMAGE_MODEL env var, or defaults to "fal-ai/flux-pro/kontext"
   */
  model?: string;

  /**
   * Path to a mask image for inpainting (optional).
   * White areas indicate where to edit, black areas are preserved.
   * Only supported by some providers (OpenAI GPT Image).
   */
  mask_path?: string;

  /**
   * Aspect ratio of the output image.
   * If not specified, maintains the original aspect ratio.
   */
  aspect_ratio?: '1:1' | '16:9' | '9:16' | '4:3' | '3:4' | '2:3' | '3:2' | '4:5' | '5:4' | '21:9';

  /**
   * Output image size/resolution.
   * - "1K" = ~1024px (default, fastest)
   * - "2K" = ~2048px (higher quality)
   * - "4K" = ~4096px (highest quality, slower)
   * @default "1K"
   */
  image_size?: '1K' | '2K' | '4K';

  /**
   * Number of edited variants to generate (1-4).
   * When > 1, response will contain multiple images array.
   * @default 1
   */
  number_of_images?: 1 | 2 | 3 | 4;

  /**
   * Control generation of people in the edited image.
   * - "dont_allow": No people allowed
   * - "allow_adult": Only adults allowed (default)
   * - "allow_all": All people including children
   * @default "allow_adult"
   */
  person_generation?: 'dont_allow' | 'allow_adult' | 'allow_all';
}

/** Single edited image result */
export interface EditedImage {
  /** URL to access the edited image (for display in chat) */
  image_url: string;
  /** Local file path where the edited image was saved */
  file_path: string;
  /** Filename of the edited image */
  filename: string;
  /** MIME type of the image (e.g., 'image/png') */
  mime_type: string;
}

export interface EditImageResponse {
  /** Whether the image was edited successfully */
  success: boolean;

  /** URL to access the edited image (for display in chat) - first image when multiple */
  image_url?: string;

  /** Local file path where the edited image was saved - first image when multiple */
  file_path?: string;

  /** Filename of the edited image - first image when multiple */
  filename?: string;

  /** MIME type of the image (e.g., 'image/png') - first image when multiple */
  mime_type?: string;

  /** All edited images when number_of_images > 1 */
  images?: EditedImage[];

  /** Provider that was used for editing (e.g., "fal-ai") */
  provider_used?: string;

  /** Model that was used for editing */
  model_used?: string;

  /** Error message if editing failed */
  error?: string;
}

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
 * Get model ID for the provider.
 * Prefers IMAGE_EDIT_MODEL (dedicated edit model) over IMAGE_MODEL (generation model).
 */
function getModelId(providerId: string, inputModel?: string): string {
  if (inputModel) {
    return inputModel;
  }

  // Prefer the dedicated image edit model setting
  if (process.env.IMAGE_EDIT_MODEL) {
    return process.env.IMAGE_EDIT_MODEL;
  }

  // Fall back to the general image model if it supports editing
  if (process.env.IMAGE_MODEL) {
    return process.env.IMAGE_MODEL;
  }

  // Provider defaults from registry
  const provider = registry.getImageProvider(providerId);
  if (provider && provider.models.length > 0) {
    // Find first model with image-edit capability
    const editModel = provider.models.find(m => m.capabilities.includes('image-edit'));
    if (editModel) return editModel.id;
    return provider.models[0].id;
  }

  return 'fal-ai/flux-pro/kontext';
}

/**
 * Convert provider result to our response format, including provider metadata
 */
function toResponse(
  result: ImageResult,
  providerId: string,
  modelId: string
): EditImageResponse {
  if (!result.success) {
    return { success: false, error: result.error };
  }

  return {
    success: true,
    image_url: result.image_url,
    file_path: result.file_path,
    filename: result.filename,
    mime_type: result.mime_type,
    ...(result.images && { images: result.images as EditedImage[] }),
    provider_used: providerId,
    model_used: modelId
  };
}

/**
 * Edit an existing image using AI.
 *
 * Uses fal.ai as the image editing provider. Supports Flux Kontext and
 * Nano Banana Pro edit models.
 *
 * The edited image is saved to disk and a URL is returned for display in the chat UI.
 * This avoids context window limitations with large base64 strings.
 *
 * @param input - The editing parameters
 * @returns The edited image URL and file path, or error details
 *
 * @example
 * ```typescript
 * // Edit using default provider
 * const result = await editImage({
 *   prompt: 'Add a cat sitting on the couch',
 *   image_path: '/path/to/living-room.png'
 * });
 *
 * // Edit using specific model
 * const result = await editImage({
 *   prompt: 'Make it black and white',
 *   image_path: '/path/to/image.png',
 *   model: 'fal-ai/nano-banana-pro'
 * });
 *
 * if (result.success) {
 *   console.log('Edited image URL:', result.image_url);
 *   console.log('Saved to:', result.file_path);
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export async function editImage(input: EditImageInput): Promise<EditImageResponse> {
  // Determine provider
  const providerId = getProviderId(input.provider);
  const provider = registry.getImageProvider(providerId);

  if (!provider) {
    const availableProviders = registry.listImageProviders().map(p => p.id);
    return {
      success: false,
      error: `Image provider '${providerId}' not found. Available providers: ${availableProviders.join(', ')}`
    };
  }

  // Check if provider supports editing
  if (!provider.edit) {
    return {
      success: false,
      error: `Provider '${provider.name}' does not support image editing. Try a different provider.`
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

  // A model outside the built-in list is a fal.ai catalog model (Settings
  // offers the whole catalog, e.g. 'openai/gpt-image-2.5/flare/edit').
  // provider.edit checks it against the catalog and refuses one fal.ai does
  // not offer, so it is not rejected here.
  if (modelInfo && !modelInfo.capabilities.includes('image-edit')) {
    return {
      success: false,
      error: `Model '${modelId}' does not support image editing. Try a different model.`
    };
  }

  // Build credentials
  const credentials: ProviderCredentials = { apiKey };

  // Delegate to provider
  const result = await provider.edit(
    {
      prompt: input.prompt,
      source_image: input.image_path,
      mask_image: input.mask_path,
      aspect_ratio: input.aspect_ratio,
      image_size: input.image_size,
      number_of_images: input.number_of_images,
      person_generation: input.person_generation,
      extra_params: parseModelParamsEnv('IMAGE_EDIT_MODEL_PARAMS')
    },
    credentials,
    modelId
  );

  return toResponse(result, providerId, modelId);
}

export default editImage;

/**
 * Image Editing Tool - Unified Provider Interface
 *
 * Edit existing images using AI from multiple providers:
 * - Google Gemini (Nano Banana): gemini-2.5-flash-image, gemini-3-pro-image-preview
 * - OpenAI GPT Image: gpt-image-1 (GPT-4o native image editing)
 *
 * The provider and model are selected based on environment variables or explicit input.
 * Images are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   IMAGE_PROVIDER - Provider ID (e.g., "google-gemini", "openai-gpt-image")
 *   IMAGE_API_KEY - API key for the selected provider
 *   IMAGE_MODEL - Model to use (e.g., "gemini-2.5-flash-image", "gpt-image-1")
 *
 *   Legacy (backwards compatible):
 *   GEMINI_API_KEY - Google AI API key (used if IMAGE_API_KEY not set for Gemini)
 *   OPENAI_API_KEY - OpenAI API key (used if IMAGE_API_KEY not set for GPT Image)
 *   GEMINI_MODEL - Gemini model (used if IMAGE_MODEL not set)
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
   * Available: "google-gemini", "openai-gpt-image"
   * If not specified, uses IMAGE_PROVIDER env var or defaults to "google-gemini"
   */
  provider?: string;

  /**
   * Override the default model for the selected provider.
   * Google Gemini: "gemini-2.5-flash-image", "gemini-3-pro-image-preview"
   * OpenAI: "gpt-image-1"
   * If not specified, uses IMAGE_MODEL/GEMINI_MODEL env var or provider default
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

  /** Error message if editing failed */
  error?: string;
}

/**
 * Determine the provider ID to use based on input and environment
 */
function getProviderId(inputProvider?: string): string {
  // Explicit input takes priority
  if (inputProvider) {
    return inputProvider;
  }

  // Check environment variables
  if (process.env.IMAGE_PROVIDER) {
    return process.env.IMAGE_PROVIDER;
  }

  // Default to Google Gemini
  return 'google-gemini';
}

/**
 * Get API key for the specified provider
 */
function getApiKey(providerId: string): string {
  // Check provider-specific env var first
  if (process.env.IMAGE_API_KEY) {
    return process.env.IMAGE_API_KEY;
  }

  // Legacy fallbacks based on provider
  if (providerId === 'google-gemini') {
    return process.env.GEMINI_API_KEY || '';
  }

  if (providerId === 'openai-gpt-image') {
    return process.env.OPENAI_API_KEY || '';
  }

  return '';
}

/**
 * Get model ID for the specified provider
 */
function getModelId(providerId: string, inputModel?: string): string {
  // Explicit input takes priority
  if (inputModel) {
    return inputModel;
  }

  // Check environment variables
  if (process.env.IMAGE_MODEL) {
    return process.env.IMAGE_MODEL;
  }

  // Legacy fallbacks
  if (providerId === 'google-gemini' && process.env.GEMINI_MODEL) {
    return process.env.GEMINI_MODEL;
  }

  // Provider defaults
  const provider = registry.getImageProvider(providerId);
  if (provider && provider.models.length > 0) {
    return provider.models[0].id;
  }

  // Ultimate fallback
  if (providerId === 'google-gemini') {
    return 'gemini-2.5-flash-image';
  }

  if (providerId === 'openai-gpt-image') {
    return 'gpt-image-1';
  }

  return '';
}

/**
 * Convert provider result to our response format
 */
function toResponse(result: ImageResult): EditImageResponse {
  if (!result.success) {
    return { success: false, error: result.error };
  }

  return {
    success: true,
    image_url: result.image_url,
    file_path: result.file_path,
    filename: result.filename,
    mime_type: result.mime_type,
    ...(result.images && { images: result.images as EditedImage[] })
  };
}

/**
 * Edit an existing image using AI.
 *
 * Uses the configured provider (Google Gemini, OpenAI GPT Image, etc.) or allows
 * explicit override via the provider/model parameters.
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
 * // Edit using specific provider
 * const result = await editImage({
 *   prompt: 'Make it black and white',
 *   image_path: '/path/to/image.png',
 *   provider: 'openai-gpt-image',
 *   model: 'gpt-image-1'
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
      error: `No API key configured for ${provider.name}. Set IMAGE_API_KEY or provider-specific key.`
    };
  }

  // Get model
  const modelId = getModelId(providerId, input.model);
  const modelInfo = provider.models.find(m => m.id === modelId);

  if (!modelInfo) {
    const availableModels = provider.models.map(m => m.id);
    return {
      success: false,
      error: `Model '${modelId}' not found for ${provider.name}. Available models: ${availableModels.join(', ')}`
    };
  }

  // Check if model supports editing
  if (!modelInfo.capabilities.includes('image-edit')) {
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
      person_generation: input.person_generation
    },
    credentials,
    modelId
  );

  return toResponse(result);
}

export default editImage;

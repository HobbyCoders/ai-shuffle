/**
 * Reference-Based Image Generation Tool - fal.ai Provider
 *
 * Generate images while maintaining character/style consistency using reference images.
 * Uses Flux Kontext Pro via fal.ai for reference-based generation.
 *
 * Environment Variables:
 *   FAL_KEY - fal.ai API key
 *   IMAGE_MODEL - Model to use (optional, defaults to fal-ai/flux-pro/kontext)
 *
 * Usage:
 *   import { generateWithReference } from '/opt/ai-tools/dist/image-generation/generateWithReference.js';
 *
 *   const result = await generateWithReference({
 *     prompt: 'The character standing in a forest',
 *     reference_images: ['/path/to/character.png']
 *   });
 */

import { registry } from '../providers/registry.js';
import { ImageResult, ProviderCredentials } from '../providers/types.js';

export interface ReferenceImage {
  /**
   * Path to the reference image file.
   * Supported formats: PNG, JPEG, GIF, WebP
   */
  path: string;

  /**
   * Optional label/description for this reference.
   * Helps the model understand what aspect to reference.
   * @example "main character" or "art style reference"
   */
  label?: string;
}

export interface GenerateWithReferenceInput {
  /**
   * The text prompt describing the image to generate.
   * Reference your images by their labels or position.
   * Max 10,000 characters.
   *
   * @example "The character from the reference standing in a forest"
   * @example "Generate a new scene in the style of the reference image"
   */
  prompt: string;

  /**
   * Reference images for character/style consistency.
   * Can be simple string paths or objects with path + label.
   * Maximum 14 images (6 objects, 5 humans recommended).
   *
   * @example ["/path/to/character.png"]
   * @example [{ path: "/path/to/character.png", label: "main hero" }]
   */
  reference_images: (string | ReferenceImage)[];

  /**
   * Aspect ratio of the generated image.
   * @default "1:1"
   */
  aspect_ratio?: '1:1' | '16:9' | '9:16' | '4:3' | '3:4' | '2:3' | '3:2' | '4:5' | '5:4' | '21:9';

  /**
   * Output image size/resolution.
   * @default "1K"
   */
  image_size?: '1K' | '2K' | '4K';

  /**
   * Number of images to generate (1-4).
   * @default 1
   */
  number_of_images?: 1 | 2 | 3 | 4;

  /**
   * Control generation of people in the image.
   * @default "allow_adult"
   */
  person_generation?: 'dont_allow' | 'allow_adult' | 'allow_all';
}

/** Single generated image result */
export interface GeneratedImage {
  image_url: string;
  file_path: string;
  filename: string;
  mime_type: string;
}

export interface GenerateWithReferenceResponse {
  success: boolean;
  image_url?: string;
  file_path?: string;
  filename?: string;
  mime_type?: string;
  images?: GeneratedImage[];
  provider_used?: string;
  model_used?: string;
  error?: string;
}

/**
 * Generate an image using reference images for consistency.
 *
 * Uses fal.ai's Flux Kontext Pro for reference-based generation.
 *
 * This is useful for:
 * - Maintaining character consistency across multiple generations
 * - Applying a specific art style from a reference
 * - Placing the same subject in different environments
 * - Creating consistent product shots or brand assets
 *
 * @param input - The generation parameters including reference images
 * @returns The generated image URL and file path, or error details
 *
 * @example
 * ```typescript
 * // Use a character reference
 * const result = await generateWithReference({
 *   prompt: 'The robot character exploring a jungle',
 *   reference_images: ['/workspace/robot-character.png']
 * });
 *
 * // Use multiple references with labels
 * const result = await generateWithReference({
 *   prompt: 'The hero fighting the villain in a city',
 *   reference_images: [
 *     { path: '/workspace/hero.png', label: 'hero character' },
 *     { path: '/workspace/villain.png', label: 'villain character' }
 *   ]
 * });
 *
 * // Style transfer
 * const result = await generateWithReference({
 *   prompt: 'A mountain landscape in this art style',
 *   reference_images: [{ path: '/workspace/style.png', label: 'art style' }]
 * });
 * ```
 */
export async function generateWithReference(input: GenerateWithReferenceInput): Promise<GenerateWithReferenceResponse> {
  // Get API key from environment
  const apiKey = process.env.FAL_KEY;
  if (!apiKey) {
    return {
      success: false,
      error: 'FAL_KEY environment variable not set. Image generation is not configured.'
    };
  }

  // Get provider from registry
  const provider = registry.getImageProvider('fal-ai');
  if (!provider) {
    return {
      success: false,
      error: 'fal-ai image provider not found in registry.'
    };
  }

  if (!provider.generateWithReference) {
    return {
      success: false,
      error: 'fal-ai image provider does not support reference-based generation.'
    };
  }

  // Validate input
  if (!input.prompt || input.prompt.trim().length === 0) {
    return {
      success: false,
      error: 'Prompt cannot be empty'
    };
  }

  if (input.prompt.length > 10000) {
    return {
      success: false,
      error: 'Prompt is too long. Maximum 10,000 characters.'
    };
  }

  if (!input.reference_images || input.reference_images.length === 0) {
    return {
      success: false,
      error: 'At least one reference image is required'
    };
  }

  if (input.reference_images.length > 14) {
    return {
      success: false,
      error: 'Maximum 14 reference images allowed'
    };
  }

  // Get model
  const modelId = process.env.IMAGE_MODEL || 'fal-ai/flux-pro/kontext';

  // Normalize reference images to the unified format
  const referenceImages = input.reference_images.map(ref => {
    if (typeof ref === 'string') {
      return { path: ref };
    }
    return ref;
  });

  // Build credentials
  const credentials: ProviderCredentials = { apiKey };

  try {
    const result: ImageResult = await provider.generateWithReference(
      {
        prompt: input.prompt.trim(),
        reference_images: referenceImages,
        aspect_ratio: input.aspect_ratio,
        image_size: input.image_size,
        number_of_images: input.number_of_images,
        person_generation: input.person_generation
      },
      credentials,
      modelId
    );

    if (!result.success) {
      return { success: false, error: result.error };
    }

    return {
      success: true,
      image_url: result.image_url,
      file_path: result.file_path,
      filename: result.filename,
      mime_type: result.mime_type,
      ...(result.images && { images: result.images as GeneratedImage[] }),
      provider_used: 'fal-ai',
      model_used: modelId
    };

  } catch (error) {
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error occurred'
    };
  }
}

export default generateWithReference;

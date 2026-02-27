/**
 * Image to 3D Tool - fal.ai Provider
 *
 * Convert an image to a 3D model using fal.ai.
 * Models are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   FAL_KEY - fal.ai API key
 *   MODEL3D_MODEL - Model to use (optional, defaults to fal-ai/hunyuan-3d/v2/turbo)
 *   GENERATED_MODELS_DIR - Output directory (optional, defaults to ./generated-models)
 *
 * Usage:
 *   import { imageTo3D } from '/opt/ai-tools/dist/model3d-generation/imageTo3D.js';
 *
 *   const result = await imageTo3D({
 *     image_path: '/path/to/character.png',
 *     prompt: 'A fantasy warrior character',
 *     wait_for_completion: true
 *   });
 *
 *   if (result.success) {
 *     // result.model_url contains the URL to the GLB file
 *     // result.file_path contains the local file path
 *   }
 */

import { Model3DResponse } from './shared.js';
import { registry } from '../providers/registry.js';

export interface ImageTo3DInput {
  /**
   * Path to the source image file.
   * Supported formats: JPEG, PNG, WebP.
   * For best results, use a clear image with the object on a neutral background.
   */
  image_path: string;

  /**
   * Optional text prompt to guide the 3D generation.
   * Describe additional details about the object.
   * Max 600 characters.
   *
   * @example "A fantasy warrior with detailed armor"
   */
  prompt?: string;

  /**
   * AI model to use for generation.
   * If not specified, uses MODEL3D_MODEL env var or defaults to fal-ai/hunyuan-3d/v2/turbo.
   */
  model?: string;

  /**
   * Whether to generate textures for the model.
   * @default true
   */
  should_texture?: boolean;

  /**
   * Whether to enable PBR (Physically Based Rendering) textures.
   * Generates metallic, roughness, and normal maps.
   * @default false
   */
  enable_pbr?: boolean;

  /**
   * Symmetry mode for the generated mesh.
   * - "off": No symmetry enforcement
   * - "auto": Automatically detect and apply symmetry
   * - "on": Force bilateral symmetry
   * @default "auto"
   */
  symmetry_mode?: 'off' | 'auto' | 'on';

  /**
   * Mesh topology type.
   * - "quad": Four-sided polygons, better for subdivision
   * - "triangle": Three-sided polygons, more compatible
   * @default "triangle"
   */
  topology?: 'quad' | 'triangle';

  /**
   * Target polygon count for the mesh.
   * Higher values = more detail, larger file size.
   * Range: 100 to 300,000
   * @default 30000
   */
  target_polycount?: number;

  /**
   * Whether to wait for the generation to complete.
   * - true: Poll until complete and return the model (may take 1-5 minutes)
   * - false: Return immediately with task_id for later checking
   * @default false
   */
  wait_for_completion?: boolean;
}

export type ImageTo3DResponse = Model3DResponse;

/**
 * Convert an image to a 3D model using fal.ai.
 *
 * The model is saved to disk and a URL is returned for display.
 * 3D generation is asynchronous by default - use wait_for_completion: true
 * to wait for the result, or check the task status with getTask3D.
 *
 * @param input - The generation parameters including source image
 * @returns The generated model URL and file path, or task_id for async mode
 *
 * @example
 * ```typescript
 * const result = await imageTo3D({
 *   image_path: '/workspace/character.png',
 *   prompt: 'A cartoon character with big eyes',
 *   should_texture: true,
 *   enable_pbr: true,
 *   wait_for_completion: true
 * });
 *
 * if (result.success) {
 *   console.log('Model URL:', result.model_url);
 *   console.log('Saved to:', result.file_path);
 *   console.log('Textures:', result.texture_urls);
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export async function imageTo3D(input: ImageTo3DInput): Promise<ImageTo3DResponse> {
  // Get API key from environment
  const falKey = process.env.FAL_KEY || '';
  if (!falKey) {
    return {
      success: false,
      error: 'FAL_KEY environment variable not set. 3D generation is not configured.'
    };
  }

  // Get provider from registry
  const provider = registry.getModel3DProvider('fal-ai');
  if (!provider || !provider.imageTo3D) {
    return { success: false, error: 'fal-ai 3D provider (imageTo3D) not found in registry.' };
  }

  // Validate input
  if (!input.image_path) {
    return {
      success: false,
      error: 'image_path is required'
    };
  }

  if (input.prompt && input.prompt.length > 600) {
    return {
      success: false,
      error: 'Prompt is too long. Maximum 600 characters.'
    };
  }

  if (input.target_polycount !== undefined) {
    if (input.target_polycount < 100 || input.target_polycount > 300000) {
      return {
        success: false,
        error: 'target_polycount must be between 100 and 300,000.'
      };
    }
  }

  // Determine model - prefer IMAGE_TO_3D_MODEL, fall back to MODEL3D_MODEL
  let falModel = input.model || process.env.IMAGE_TO_3D_MODEL || process.env.MODEL3D_MODEL || '';
  if (!falModel) {
    const i2mModel = provider.models.find(m => m.capabilities.includes('image-to-3d'));
    falModel = i2mModel?.id || 'fal-ai/hunyuan-3d/v2/turbo';
  }

  try {
    const result = await provider.imageTo3D(
      {
        prompt: input.prompt || '',
        image_path: input.image_path,
        topology: input.topology,
        target_polycount: input.target_polycount
      },
      { apiKey: falKey },
      falModel
    );

    // Map Model3DResult to Model3DResponse
    return {
      success: result.success,
      task_id: result.task_id,
      model_url: result.model_urls?.glb,
      model_urls: result.model_urls,
      file_path: result.file_path,
      filename: result.filename,
      status: result.status,
      progress: result.progress,
      thumbnail_url: result.thumbnail_url,
      error: result.error,
    };

  } catch (error) {
    console.error('[imageTo3D] Error:', error);
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error occurred'
    };
  }
}

export default imageTo3D;

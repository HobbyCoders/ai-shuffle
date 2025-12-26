/**
 * Image to 3D Tool - Meshy AI
 *
 * Convert an image to a 3D model using Meshy AI.
 * The API key is provided via environment variable.
 * Models are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   MESHY_API_KEY - Meshy AI API key (injected by AI Hub at runtime)
 *   MODEL3D_API_KEY - Alternative API key (used if MESHY_API_KEY not set)
 *   MODEL3D_MODEL - Model to use (optional, defaults to meshy-6)
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

import {
  Model3DResponse,
  MESHY_API_BASE,
  getMeshyApiKey,
  getMeshyHeaders,
  handleApiError,
  imageToDataUri,
  pollForCompletion,
  saveModelFromResponse
} from './shared.js';

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
   * - "meshy-6": Latest model, best quality (20 credits)
   * - "meshy-5": Balanced quality and speed (5 credits)
   * - "meshy-4": Fast generation (5 credits)
   * - "latest": Alias for the latest model
   * @default "meshy-6"
   */
  model?: 'meshy-4' | 'meshy-5' | 'meshy-6' | 'latest';

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
 * Convert an image to a 3D model using Meshy AI.
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
 * // Convert a character image to 3D
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
  const apiKey = getMeshyApiKey();
  if (!apiKey) {
    return {
      success: false,
      error: 'MESHY_API_KEY environment variable not set. 3D generation is not configured.'
    };
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

  // Validate target_polycount if provided
  if (input.target_polycount !== undefined) {
    if (input.target_polycount < 100 || input.target_polycount > 300000) {
      return {
        success: false,
        error: 'target_polycount must be between 100 and 300,000.'
      };
    }
  }

  // Read and encode the image
  const imageResult = imageToDataUri(input.image_path);
  if ('error' in imageResult) {
    return {
      success: false,
      error: imageResult.error
    };
  }

  // Get model from environment or use default
  const model = input.model || process.env.MODEL3D_MODEL || 'meshy-6';

  try {
    // Build request body
    const requestBody: Record<string, any> = {
      image_url: imageResult.dataUri,
      ai_model: model === 'latest' ? 'meshy-6' : model,
      should_texture: input.should_texture !== false  // Default true
    };

    // Add optional parameters
    if (input.prompt) {
      requestBody.texture_prompt = input.prompt.trim();
    }

    if (input.enable_pbr !== undefined) {
      requestBody.enable_pbr = input.enable_pbr;
    }

    if (input.symmetry_mode) {
      requestBody.symmetry_mode = input.symmetry_mode;
    }

    if (input.topology) {
      requestBody.topology = input.topology;
    }

    if (input.target_polycount !== undefined) {
      requestBody.target_polycount = input.target_polycount;
    }

    // Start the 3D generation
    const response = await fetch(`${MESHY_API_BASE}/openapi/v1/image-to-3d`, {
      method: 'POST',
      headers: getMeshyHeaders(apiKey),
      body: JSON.stringify(requestBody)
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      return handleApiError(response, errorData);
    }

    const result = await response.json() as any;
    const taskId = result.result;

    if (!taskId) {
      return {
        success: false,
        error: 'Failed to start 3D generation: no task ID returned.'
      };
    }

    // If not waiting for completion, return the task ID immediately
    if (!input.wait_for_completion) {
      return {
        success: false,  // Not yet complete
        task_id: taskId,
        status: 'PENDING',
        progress: 0
      };
    }

    // Poll for completion
    console.log(`[imageTo3D] Task ${taskId} started, waiting for completion...`);
    const pollResult = await pollForCompletion(taskId, 'image-to-3d', apiKey);

    if (!pollResult.success) {
      return pollResult;
    }

    // Download and save the model
    return await saveModelFromResponse(pollResult, 'i2m');

  } catch (error) {
    console.error('[imageTo3D] Error:', error);
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error occurred'
    };
  }
}

export default imageTo3D;

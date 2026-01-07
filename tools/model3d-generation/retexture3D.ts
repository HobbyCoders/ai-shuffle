/**
 * Retexture 3D Tool - Meshy AI
 *
 * Apply AI-generated textures to an existing 3D model using Meshy AI.
 * Can retexture models from previous Meshy tasks or from uploaded files.
 *
 * Environment Variables:
 *   MESHY_API_KEY - Meshy AI API key (injected by AI Hub at runtime)
 *   MODEL3D_API_KEY - Alternative API key (used if MESHY_API_KEY not set)
 *   MODEL3D_MODEL - Model to use (optional, defaults to meshy-5)
 *   GENERATED_MODELS_DIR - Output directory (optional, defaults to ./generated-models)
 *
 * Usage:
 *   import { retexture3D } from '/opt/ai-tools/dist/model3d-generation/retexture3D.js';
 *
 *   // Retexture a model from a previous task
 *   const result = await retexture3D({
 *     model_path_or_task_id: 'task-id-from-previous-generation',
 *     style_prompt: 'Medieval stone texture with moss',
 *     wait_for_completion: true
 *   });
 *
 *   // Or use a style image
 *   const result = await retexture3D({
 *     model_path_or_task_id: '/path/to/model.glb',
 *     style_image: '/path/to/style.png',
 *     enable_pbr: true,
 *     wait_for_completion: true
 *   });
 */

import {
  Model3DResponse,
  MESHY_API_BASE,
  getMeshyApiKey,
  getMeshyHeaders,
  handleApiError,
  imageToDataUri,
  modelToDataUri,
  pollForCompletion,
  saveModelFromResponse
} from './shared.js';

export interface Retexture3DInput {
  /**
   * Either a Meshy task ID from a previous generation, or a path to a local model file.
   * - Task ID: Use the task_id from textTo3D or imageTo3D
   * - File path: Path to a .glb, .fbx, or .obj file
   */
  model_path_or_task_id: string;

  /**
   * Text prompt describing the desired texture style.
   * Either style_prompt or style_image is required.
   * Max 600 characters.
   *
   * @example "Weathered bronze with green patina"
   * @example "Cartoon style with bright colors and cel shading"
   */
  style_prompt?: string;

  /**
   * Path to a style reference image.
   * Either style_prompt or style_image is required.
   * The texture will match the style of this image.
   */
  style_image?: string;

  /**
   * AI model to use for retexturing.
   * - "meshy-5": Balanced quality and speed
   * - "meshy-4": Fast generation
   * - "latest": Alias for the latest model
   * @default "meshy-5"
   */
  model?: 'meshy-4' | 'meshy-5' | 'latest';

  /**
   * Whether to enable PBR (Physically Based Rendering) textures.
   * Generates metallic, roughness, and normal maps.
   * @default false
   */
  enable_pbr?: boolean;

  /**
   * Whether to wait for the generation to complete.
   * - true: Poll until complete and return the model (may take 1-3 minutes)
   * - false: Return immediately with task_id for later checking
   * @default false
   */
  wait_for_completion?: boolean;
}

export type Retexture3DResponse = Model3DResponse;

/**
 * Check if a string looks like a Meshy task ID
 */
function isTaskId(str: string): boolean {
  // Meshy task IDs are alphanumeric strings, not file paths
  return !str.includes('/') && !str.includes('\\') && !str.includes('.');
}

/**
 * Apply AI-generated textures to an existing 3D model.
 *
 * The retextured model is saved to disk and a URL is returned for display.
 * Retexturing is asynchronous by default - use wait_for_completion: true
 * to wait for the result, or check the task status with getTask3D.
 *
 * @param input - The retexturing parameters
 * @returns The retextured model URL and file path, or task_id for async mode
 *
 * @example
 * ```typescript
 * // Retexture with a text prompt
 * const result = await retexture3D({
 *   model_path_or_task_id: 'abc123-task-id',
 *   style_prompt: 'Futuristic chrome with neon accents',
 *   enable_pbr: true,
 *   wait_for_completion: true
 * });
 *
 * // Retexture with a style image
 * const result = await retexture3D({
 *   model_path_or_task_id: '/workspace/model.glb',
 *   style_image: '/workspace/reference.png',
 *   wait_for_completion: true
 * });
 *
 * if (result.success) {
 *   console.log('Model URL:', result.model_url);
 *   console.log('Textures:', result.texture_urls);
 * }
 * ```
 */
export async function retexture3D(input: Retexture3DInput): Promise<Retexture3DResponse> {
  // Get API key from environment
  const apiKey = getMeshyApiKey();
  if (!apiKey) {
    return {
      success: false,
      error: 'MESHY_API_KEY environment variable not set. 3D generation is not configured.'
    };
  }

  // Validate input
  if (!input.model_path_or_task_id) {
    return {
      success: false,
      error: 'model_path_or_task_id is required'
    };
  }

  if (!input.style_prompt && !input.style_image) {
    return {
      success: false,
      error: 'Either style_prompt or style_image is required'
    };
  }

  if (input.style_prompt && input.style_prompt.length > 600) {
    return {
      success: false,
      error: 'style_prompt is too long. Maximum 600 characters.'
    };
  }

  // Get model from environment or use default
  // Valid models for retexture: meshy-4, meshy-5, latest
  const envModel = process.env.MODEL3D_MODEL;
  // Map meshy-6 to latest for backwards compatibility
  const normalizedEnvModel = envModel === 'meshy-6' ? 'latest' : envModel;
  const model = input.model || normalizedEnvModel || 'meshy-5';

  try {
    // Build request body
    const requestBody: Record<string, any> = {
      ai_model: model  // Valid: meshy-4, meshy-5, latest
    };

    // Determine if input is a task ID or file path
    if (isTaskId(input.model_path_or_task_id)) {
      requestBody.input_task_id = input.model_path_or_task_id;
    } else {
      // Read and encode the model file
      const modelResult = modelToDataUri(input.model_path_or_task_id);
      if ('error' in modelResult) {
        return {
          success: false,
          error: modelResult.error
        };
      }
      requestBody.model_url = modelResult.dataUri;
    }

    // Add style prompt or image
    if (input.style_prompt) {
      requestBody.text_style_prompt = input.style_prompt.trim();
    }

    if (input.style_image) {
      const imageResult = imageToDataUri(input.style_image);
      if ('error' in imageResult) {
        return {
          success: false,
          error: imageResult.error
        };
      }
      requestBody.image_style_url = imageResult.dataUri;
    }

    // Add optional parameters
    if (input.enable_pbr !== undefined) {
      requestBody.enable_pbr = input.enable_pbr;
    }

    // Start the retexturing
    const response = await fetch(`${MESHY_API_BASE}/openapi/v1/retexture`, {
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
        error: 'Failed to start retexturing: no task ID returned.'
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
    console.log(`[retexture3D] Task ${taskId} started, waiting for completion...`);
    const pollResult = await pollForCompletion(taskId, 'retexture', apiKey);

    if (!pollResult.success) {
      return pollResult;
    }

    // Download and save the model
    return await saveModelFromResponse(pollResult, 'rtx');

  } catch (error) {
    console.error('[retexture3D] Error:', error);
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error occurred'
    };
  }
}

export default retexture3D;

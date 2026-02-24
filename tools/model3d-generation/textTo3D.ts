/**
 * Text to 3D Tool - fal.ai Provider
 *
 * Generate a 3D model from a text prompt using fal.ai.
 * Models are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   FAL_KEY - fal.ai API key
 *   MODEL3D_MODEL - Model to use (optional, defaults to fal-ai/meshy/v6/text-to-3d)
 *   GENERATED_MODELS_DIR - Output directory (optional, defaults to ./generated-models)
 *
 * Usage:
 *   import { textTo3D } from '/opt/ai-tools/dist/model3d-generation/textTo3D.js';
 *
 *   const result = await textTo3D({
 *     prompt: 'A medieval castle with stone walls and towers',
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

export interface TextTo3DInput {
  /**
   * Text prompt describing the 3D model to generate.
   * Be descriptive! Include details about shape, style, materials, and colors.
   * Max 600 characters.
   *
   * @example "A medieval castle with stone walls and tall towers"
   * @example "A cute cartoon robot with big eyes and chrome body"
   */
  prompt: string;

  /**
   * Art style for the generated model.
   * - "realistic": Photorealistic textures and proportions
   * - "sculpture": Stylized, artistic interpretation
   * @default "realistic"
   */
  art_style?: 'realistic' | 'sculpture';

  /**
   * AI model to use for generation.
   * If not specified, uses MODEL3D_MODEL env var or defaults to fal-ai/meshy/v6/text-to-3d.
   */
  model?: string;

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
   * Symmetry mode for the generated model.
   * - "auto": Automatically detect and apply symmetry
   * - "on": Force symmetrical generation
   * - "off": No symmetry enforcement
   * @default "auto"
   */
  symmetry_mode?: 'auto' | 'on' | 'off';

  /**
   * Whether to apply remeshing for cleaner topology.
   * @default true
   */
  should_remesh?: boolean;

  /**
   * Pose mode for humanoid/character models.
   * - "a-pose": Arms at ~45 degrees (good for rigging)
   * - "t-pose": Arms horizontal (standard for rigging)
   * - "": No specific pose
   * @default ""
   */
  pose_mode?: 'a-pose' | 't-pose' | '';

  /**
   * Whether to wait for the generation to complete.
   * - true: Poll until complete and return the model (may take 1-5 minutes)
   * - false: Return immediately with task_id for later checking
   * @default false
   */
  wait_for_completion?: boolean;
}

export type TextTo3DResponse = Model3DResponse;

/**
 * Generate a 3D model from a text prompt using fal.ai.
 *
 * The model is saved to disk and a URL is returned for display.
 * 3D generation is asynchronous by default - use wait_for_completion: true
 * to wait for the result, or check the task status with getTask3D.
 *
 * @param input - The generation parameters
 * @returns The generated model URL and file path, or task_id for async mode
 *
 * @example
 * ```typescript
 * // Sync mode - waits for completion
 * const result = await textTo3D({
 *   prompt: 'A medieval sword with ornate handle',
 *   topology: 'quad',
 *   target_polycount: 50000,
 *   wait_for_completion: true
 * });
 *
 * if (result.success) {
 *   console.log('Model URL:', result.model_url);
 *   console.log('Saved to:', result.file_path);
 * }
 * ```
 */
export async function textTo3D(input: TextTo3DInput): Promise<TextTo3DResponse> {
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
  if (!provider) {
    return { success: false, error: 'fal-ai 3D provider not found in registry.' };
  }

  // Validate input
  if (!input.prompt || input.prompt.trim().length === 0) {
    return {
      success: false,
      error: 'Prompt cannot be empty. Describe the 3D model you want to generate.'
    };
  }

  if (input.prompt.length > 600) {
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

  // Determine model
  const falModel = input.model || process.env.MODEL3D_MODEL || provider.models[0]?.id || 'fal-ai/meshy/v6/text-to-3d';

  try {
    const result = await provider.textTo3D(
      {
        prompt: input.prompt.trim(),
        art_style: input.art_style,
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
    console.error('[textTo3D] Error:', error);
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error occurred'
    };
  }
}

export default textTo3D;

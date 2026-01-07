/**
 * Get Task 3D Tool - Meshy AI
 *
 * Get the status of a 3D generation task from Meshy AI.
 * Use this to check on tasks started with wait_for_completion: false.
 *
 * Environment Variables:
 *   MESHY_API_KEY - Meshy AI API key (injected by AI Hub at runtime)
 *   MODEL3D_API_KEY - Alternative API key (used if MESHY_API_KEY not set)
 *   GENERATED_MODELS_DIR - Output directory (optional, defaults to ./generated-models)
 *
 * Usage:
 *   import { getTask3D } from '/opt/ai-tools/dist/model3d-generation/getTask3D.js';
 *
 *   // Start a task without waiting
 *   const task = await textTo3D({
 *     prompt: 'A medieval castle',
 *     wait_for_completion: false
 *   });
 *
 *   // Check status later
 *   const status = await getTask3D({
 *     task_id: task.task_id,
 *     task_type: 'text-to-3d'
 *   });
 *
 *   if (status.success) {
 *     console.log('Model ready:', status.model_url);
 *   } else if (status.status === 'IN_PROGRESS') {
 *     console.log('Progress:', status.progress, '%');
 *   }
 */

import {
  Model3DResponse,
  TaskType,
  TASK_ENDPOINTS,
  MESHY_API_BASE,
  getMeshyApiKey,
  getMeshyHeaders,
  handleApiError,
  parseTaskResponse,
  saveModelFromResponse
} from './shared.js';

export interface GetTask3DInput {
  /**
   * The task ID to check.
   * This is returned by textTo3D, imageTo3D, retexture3D, rig3D, or animate3D.
   */
  task_id: string;

  /**
   * The type of task.
   * Must match the tool that created the task.
   */
  task_type: TaskType;

  /**
   * Whether to download and save the model if complete.
   * @default true
   */
  download_if_complete?: boolean;
}

export type GetTask3DResponse = Model3DResponse;

/**
 * Get the status of a 3D generation task.
 *
 * Use this to check on tasks that were started with wait_for_completion: false.
 * If the task is complete, optionally downloads and saves the model.
 *
 * @param input - The task parameters
 * @returns The task status, and model info if complete
 *
 * @example
 * ```typescript
 * // Start a long-running task
 * const task = await textTo3D({
 *   prompt: 'A detailed dragon statue',
 *   model: 'meshy-6',
 *   wait_for_completion: false
 * });
 *
 * console.log('Task started:', task.task_id);
 *
 * // Poll for completion
 * let status;
 * do {
 *   await new Promise(r => setTimeout(r, 5000));  // Wait 5 seconds
 *   status = await getTask3D({
 *     task_id: task.task_id,
 *     task_type: 'text-to-3d'
 *   });
 *   console.log(`Progress: ${status.progress}%`);
 * } while (status.status === 'IN_PROGRESS' || status.status === 'PENDING');
 *
 * if (status.success) {
 *   console.log('Model ready:', status.model_url);
 *   console.log('Saved to:', status.file_path);
 * } else {
 *   console.error('Failed:', status.error);
 * }
 * ```
 */
export async function getTask3D(input: GetTask3DInput): Promise<GetTask3DResponse> {
  // Get API key from environment
  const apiKey = getMeshyApiKey();
  if (!apiKey) {
    return {
      success: false,
      error: 'MESHY_API_KEY environment variable not set. 3D generation is not configured.'
    };
  }

  // Validate input
  if (!input.task_id) {
    return {
      success: false,
      error: 'task_id is required'
    };
  }

  if (!input.task_type) {
    return {
      success: false,
      error: 'task_type is required (text-to-3d, image-to-3d, retexture, rigging, or animation)'
    };
  }

  const endpoint = TASK_ENDPOINTS[input.task_type];
  if (!endpoint) {
    return {
      success: false,
      error: `Invalid task_type: ${input.task_type}. Must be one of: ${Object.keys(TASK_ENDPOINTS).join(', ')}`
    };
  }

  try {
    // Get task status
    const response = await fetch(`${MESHY_API_BASE}${endpoint}/${input.task_id}`, {
      method: 'GET',
      headers: getMeshyHeaders(apiKey)
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      return handleApiError(response, errorData);
    }

    const taskData = await response.json();
    const result = parseTaskResponse(taskData);

    // If task is complete and download is enabled, save the model
    if (result.success && result.model_url && input.download_if_complete !== false) {
      // Determine prefix based on task type
      const prefixes: Record<TaskType, string> = {
        'text-to-3d': 't2m',
        'image-to-3d': 'i2m',
        'retexture': 'rtx',
        'rigging': 'rig',
        'animation': 'anim'
      };
      const prefix = prefixes[input.task_type] || 'model';

      return await saveModelFromResponse(result, prefix);
    }

    return result;

  } catch (error) {
    console.error('[getTask3D] Error:', error);
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error occurred'
    };
  }
}

export default getTask3D;

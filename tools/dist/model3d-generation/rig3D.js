/**
 * Rig 3D Tool - Meshy AI
 *
 * Automatically rig a humanoid 3D model for animation using Meshy AI.
 * Creates a skeletal armature that can be used with animate3D.
 *
 * Environment Variables:
 *   MESHY_API_KEY - Meshy AI API key (injected by AI Hub at runtime)
 *   MODEL3D_API_KEY - Alternative API key (used if MESHY_API_KEY not set)
 *   GENERATED_MODELS_DIR - Output directory (optional, defaults to ./generated-models)
 *
 * Usage:
 *   import { rig3D } from '/opt/ai-tools/dist/model3d-generation/rig3D.js';
 *
 *   // Rig a model from a previous task
 *   const result = await rig3D({
 *     model_path_or_task_id: 'task-id-from-previous-generation',
 *     height_meters: 1.8,
 *     wait_for_completion: true
 *   });
 *
 *   if (result.success) {
 *     // Use result.task_id with animate3D to apply animations
 *   }
 */
import { MESHY_API_BASE, getMeshyApiKey, getMeshyHeaders, handleApiError, modelToDataUri, pollForCompletion, saveModelFromResponse } from './shared.js';
/**
 * Check if a string looks like a Meshy task ID
 */
function isTaskId(str) {
    // Meshy task IDs are alphanumeric strings, not file paths
    return !str.includes('/') && !str.includes('\\') && !str.includes('.');
}
/**
 * Automatically rig a humanoid 3D model for animation.
 *
 * The rigged model is saved to disk and can be used with animate3D
 * to apply animations. Auto-rigging works best with T-pose or A-pose
 * humanoid characters.
 *
 * @param input - The rigging parameters
 * @returns The rigged model URL and file path, or task_id for async mode
 *
 * @example
 * ```typescript
 * // Rig a character model
 * const result = await rig3D({
 *   model_path_or_task_id: 'character-task-id',
 *   height_meters: 1.8,
 *   wait_for_completion: true
 * });
 *
 * if (result.success) {
 *   console.log('Rigged model:', result.model_url);
 *   console.log('Rig task ID:', result.task_id);
 *
 *   // Now use the task_id with animate3D
 *   const animated = await animate3D({
 *     rig_task_id: result.task_id,
 *     action_id: 'walk_forward',
 *     wait_for_completion: true
 *   });
 * }
 * ```
 */
export async function rig3D(input) {
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
    // Validate height if provided
    if (input.height_meters !== undefined) {
        if (input.height_meters <= 0 || input.height_meters > 10) {
            return {
                success: false,
                error: 'height_meters must be a positive number (typically 0.5 to 3.0 for characters)'
            };
        }
    }
    try {
        // Build request body
        const requestBody = {
            height_meters: input.height_meters ?? 1.7
        };
        // Determine if input is a task ID or file path
        if (isTaskId(input.model_path_or_task_id)) {
            requestBody.input_task_id = input.model_path_or_task_id;
        }
        else {
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
        // Start the rigging
        const response = await fetch(`${MESHY_API_BASE}/openapi/v1/rigging`, {
            method: 'POST',
            headers: getMeshyHeaders(apiKey),
            body: JSON.stringify(requestBody)
        });
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            return handleApiError(response, errorData);
        }
        const result = await response.json();
        const taskId = result.result;
        if (!taskId) {
            return {
                success: false,
                error: 'Failed to start rigging: no task ID returned.'
            };
        }
        // If not waiting for completion, return the task ID immediately
        if (!input.wait_for_completion) {
            return {
                success: false, // Not yet complete
                task_id: taskId,
                status: 'PENDING',
                progress: 0
            };
        }
        // Poll for completion
        console.log(`[rig3D] Task ${taskId} started, waiting for completion...`);
        const pollResult = await pollForCompletion(taskId, 'rigging', apiKey);
        if (!pollResult.success) {
            return pollResult;
        }
        // Download and save the model
        return await saveModelFromResponse(pollResult, 'rig');
    }
    catch (error) {
        console.error('[rig3D] Error:', error);
        return {
            success: false,
            error: error instanceof Error ? error.message : 'Unknown error occurred'
        };
    }
}
export default rig3D;
//# sourceMappingURL=rig3D.js.map
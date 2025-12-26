/**
 * Animate 3D Tool - Meshy AI
 *
 * Apply animation to a rigged 3D model using Meshy AI.
 * Requires a model that has been rigged with rig3D first.
 *
 * Environment Variables:
 *   MESHY_API_KEY - Meshy AI API key (injected by AI Hub at runtime)
 *   MODEL3D_API_KEY - Alternative API key (used if MESHY_API_KEY not set)
 *   GENERATED_MODELS_DIR - Output directory (optional, defaults to ./generated-models)
 *
 * Usage:
 *   import { animate3D } from '/opt/ai-tools/dist/model3d-generation/animate3D.js';
 *
 *   // First rig the model, then animate
 *   const rigged = await rig3D({
 *     model_path_or_task_id: 'character-task-id',
 *     wait_for_completion: true
 *   });
 *
 *   const animated = await animate3D({
 *     rig_task_id: rigged.task_id,
 *     action_id: 'walk_forward',
 *     fps: 30,
 *     wait_for_completion: true
 *   });
 *
 * Note: Meshy has 500+ animation actions available. Common actions include:
 *   - walk_forward, walk_backward
 *   - run_forward
 *   - jump, jump_forward
 *   - idle, idle_breathing
 *   - wave, clap
 *   - punch, kick
 *   - dance_hip_hop, dance_salsa
 */
import { MESHY_API_BASE, getMeshyApiKey, getMeshyHeaders, handleApiError, pollForCompletion, saveModelFromResponse } from './shared.js';
/**
 * Apply animation to a rigged 3D model.
 *
 * The animated model is saved to disk as FBX with embedded animation.
 * Animation is asynchronous by default - use wait_for_completion: true
 * to wait for the result, or check the task status with getTask3D.
 *
 * @param input - The animation parameters
 * @returns The animated model URL and file path, or task_id for async mode
 *
 * @example
 * ```typescript
 * // First rig the model
 * const rigged = await rig3D({
 *   model_path_or_task_id: 'character-task-id',
 *   wait_for_completion: true
 * });
 *
 * // Then animate it
 * const animated = await animate3D({
 *   rig_task_id: rigged.task_id,
 *   action_id: 'dance_hip_hop',
 *   fps: 30,
 *   wait_for_completion: true
 * });
 *
 * if (animated.success) {
 *   console.log('Animated model:', animated.model_url);
 *   console.log('Saved to:', animated.file_path);
 * }
 *
 * // Create multiple animations from the same rig
 * const walking = await animate3D({
 *   rig_task_id: rigged.task_id,
 *   action_id: 'walk_forward',
 *   wait_for_completion: true
 * });
 *
 * const jumping = await animate3D({
 *   rig_task_id: rigged.task_id,
 *   action_id: 'jump',
 *   wait_for_completion: true
 * });
 * ```
 */
export async function animate3D(input) {
    // Get API key from environment
    const apiKey = getMeshyApiKey();
    if (!apiKey) {
        return {
            success: false,
            error: 'MESHY_API_KEY environment variable not set. 3D generation is not configured.'
        };
    }
    // Validate input
    if (!input.rig_task_id) {
        return {
            success: false,
            error: 'rig_task_id is required. Use rig3D to rig a model first.'
        };
    }
    if (!input.action_id) {
        return {
            success: false,
            error: 'action_id is required. Specify an animation action like "walk_forward" or "dance_hip_hop".'
        };
    }
    // Validate FPS if provided
    if (input.fps !== undefined) {
        const validFps = [24, 25, 30, 60];
        if (!validFps.includes(input.fps)) {
            return {
                success: false,
                error: `fps must be one of: ${validFps.join(', ')}`
            };
        }
    }
    try {
        // Build request body
        const requestBody = {
            rig_task_id: input.rig_task_id,
            action_id: input.action_id
        };
        // Add optional parameters
        if (input.fps !== undefined) {
            requestBody.fps = input.fps;
        }
        // Start the animation
        const response = await fetch(`${MESHY_API_BASE}/openapi/v1/animations`, {
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
                error: 'Failed to start animation: no task ID returned.'
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
        console.log(`[animate3D] Task ${taskId} started, waiting for completion...`);
        const pollResult = await pollForCompletion(taskId, 'animation', apiKey);
        if (!pollResult.success) {
            return pollResult;
        }
        // Download and save the model
        return await saveModelFromResponse(pollResult, 'anim');
    }
    catch (error) {
        console.error('[animate3D] Error:', error);
        return {
            success: false,
            error: error instanceof Error ? error.message : 'Unknown error occurred'
        };
    }
}
export default animate3D;
//# sourceMappingURL=animate3D.js.map
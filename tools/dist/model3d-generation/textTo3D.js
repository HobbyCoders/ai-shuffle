/**
 * Text to 3D Tool - Meshy AI
 *
 * Generate a 3D model from a text prompt using Meshy AI.
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
 *   import { textTo3D } from '/opt/ai-tools/dist/model3d-generation/textTo3D.js';
 *
 *   // Quick async mode (returns task_id immediately)
 *   const result = await textTo3D({
 *     prompt: 'A medieval castle with stone walls and towers'
 *   });
 *
 *   // Sync mode (waits for completion)
 *   const result = await textTo3D({
 *     prompt: 'A wooden treasure chest',
 *     wait_for_completion: true
 *   });
 *
 *   if (result.success) {
 *     // result.model_url contains the URL to the GLB file
 *     // result.file_path contains the local file path
 *   }
 */
import { MESHY_API_BASE, getMeshyApiKey, getMeshyHeaders, handleApiError, pollForCompletion, saveModelFromResponse } from './shared.js';
/**
 * Generate a 3D model from a text prompt using Meshy AI.
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
 * // Async mode - returns immediately with task_id
 * const result = await textTo3D({
 *   prompt: 'A wooden pirate ship with cannons',
 *   art_style: 'realistic'
 * });
 *
 * if (!result.success && result.task_id) {
 *   console.log('Task started:', result.task_id);
 *   // Check later with getTask3D({ task_id: result.task_id, task_type: 'text-to-3d' })
 * }
 *
 * // Sync mode - waits for completion
 * const result = await textTo3D({
 *   prompt: 'A medieval sword with ornate handle',
 *   model: 'meshy-6',
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
export async function textTo3D(input) {
    // Get API key from environment
    const apiKey = getMeshyApiKey();
    if (!apiKey) {
        return {
            success: false,
            error: 'MESHY_API_KEY environment variable not set. 3D generation is not configured.'
        };
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
    // Validate target_polycount if provided
    if (input.target_polycount !== undefined) {
        if (input.target_polycount < 100 || input.target_polycount > 300000) {
            return {
                success: false,
                error: 'target_polycount must be between 100 and 300,000.'
            };
        }
    }
    // Get model from environment or use default
    const model = input.model || process.env.MODEL3D_MODEL || 'meshy-6';
    try {
        // Build request body
        const requestBody = {
            mode: 'preview', // Always start with preview mode
            prompt: input.prompt.trim(),
            ai_model: model === 'latest' ? 'meshy-6' : model
        };
        // Add optional parameters
        if (input.art_style) {
            requestBody.art_style = input.art_style;
        }
        if (input.topology) {
            requestBody.topology = input.topology;
        }
        if (input.target_polycount !== undefined) {
            requestBody.target_polycount = input.target_polycount;
        }
        // Start the 3D generation
        const response = await fetch(`${MESHY_API_BASE}/openapi/v2/text-to-3d`, {
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
                error: 'Failed to start 3D generation: no task ID returned.'
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
        console.log(`[textTo3D] Task ${taskId} started, waiting for completion...`);
        const pollResult = await pollForCompletion(taskId, 'text-to-3d', apiKey);
        if (!pollResult.success) {
            return pollResult;
        }
        // Download and save the model
        return await saveModelFromResponse(pollResult, 't2m');
    }
    catch (error) {
        console.error('[textTo3D] Error:', error);
        return {
            success: false,
            error: error instanceof Error ? error.message : 'Unknown error occurred'
        };
    }
}
export default textTo3D;
//# sourceMappingURL=textTo3D.js.map
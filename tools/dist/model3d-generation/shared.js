/**
 * Shared utilities for 3D model generation tools
 */
import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';
// ============================================================================
// CONSTANTS
// ============================================================================
/** Meshy API base URL */
export const MESHY_API_BASE = 'https://api.meshy.ai';
/** Default polling interval in milliseconds */
export const DEFAULT_POLL_INTERVAL = 5000;
/** Maximum polling time in milliseconds (10 minutes) */
export const MAX_POLL_TIME = 10 * 60 * 1000;
/**
 * Endpoint mapping for task types
 */
export const TASK_ENDPOINTS = {
    'text-to-3d': '/openapi/v2/text-to-3d',
    'image-to-3d': '/openapi/v1/image-to-3d',
    'retexture': '/openapi/v1/retexture',
    'rigging': '/openapi/v1/rigging',
    'animation': '/openapi/v1/animations'
};
// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================
/**
 * Get the directory for storing generated 3D models.
 * Uses GENERATED_MODELS_DIR env var if set, otherwise creates a 'generated-models'
 * folder in the current working directory.
 */
export function getGeneratedModelsDir() {
    if (process.env.GENERATED_MODELS_DIR) {
        return process.env.GENERATED_MODELS_DIR;
    }
    return join(process.cwd(), 'generated-models');
}
/**
 * Sleep for a given number of milliseconds
 */
export function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}
/**
 * Read an image file and convert to base64 data URI
 */
export function imageToDataUri(imagePath) {
    try {
        if (!existsSync(imagePath)) {
            return { error: `Image file not found: ${imagePath}` };
        }
        const imageBuffer = readFileSync(imagePath);
        const base64 = imageBuffer.toString('base64');
        // Determine MIME type from extension
        const ext = imagePath.toLowerCase().split('.').pop();
        const mimeTypes = {
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'png': 'image/png',
            'gif': 'image/gif',
            'webp': 'image/webp',
        };
        const mimeType = mimeTypes[ext || ''] || 'image/jpeg';
        const dataUri = `data:${mimeType};base64,${base64}`;
        return { dataUri, mimeType };
    }
    catch (error) {
        return { error: error instanceof Error ? error.message : 'Failed to read image file' };
    }
}
/**
 * Read a 3D model file and convert to base64 data URI
 */
export function modelToDataUri(modelPath) {
    try {
        if (!existsSync(modelPath)) {
            return { error: `Model file not found: ${modelPath}` };
        }
        const modelBuffer = readFileSync(modelPath);
        const base64 = modelBuffer.toString('base64');
        // Determine MIME type from extension
        const ext = modelPath.toLowerCase().split('.').pop();
        const mimeTypes = {
            'glb': 'model/gltf-binary',
            'gltf': 'model/gltf+json',
            'fbx': 'application/octet-stream',
            'obj': 'text/plain',
            'stl': 'application/sla',
        };
        const mimeType = mimeTypes[ext || ''] || 'application/octet-stream';
        const dataUri = `data:${mimeType};base64,${base64}`;
        return { dataUri, mimeType };
    }
    catch (error) {
        return { error: error instanceof Error ? error.message : 'Failed to read model file' };
    }
}
/**
 * Get Meshy API key from environment
 */
export function getMeshyApiKey() {
    return process.env.MESHY_API_KEY || process.env.MODEL3D_API_KEY;
}
/**
 * Create authorization headers for Meshy API
 */
export function getMeshyHeaders(apiKey) {
    return {
        'Authorization': `Bearer ${apiKey}`,
        'Content-Type': 'application/json'
    };
}
/**
 * Handle common API errors
 */
export function handleApiError(response, errorData) {
    const errorMsg = errorData?.message || errorData?.error?.message || `HTTP ${response.status}`;
    if (response.status === 400) {
        return {
            success: false,
            error: `Bad request: ${errorMsg}`
        };
    }
    if (response.status === 401 || response.status === 403) {
        return {
            success: false,
            error: 'Meshy API key is invalid or expired. Please check your settings.'
        };
    }
    if (response.status === 429) {
        return {
            success: false,
            error: 'Rate limit exceeded. Please wait a moment and try again.'
        };
    }
    if (response.status === 402) {
        return {
            success: false,
            error: 'Insufficient credits. Please add more credits to your Meshy account.'
        };
    }
    return {
        success: false,
        error: `API error: ${errorMsg}`
    };
}
/**
 * Parse Meshy task response into our standard format
 */
export function parseTaskResponse(taskData) {
    const status = taskData.status?.toUpperCase() || 'PENDING';
    const response = {
        success: status === 'SUCCEEDED',
        task_id: taskData.id,
        status: status,
        progress: taskData.progress || 0
    };
    // Add thumbnail if available
    if (taskData.thumbnail_url) {
        response.thumbnail_url = taskData.thumbnail_url;
    }
    // Add model URLs if available
    if (taskData.model_urls || taskData.model_url) {
        response.model_urls = {
            glb: taskData.model_urls?.glb || taskData.model_url,
            fbx: taskData.model_urls?.fbx,
            obj: taskData.model_urls?.obj,
            usdz: taskData.model_urls?.usdz
        };
        response.model_url = taskData.model_urls?.glb || taskData.model_url;
    }
    // Add texture URLs if available (for PBR textures)
    if (taskData.texture_urls) {
        response.texture_urls = {
            base_color: taskData.texture_urls.base_color,
            metallic: taskData.texture_urls.metallic,
            roughness: taskData.texture_urls.roughness,
            normal: taskData.texture_urls.normal
        };
    }
    // Check for errors
    if (status === 'FAILED') {
        response.success = false;
        response.error = taskData.task_error?.message || 'Task failed';
    }
    if (status === 'CANCELED') {
        response.success = false;
        response.error = 'Task was canceled';
    }
    return response;
}
/**
 * Poll for task completion
 */
export async function pollForCompletion(taskId, taskType, apiKey, maxPollTime = MAX_POLL_TIME, pollInterval = DEFAULT_POLL_INTERVAL) {
    const startTime = Date.now();
    const endpoint = TASK_ENDPOINTS[taskType];
    while (Date.now() - startTime < maxPollTime) {
        await sleep(pollInterval);
        try {
            const response = await fetch(`${MESHY_API_BASE}${endpoint}/${taskId}`, {
                method: 'GET',
                headers: getMeshyHeaders(apiKey)
            });
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                return handleApiError(response, errorData);
            }
            const taskData = await response.json();
            const result = parseTaskResponse(taskData);
            // If task is complete (success or failure), return
            if (result.status === 'SUCCEEDED' || result.status === 'FAILED' || result.status === 'CANCELED') {
                return result;
            }
            // Log progress for debugging
            console.log(`[${taskType}] Task ${taskId}: ${result.progress}% (${result.status})`);
        }
        catch (error) {
            return {
                success: false,
                error: error instanceof Error ? error.message : 'Polling error occurred'
            };
        }
    }
    return {
        success: false,
        task_id: taskId,
        status: 'IN_PROGRESS',
        error: '3D generation timed out. Use getTask3D to check status later.'
    };
}
/**
 * Download a 3D model from URL and save to disk
 */
export async function downloadAndSaveModel(modelUrl, prefix = 'model') {
    try {
        const response = await fetch(modelUrl);
        if (!response.ok) {
            return { error: `Failed to download model: HTTP ${response.status}` };
        }
        const modelBuffer = Buffer.from(await response.arrayBuffer());
        // Generate unique filename
        const timestamp = Date.now();
        const randomSuffix = crypto.randomUUID().slice(0, 8);
        // Determine extension from URL or default to .glb
        const urlPath = new URL(modelUrl).pathname;
        const ext = urlPath.split('.').pop() || 'glb';
        const filename = `${prefix}-${timestamp}-${randomSuffix}.${ext}`;
        // Ensure output directory exists
        const outputDir = getGeneratedModelsDir();
        if (!existsSync(outputDir)) {
            mkdirSync(outputDir, { recursive: true });
        }
        const filePath = join(outputDir, filename);
        writeFileSync(filePath, modelBuffer);
        return { file_path: filePath, filename };
    }
    catch (error) {
        return { error: error instanceof Error ? error.message : 'Failed to download model' };
    }
}
/**
 * Save 3D model response with downloaded file
 */
export async function saveModelFromResponse(response, prefix = 'model') {
    if (!response.success || !response.model_url) {
        return response;
    }
    const downloadResult = await downloadAndSaveModel(response.model_url, prefix);
    if ('error' in downloadResult) {
        return {
            ...response,
            error: downloadResult.error
        };
    }
    return {
        ...response,
        file_path: downloadResult.file_path,
        filename: downloadResult.filename
    };
}
//# sourceMappingURL=shared.js.map
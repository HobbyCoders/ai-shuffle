/**
 * Shared utilities for 3D model generation tools
 */
/** Meshy API base URL */
export declare const MESHY_API_BASE = "https://api.meshy.ai";
/** Default polling interval in milliseconds */
export declare const DEFAULT_POLL_INTERVAL = 5000;
/** Maximum polling time in milliseconds (10 minutes) */
export declare const MAX_POLL_TIME: number;
/**
 * Common response interface for 3D model generation
 */
export interface Model3DResponse {
    success: boolean;
    task_id?: string;
    model_url?: string;
    file_path?: string;
    filename?: string;
    progress?: number;
    status?: 'PENDING' | 'IN_PROGRESS' | 'SUCCEEDED' | 'FAILED' | 'CANCELED';
    model_urls?: {
        glb?: string;
        fbx?: string;
        obj?: string;
        usdz?: string;
    };
    texture_urls?: {
        base_color?: string;
        metallic?: string;
        roughness?: string;
        normal?: string;
    };
    thumbnail_url?: string;
    error?: string;
}
/**
 * Task types for the Meshy API
 */
export type TaskType = 'text-to-3d' | 'image-to-3d' | 'retexture' | 'rigging' | 'animation';
/**
 * Endpoint mapping for task types
 */
export declare const TASK_ENDPOINTS: Record<TaskType, string>;
/**
 * Get the directory for storing generated 3D models.
 * Uses GENERATED_MODELS_DIR env var if set, otherwise creates a 'generated-models'
 * folder in the current working directory.
 */
export declare function getGeneratedModelsDir(): string;
/**
 * Sleep for a given number of milliseconds
 */
export declare function sleep(ms: number): Promise<void>;
/**
 * Read an image file and convert to base64 data URI
 */
export declare function imageToDataUri(imagePath: string): {
    dataUri: string;
    mimeType: string;
} | {
    error: string;
};
/**
 * Read a 3D model file and convert to base64 data URI
 */
export declare function modelToDataUri(modelPath: string): {
    dataUri: string;
    mimeType: string;
} | {
    error: string;
};
/**
 * Get Meshy API key from environment
 */
export declare function getMeshyApiKey(): string | undefined;
/**
 * Create authorization headers for Meshy API
 */
export declare function getMeshyHeaders(apiKey: string): Record<string, string>;
/**
 * Handle common API errors
 */
export declare function handleApiError(response: Response, errorData: any): Model3DResponse;
/**
 * Parse Meshy task response into our standard format
 */
export declare function parseTaskResponse(taskData: any): Model3DResponse;
/**
 * Poll for task completion
 */
export declare function pollForCompletion(taskId: string, taskType: TaskType, apiKey: string, maxPollTime?: number, pollInterval?: number): Promise<Model3DResponse>;
/**
 * Download a 3D model from URL and save to disk
 */
export declare function downloadAndSaveModel(modelUrl: string, prefix?: string): Promise<{
    file_path: string;
    filename: string;
} | {
    error: string;
}>;
/**
 * Save 3D model response with downloaded file
 */
export declare function saveModelFromResponse(response: Model3DResponse, prefix?: string): Promise<Model3DResponse>;
//# sourceMappingURL=shared.d.ts.map
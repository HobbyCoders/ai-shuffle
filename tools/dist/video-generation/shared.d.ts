/**
 * Shared utilities for video generation tools
 */
/**
 * Get the directory for storing generated videos.
 * Uses GENERATED_VIDEOS_DIR env var if set, otherwise creates a 'generated-videos'
 * folder in the current working directory.
 */
export declare function getGeneratedVideosDir(): string;
/**
 * Sleep for a given number of milliseconds
 */
export declare function sleep(ms: number): Promise<void>;
/**
 * Read an image file and convert to base64
 */
export declare function imageToBase64(imagePath: string): {
    base64: string;
    mimeType: string;
} | {
    error: string;
};
/**
 * Common response interface for video generation
 */
export interface VideoResponse {
    success: boolean;
    video_url?: string;
    file_path?: string;
    filename?: string;
    mime_type?: string;
    duration_seconds?: number;
    /** The source video URI from Veo - use this with extendVideo to extend the video */
    source_video_uri?: string;
    error?: string;
}
/**
 * Save video buffer to disk and return response
 * @param videoBuffer - The video data to save
 * @param prefix - Filename prefix (e.g., 'video', 'i2v', 'extended', 'bridge')
 * @param duration - Video duration in seconds
 * @param sourceVideoUri - Optional: The Veo video URI for use with extendVideo
 */
export declare function saveVideo(videoBuffer: Buffer, prefix: string, duration: number, sourceVideoUri?: string): VideoResponse;
/**
 * Handle common API errors
 */
export declare function handleApiError(response: Response, errorData: any): VideoResponse;
/**
 * Poll for video generation completion
 */
export declare function pollForCompletion(operationName: string, apiKey: string, maxPollTime?: number, pollInterval?: number): Promise<{
    success: true;
    videoUri: string;
} | {
    success: false;
    error: string;
}>;
/**
 * Download video from URI
 */
export declare function downloadVideo(videoUri: string, apiKey?: string): Promise<Buffer | {
    error: string;
}>;
//# sourceMappingURL=shared.d.ts.map
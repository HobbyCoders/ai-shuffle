/**
 * Video Extension Tool - Veo (Google)
 *
 * Extend an existing Veo-generated video by ~7 seconds. The new segment continues
 * seamlessly from the final frames of the source video.
 *
 * Environment Variables:
 *   GEMINI_API_KEY - Google AI API key (injected by AI Hub at runtime)
 *   VEO_MODEL - Model to use (optional, defaults to veo-3.1-fast-generate-preview)
 *
 * Usage:
 *   import { extendVideo } from '/workspace/ai-hub/tools/dist/video-generation/extendVideo.js';
 *
 *   const result = await extendVideo({
 *     video_path: '/path/to/video.mp4',
 *     prompt: 'The camera continues panning right to reveal the ocean'
 *   });
 *
 *   if (result.success) {
 *     // result.video_url contains the URL to access the extended video
 *     // result.file_path contains the local file path
 *   }
 */
import { readFileSync, existsSync } from 'fs';
import { handleApiError, pollForCompletion, downloadVideo, saveVideo } from './shared.js';
/**
 * Read a video file and convert to base64
 */
function videoToBase64(videoPath) {
    try {
        if (!existsSync(videoPath)) {
            return { error: `Video file not found: ${videoPath}` };
        }
        const videoBuffer = readFileSync(videoPath);
        const base64 = videoBuffer.toString('base64');
        return { base64, mimeType: 'video/mp4' };
    }
    catch (error) {
        return { error: error instanceof Error ? error.message : 'Failed to read video file' };
    }
}
/**
 * Extend a Veo-generated video by approximately 7 seconds.
 *
 * The extension continues seamlessly from the final second of the source video.
 * You can extend a video up to 20 times to create longer content.
 *
 * @param input - The extension parameters including source video
 * @returns The extended video URL and file path, or error details
 *
 * @example
 * ```typescript
 * const result = await extendVideo({
 *   video_path: '/workspace/generated-videos/video-123.mp4',
 *   prompt: 'The character continues walking and then stops to look around'
 * });
 *
 * if (result.success) {
 *   console.log('Extended video URL:', result.video_url);
 *   console.log('Saved to:', result.file_path);
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export async function extendVideo(input) {
    // Get API key from environment
    const apiKey = process.env.GEMINI_API_KEY;
    if (!apiKey) {
        return {
            success: false,
            error: 'GEMINI_API_KEY environment variable not set. Video generation is not configured.'
        };
    }
    // Get model from environment or use default
    const model = process.env.VEO_MODEL || 'veo-3.1-fast-generate-preview';
    // Validate input
    if (!input.video_path) {
        return {
            success: false,
            error: 'video_path is required'
        };
    }
    if (input.prompt && input.prompt.length > 4000) {
        return {
            success: false,
            error: 'Prompt is too long. Maximum ~1,024 tokens (approximately 4,000 characters).'
        };
    }
    // Read and encode the video
    const videoResult = videoToBase64(input.video_path);
    if ('error' in videoResult) {
        return {
            success: false,
            error: videoResult.error
        };
    }
    try {
        // Build the request body
        const instances = {
            video: {
                bytesBase64Encoded: videoResult.base64,
                mimeType: videoResult.mimeType
            }
        };
        // Add optional prompt
        if (input.prompt) {
            instances.prompt = input.prompt.trim();
        }
        // Start the video extension (long-running operation)
        const startResponse = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:predictLongRunning?key=${apiKey}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                instances: [instances],
                parameters: {
                    ...(input.negative_prompt && { negativePrompt: input.negative_prompt }),
                    ...(input.seed !== undefined && { seed: input.seed }),
                    ...(input.person_generation && { personGeneration: input.person_generation })
                }
            }),
        });
        if (!startResponse.ok) {
            const errorData = await startResponse.json().catch(() => ({}));
            return handleApiError(startResponse, errorData);
        }
        const startResult = await startResponse.json();
        const operationName = startResult.name;
        if (!operationName) {
            return {
                success: false,
                error: 'Failed to start video extension: no operation name returned.'
            };
        }
        // Poll for completion (extensions may take longer)
        const pollResult = await pollForCompletion(operationName, apiKey, 8 * 60 * 1000);
        if (!pollResult.success) {
            return { success: false, error: pollResult.error };
        }
        // Download the video (pass apiKey for authenticated download)
        const videoBuffer = await downloadVideo(pollResult.videoUri, apiKey);
        if ('error' in videoBuffer) {
            return { success: false, error: videoBuffer.error };
        }
        // Save and return (extension adds ~7 seconds)
        return saveVideo(videoBuffer, 'extended', 7);
    }
    catch (error) {
        return {
            success: false,
            error: error instanceof Error ? error.message : 'Unknown error occurred'
        };
    }
}
export default extendVideo;
//# sourceMappingURL=extendVideo.js.map
/**
 * Image-to-Video Tool - Veo (Google)
 *
 * Animate a still image into a video using AI. The API key is provided via environment variable.
 * Videos are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   GEMINI_API_KEY - Google AI API key (injected by AI Hub at runtime)
 *   VEO_MODEL - Model to use (optional, defaults to veo-3.1-fast-generate-preview)
 *
 * Usage:
 *   import { imageToVideo } from '/opt/ai-tools/dist/video-generation/imageToVideo.js';
 *
 *   const result = await imageToVideo({
 *     image_path: '/path/to/image.png',
 *     prompt: 'The character starts walking forward slowly'
 *   });
 *
 *   if (result.success) {
 *     // result.video_url contains the URL to access the video
 *     // result.file_path contains the local file path
 *   }
 */
import { imageToBase64, handleApiError, pollForCompletion, downloadVideo, saveVideo } from './shared.js';
/**
 * Convert a still image into an animated video using Google Veo.
 *
 * The video is saved to disk and a URL is returned for display in the chat UI.
 * Video generation is asynchronous - this function polls until complete.
 *
 * @param input - The generation parameters including source image
 * @returns The generated video URL and file path, or error details
 *
 * @example
 * ```typescript
 * const result = await imageToVideo({
 *   image_path: '/workspace/photo.jpg',
 *   prompt: 'The person in the photo smiles and waves at the camera',
 *   duration: 4,
 *   aspect_ratio: '16:9'
 * });
 *
 * if (result.success) {
 *   console.log('Video URL:', result.video_url);
 *   console.log('Saved to:', result.file_path);
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export async function imageToVideo(input) {
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
    if (!input.image_path) {
        return {
            success: false,
            error: 'image_path is required'
        };
    }
    if (!input.prompt || input.prompt.trim().length === 0) {
        return {
            success: false,
            error: 'Prompt cannot be empty. Describe the motion/animation to apply.'
        };
    }
    if (input.prompt.length > 4000) {
        return {
            success: false,
            error: 'Prompt is too long. Maximum ~1,024 tokens (approximately 4,000 characters).'
        };
    }
    // Validate resolution/duration combination
    if (input.resolution === '1080p' && input.duration && input.duration !== 8) {
        return {
            success: false,
            error: '1080p resolution is only available for 8-second videos.'
        };
    }
    // Read and encode the image
    const imageResult = imageToBase64(input.image_path);
    if ('error' in imageResult) {
        return {
            success: false,
            error: imageResult.error
        };
    }
    const prompt = input.prompt.trim();
    const aspectRatio = input.aspect_ratio || '16:9';
    const duration = input.duration || 8;
    const resolution = input.resolution || '720p';
    try {
        // Start the video generation (long-running operation)
        const startResponse = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:predictLongRunning?key=${apiKey}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                instances: [{
                        prompt: prompt,
                        image: {
                            bytesBase64Encoded: imageResult.base64,
                            mimeType: imageResult.mimeType
                        }
                    }],
                parameters: {
                    aspectRatio: aspectRatio,
                    durationSeconds: duration,
                    resolution: resolution,
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
                error: 'Failed to start video generation: no operation name returned.'
            };
        }
        // Poll for completion
        const pollResult = await pollForCompletion(operationName, apiKey);
        if (!pollResult.success) {
            return { success: false, error: pollResult.error };
        }
        // Download the video (pass apiKey for authenticated download)
        const videoBuffer = await downloadVideo(pollResult.videoUri, apiKey);
        if ('error' in videoBuffer) {
            return { success: false, error: videoBuffer.error };
        }
        // Save and return (include source_video_uri for use with extendVideo)
        return saveVideo(videoBuffer, 'i2v', duration, pollResult.videoUri);
    }
    catch (error) {
        return {
            success: false,
            error: error instanceof Error ? error.message : 'Unknown error occurred'
        };
    }
}
export default imageToVideo;
//# sourceMappingURL=imageToVideo.js.map
/**
 * Frame Bridging Tool - Veo (Google)
 *
 * Generate a video that smoothly transitions between two images (first and last frame).
 * Perfect for creating natural transitions, morph effects, or scene changes.
 *
 * Environment Variables:
 *   GEMINI_API_KEY - Google AI API key (injected by AI Hub at runtime)
 *   VEO_MODEL - Model to use (optional, defaults to veo-3.1-fast-generate-preview)
 *
 * Usage:
 *   import { bridgeFrames } from '/workspace/ai-hub/tools/dist/video-generation/bridgeFrames.js';
 *
 *   const result = await bridgeFrames({
 *     start_image: '/path/to/start.png',
 *     end_image: '/path/to/end.png',
 *     prompt: 'Smooth camera pan from the bedroom to the living room'
 *   });
 *
 *   if (result.success) {
 *     // result.video_url contains the URL to access the video
 *     // result.file_path contains the local file path
 *   }
 */
import { imageToBase64, handleApiError, pollForCompletion, downloadVideo, saveVideo } from './shared.js';
/**
 * Generate a video that transitions between two images using Google Veo.
 *
 * This creates a smooth, natural video that starts at one image and ends at another.
 * Useful for transitions, morphs, time-lapses, and scene changes.
 *
 * @param input - The generation parameters including start and end images
 * @returns The generated video URL and file path, or error details
 *
 * @example
 * ```typescript
 * const result = await bridgeFrames({
 *   start_image: '/workspace/morning.jpg',
 *   end_image: '/workspace/evening.jpg',
 *   prompt: 'Time lapse of the sun moving across the sky',
 *   duration: 8,
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
export async function bridgeFrames(input) {
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
    if (!input.start_image) {
        return {
            success: false,
            error: 'start_image is required'
        };
    }
    if (!input.end_image) {
        return {
            success: false,
            error: 'end_image is required'
        };
    }
    if (input.prompt && input.prompt.length > 4000) {
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
    // Read and encode both images
    const startImageResult = imageToBase64(input.start_image);
    if ('error' in startImageResult) {
        return {
            success: false,
            error: `Start image error: ${startImageResult.error}`
        };
    }
    const endImageResult = imageToBase64(input.end_image);
    if ('error' in endImageResult) {
        return {
            success: false,
            error: `End image error: ${endImageResult.error}`
        };
    }
    const aspectRatio = input.aspect_ratio || '16:9';
    const duration = input.duration || 8;
    const resolution = input.resolution || '720p';
    try {
        // Build the request body with both images
        const instances = {
            image: {
                bytesBase64Encoded: startImageResult.base64,
                mimeType: startImageResult.mimeType
            },
            lastFrame: {
                bytesBase64Encoded: endImageResult.base64,
                mimeType: endImageResult.mimeType
            }
        };
        // Add optional prompt
        if (input.prompt) {
            instances.prompt = input.prompt.trim();
        }
        // Start the video generation (long-running operation)
        const startResponse = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:predictLongRunning?key=${apiKey}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                instances: [instances],
                parameters: {
                    aspectRatio: aspectRatio,
                    durationSeconds: String(duration),
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
        // Save and return
        return saveVideo(videoBuffer, 'bridge', duration);
    }
    catch (error) {
        return {
            success: false,
            error: error instanceof Error ? error.message : 'Unknown error occurred'
        };
    }
}
export default bridgeFrames;
//# sourceMappingURL=bridgeFrames.js.map
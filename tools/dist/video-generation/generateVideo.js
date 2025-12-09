/**
 * Video Generation Tool - Veo (Google)
 *
 * Generate videos using AI. The API key is provided via environment variable.
 * Videos are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   GEMINI_API_KEY - Google AI API key (injected by AI Hub at runtime)
 *   VEO_MODEL - Model to use (optional, defaults to veo-3.1-fast-generate-preview)
 *
 * Usage:
 *   import { generateVideo } from '/opt/ai-tools/dist/video-generation/generateVideo.js';
 *
 *   const result = await generateVideo({
 *     prompt: 'A cat playing with a ball of yarn'
 *   });
 *
 *   if (result.success) {
 *     // result.video_url contains the URL to access the video
 *     // result.file_path contains the local file path
 *   }
 */
import { handleApiError, pollForCompletion, downloadVideo, saveVideo } from './shared.js';
/**
 * Generate a video from a text prompt using Google Veo.
 *
 * The video is saved to disk and a URL is returned for display in the chat UI.
 * Video generation is asynchronous - this function polls until complete.
 *
 * @param input - The generation parameters
 * @returns The generated video URL and file path, or error details
 *
 * @example
 * ```typescript
 * const result = await generateVideo({
 *   prompt: 'A butterfly landing on a flower, macro shot, cinematic',
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
export async function generateVideo(input) {
    // Get API key from environment
    const apiKey = process.env.GEMINI_API_KEY;
    if (!apiKey) {
        return {
            success: false,
            error: 'GEMINI_API_KEY environment variable not set. Video generation is not configured.'
        };
    }
    // Get model from environment or use default (Veo 3.1 Fast for speed)
    const model = process.env.VEO_MODEL || 'veo-3.1-fast-generate-preview';
    // Validate input
    if (!input.prompt || input.prompt.trim().length === 0) {
        return {
            success: false,
            error: 'Prompt cannot be empty'
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
                        prompt: prompt
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
        return saveVideo(videoBuffer, 'video', duration, pollResult.videoUri);
    }
    catch (error) {
        return {
            success: false,
            error: error instanceof Error ? error.message : 'Unknown error occurred'
        };
    }
}
export default generateVideo;
//# sourceMappingURL=generateVideo.js.map
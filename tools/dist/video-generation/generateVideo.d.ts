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
import { VideoResponse } from './shared.js';
export interface GenerateVideoInput {
    /**
     * The text prompt describing the video to generate.
     * Be descriptive! Include action, scene, style, mood details.
     * Max 1,024 tokens.
     *
     * @example "A golden retriever running through a field of sunflowers at sunset"
     * @example "Aerial drone shot of a city skyline transitioning from day to night"
     */
    prompt: string;
    /**
     * Aspect ratio of the generated video.
     * @default "16:9"
     */
    aspect_ratio?: '16:9' | '9:16';
    /**
     * Duration of the generated video in seconds.
     * @default 8
     */
    duration?: 4 | 6 | 8;
    /**
     * Resolution of the generated video.
     * Note: 1080p is only available for 8-second videos.
     * @default "720p"
     */
    resolution?: '720p' | '1080p';
    /**
     * Elements to exclude from the video generation.
     * @example "blurry, low quality, text, watermark"
     */
    negative_prompt?: string;
    /**
     * Seed for reproducibility. Same seed + same inputs may produce similar results.
     * Note: Not guaranteed to be deterministic.
     */
    seed?: number;
    /**
     * Control generation of people in the video.
     * @default "allow_adult"
     */
    person_generation?: 'allow_all' | 'allow_adult';
}
export type GenerateVideoResponse = VideoResponse;
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
export declare function generateVideo(input: GenerateVideoInput): Promise<GenerateVideoResponse>;
export default generateVideo;
//# sourceMappingURL=generateVideo.d.ts.map
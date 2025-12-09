/**
 * Video Generation Tool - Unified Provider Interface
 *
 * Generate videos using AI from multiple providers (Google Veo, OpenAI Sora, etc.)
 * The provider and model are selected based on environment variables or explicit input.
 * Videos are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   VIDEO_PROVIDER - Provider ID (e.g., "google-veo", "openai-sora")
 *   VIDEO_API_KEY - API key for the selected provider
 *   VIDEO_MODEL - Model to use (e.g., "veo-3.1-fast-generate-preview", "sora-2")
 *
 *   Legacy (backwards compatible):
 *   GEMINI_API_KEY - Google AI API key (used if VIDEO_API_KEY not set)
 *   VEO_MODEL - Veo model (used if VIDEO_MODEL not set for Google provider)
 *   OPENAI_API_KEY - OpenAI API key (used for Sora if VIDEO_API_KEY not set)
 *
 * Usage:
 *   import { generateVideo } from '/opt/ai-tools/dist/video-generation/generateVideo.js';
 *
 *   // Use default provider from settings
 *   const result = await generateVideo({
 *     prompt: 'A cat playing with a ball of yarn'
 *   });
 *
 *   // Or explicitly specify provider/model
 *   const result = await generateVideo({
 *     prompt: 'A cat playing with a ball of yarn',
 *     provider: 'openai-sora',
 *     model: 'sora-2-pro'
 *   });
 *
 *   if (result.success) {
 *     // result.video_url contains the URL to access the video
 *     // result.file_path contains the local file path
 *   }
 */
import { VideoResult } from '../providers/types.js';
export { VideoResponse } from './shared.js';
export interface GenerateVideoInput {
    /**
     * The text prompt describing the video to generate.
     * Be descriptive! Include action, scene, style, mood details.
     * Max ~1,024 tokens (approximately 4,000 characters).
     *
     * @example "A golden retriever running through a field of sunflowers at sunset"
     * @example "Aerial drone shot of a city skyline transitioning from day to night"
     */
    prompt: string;
    /**
     * Override the default video provider.
     * Available: "google-veo", "openai-sora"
     * If not specified, uses VIDEO_PROVIDER env var or defaults to "google-veo"
     */
    provider?: string;
    /**
     * Override the default model for the selected provider.
     * Google Veo: "veo-3.1-fast-generate-preview", "veo-3.1-generate-preview"
     * OpenAI Sora: "sora-2", "sora-2-pro"
     * If not specified, uses VIDEO_MODEL/VEO_MODEL env var or provider default
     */
    model?: string;
    /**
     * Aspect ratio of the generated video.
     * @default "16:9"
     */
    aspect_ratio?: '16:9' | '9:16' | '1:1';
    /**
     * Duration of the generated video in seconds.
     * Google Veo: 4, 6, or 8 seconds
     * OpenAI Sora: 4, 8, or 12 seconds
     * @default 8
     */
    duration?: number;
    /**
     * Resolution of the generated video.
     * Note: Higher resolutions may have duration restrictions.
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
export type GenerateVideoResponse = VideoResult;
/**
 * Generate a video from a text prompt.
 *
 * Uses the configured provider (Google Veo, OpenAI Sora, etc.) or allows
 * explicit override via the provider/model parameters.
 *
 * The video is saved to disk and a URL is returned for display in the chat UI.
 * Video generation is asynchronous - this function polls until complete.
 *
 * @param input - The generation parameters
 * @returns The generated video URL and file path, or error details
 *
 * @example
 * ```typescript
 * // Use default provider
 * const result = await generateVideo({
 *   prompt: 'A butterfly landing on a flower, macro shot, cinematic',
 *   duration: 8,
 *   aspect_ratio: '16:9'
 * });
 *
 * // Use specific provider
 * const result = await generateVideo({
 *   prompt: 'A butterfly landing on a flower',
 *   provider: 'openai-sora',
 *   model: 'sora-2-pro'
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
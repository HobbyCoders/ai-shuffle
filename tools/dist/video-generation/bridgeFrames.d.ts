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
import { VideoResponse } from './shared.js';
export interface BridgeFramesInput {
    /**
     * Path to the starting image (first frame of the video).
     * Supported formats: JPEG, PNG, GIF, WebP.
     * For best quality, use 720p (1280x720) or higher resolution.
     */
    start_image: string;
    /**
     * Path to the ending image (last frame of the video).
     * Supported formats: JPEG, PNG, GIF, WebP.
     * Should have the same aspect ratio as start_image for best results.
     */
    end_image: string;
    /**
     * Text prompt describing how to transition between the frames.
     * Describe the motion, camera movement, or transformation.
     * Max 1,024 tokens.
     *
     * @example "Smooth zoom out revealing the full cityscape"
     * @example "Time lapse of the flower blooming"
     * @example "Camera slowly pans from left to right"
     */
    prompt?: string;
    /**
     * Aspect ratio of the generated video.
     * Should match your input images for best results.
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
     * @example "abrupt cuts, morphing artifacts, blur"
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
export type BridgeFramesResponse = VideoResponse;
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
export declare function bridgeFrames(input: BridgeFramesInput): Promise<BridgeFramesResponse>;
export default bridgeFrames;
//# sourceMappingURL=bridgeFrames.d.ts.map
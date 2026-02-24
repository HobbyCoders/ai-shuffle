/**
 * Video Generation Tools
 *
 * AI-powered video generation via fal.ai (Kling, Veo 3.1, Sora 2).
 * Configure your FAL_KEY in Settings > Integrations before use.
 *
 * Available functions:
 * - generateVideo: Generate a video from a text prompt
 * - imageToVideo: Animate a still image into a video
 *
 * @example
 * ```typescript
 * import { generateVideo, imageToVideo } from './tools/video-generation';
 *
 * // Text-to-video
 * const result = await generateVideo({
 *   prompt: 'A cat playing with a ball of yarn, slow motion',
 *   duration: 8,
 *   aspect_ratio: '16:9'
 * });
 *
 * // Image-to-video
 * const animated = await imageToVideo({
 *   image_path: '/path/to/photo.jpg',
 *   prompt: 'The character starts walking forward'
 * });
 * ```
 */

// Shared types
export { VideoResponse } from './shared.js';

// Text-to-video
export { generateVideo, type GenerateVideoInput, type GenerateVideoResponse } from './generateVideo.js';

// Image-to-video
export { imageToVideo, type ImageToVideoInput, type ImageToVideoResponse } from './imageToVideo.js';

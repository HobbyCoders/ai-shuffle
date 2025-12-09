/**
 * Video Generation Tools
 *
 * AI-powered video generation using Veo (Google).
 * Configure your API key in Settings > Integrations before use.
 *
 * Available functions:
 * - generateVideo: Generate a video from a text prompt
 * - imageToVideo: Animate a still image into a video
 * - extendVideo: Extend an existing Veo video by ~7 seconds
 * - bridgeFrames: Generate a smooth transition between two images
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
export { VideoResponse } from './shared.js';
export { generateVideo, type GenerateVideoInput, type GenerateVideoResponse } from './generateVideo.js';
export { imageToVideo, type ImageToVideoInput, type ImageToVideoResponse } from './imageToVideo.js';
export { extendVideo, type ExtendVideoInput, type ExtendVideoResponse } from './extendVideo.js';
export { bridgeFrames, type BridgeFramesInput, type BridgeFramesResponse } from './bridgeFrames.js';
//# sourceMappingURL=index.d.ts.map
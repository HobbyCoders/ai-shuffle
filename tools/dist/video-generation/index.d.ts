/**
 * Video Generation Tools
 *
 * AI-powered video generation using Veo (Google).
 * Configure your API key in Settings > Integrations before use.
 *
 * Available functions:
 * - generateVideo: Generate a video from a text prompt
 *
 * @example
 * ```typescript
 * import * as videoGen from './tools/video-generation';
 *
 * const result = await videoGen.generateVideo({
 *   prompt: 'A cat playing with a ball of yarn, slow motion',
 *   duration: 8,
 *   aspect_ratio: '16:9'
 * });
 *
 * if (result.success) {
 *   console.log('Video URL:', result.video_url);
 * }
 * ```
 */
export { generateVideo, type GenerateVideoInput, type GenerateVideoResponse } from './generateVideo.js';
//# sourceMappingURL=index.d.ts.map
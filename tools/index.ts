/**
 * AI Tools — fal.ai Provider
 *
 * All tools use fal.ai as the sole AI backend through a single API key.
 *
 * ## Available Tools
 *
 * ### Image Generation (Flux, Recraft, Nano Banana Pro)
 * ```typescript
 * import { generateImage } from '/opt/ai-tools/dist/image-generation/generateImage.js';
 * const result = await generateImage({ prompt: 'A sunset over mountains' });
 * ```
 *
 * ### Image Editing
 * ```typescript
 * import { editImage } from '/opt/ai-tools/dist/image-generation/editImage.js';
 * const result = await editImage({
 *   prompt: 'Add a rainbow in the sky',
 *   image_path: '/path/to/image.png'
 * });
 * ```
 *
 * ### Video Generation (Kling O3, Veo 3.1, Sora 2)
 * ```typescript
 * import { generateVideo } from '/opt/ai-tools/dist/video-generation/generateVideo.js';
 * const result = await generateVideo({
 *   prompt: 'A cat playing with yarn',
 *   duration: 8,
 *   aspect_ratio: '16:9'
 * });
 * ```
 *
 * ### 3D Model Generation (Meshy v6, Hunyuan-3D)
 * ```typescript
 * import { textTo3D, imageTo3D } from '/opt/ai-tools/dist/model3d-generation/index.js';
 *
 * const model = await textTo3D({
 *   prompt: 'A medieval castle',
 *   wait_for_completion: true
 * });
 *
 * const fromImage = await imageTo3D({
 *   image_path: '/path/to/character.png',
 *   wait_for_completion: true
 * });
 * ```
 *
 * ## Note on TTS/STT
 *
 * Text-to-Speech and Speech-to-Text are NOT model tools — they are app features:
 * - TTS: For reading Claude's messages aloud (Kokoro, MiniMax, Chatterbox via fal.ai)
 * - STT: For voice input (Whisper via fal.ai)
 * These are handled by the backend API directly, not as tools Claude calls.
 */

// Re-export all tools for convenience
export * as imageGeneration from './image-generation/index.js';
export * as videoGeneration from './video-generation/index.js';
export * as model3dGeneration from './model3d-generation/index.js';

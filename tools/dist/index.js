/**
 * AI Hub Tools
 *
 * This directory contains tools that Claude can use during code execution.
 * Each tool is a TypeScript module that wraps AI Hub's internal APIs.
 *
 * ## Available Tools
 *
 * ### Image Generation (Nano Banana)
 * Generate images using Google Gemini's image models.
 * ```typescript
 * import { generateImage } from './tools/image-generation/generateImage.js';
 * const result = await generateImage({ prompt: 'A sunset over mountains' });
 * ```
 *
 * ### Image Editing (Nano Banana)
 * Edit existing images using Google Gemini's image models.
 * ```typescript
 * import { editImage } from './tools/image-generation/editImage.js';
 * const result = await editImage({
 *   prompt: 'Add a rainbow in the sky',
 *   image_path: '/path/to/image.png'
 * });
 * ```
 *
 * ### Video Generation (Veo)
 * Generate videos using Google Veo models.
 * ```typescript
 * import { generateVideo } from './tools/video-generation/generateVideo.js';
 * const result = await generateVideo({
 *   prompt: 'A cat playing with yarn',
 *   duration: 8,
 *   aspect_ratio: '16:9'
 * });
 * ```
 *
 * ### Voice-to-Text (OpenAI Whisper)
 * Transcribe audio to text using OpenAI's Whisper API.
 * ```typescript
 * import { transcribeAudio } from './tools/voice-to-text/transcribe.js';
 * const result = await transcribeAudio({ audioPath: '/path/to/audio.mp3' });
 * ```
 *
 * ## How to Use
 *
 * 1. Import the tool module you need
 * 2. Call the function with appropriate parameters
 * 3. Handle the response (check success/error)
 *
 * ## Adding New Tools
 *
 * Create a new directory under /tools with:
 * - Individual tool files (e.g., toolName.ts)
 * - An index.ts that exports all tools
 * - Clear documentation and TypeScript interfaces
 *
 * Tools should follow the pattern:
 * - Input interface with JSDoc descriptions
 * - Output interface with success/error handling
 * - Async function that calls the API and handles errors
 */
// Re-export all tools for convenience
export * as imageGeneration from './image-generation/index.js';
export * as videoGeneration from './video-generation/index.js';
// Note: voice-to-text module will be available in a future release
// export * as voiceToText from './voice-to-text/index.js';
// Export the client utilities
export { callTool, getTool, uploadTool, getApiBaseUrl } from './client.js';
//# sourceMappingURL=index.js.map
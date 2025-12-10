/**
 * AI Hub Tools
 *
 * This directory contains tools that Claude can use during code execution.
 * Each tool is a TypeScript module that wraps AI Hub's internal APIs.
 *
 * ## Available Tools
 *
 * ### Image Generation (Nano Banana / Imagen 4)
 * Generate images using Google Gemini, Imagen 4, or OpenAI GPT Image.
 * ```typescript
 * import { generateImage } from '/opt/ai-tools/dist/image-generation/generateImage.js';
 * const result = await generateImage({ prompt: 'A sunset over mountains' });
 * ```
 *
 * ### Image Editing
 * Edit existing images using AI models.
 * ```typescript
 * import { editImage } from '/opt/ai-tools/dist/image-generation/editImage.js';
 * const result = await editImage({
 *   prompt: 'Add a rainbow in the sky',
 *   image_path: '/path/to/image.png'
 * });
 * ```
 *
 * ### Video Generation (Veo 3 / Veo 3.1 / Sora)
 * Generate videos with optional native audio using Veo 3 or Sora.
 * ```typescript
 * import { generateVideo } from '/opt/ai-tools/dist/video-generation/generateVideo.js';
 * const result = await generateVideo({
 *   prompt: 'A cat playing with yarn',
 *   duration: 8,
 *   aspect_ratio: '16:9',
 *   model: 'veo-3-fast-generate-preview'  // For video with audio
 * });
 * ```
 *
 * ### Text-to-Speech (OpenAI TTS)
 * Convert text to natural speech.
 * ```typescript
 * import { textToSpeech } from '/opt/ai-tools/dist/audio-generation/textToSpeech.js';
 * const result = await textToSpeech({
 *   text: 'Hello, world!',
 *   voice: 'alloy',
 *   instructions: 'Speak in a friendly, upbeat tone'
 * });
 * ```
 *
 * ### Speech-to-Text (GPT-4o Transcribe)
 * Transcribe audio to text with improved accuracy.
 * ```typescript
 * import { speechToText } from '/opt/ai-tools/dist/audio-generation/speechToText.js';
 * const result = await speechToText({
 *   audio_path: '/path/to/audio.mp3',
 *   language: 'en'
 * });
 * ```
 *
 * ### Video Analysis (Gemini)
 * Analyze video content and answer questions.
 * ```typescript
 * import { analyzeVideo } from '/opt/ai-tools/dist/video-analysis/analyzeVideo.js';
 * const result = await analyzeVideo({
 *   video_path: '/path/to/video.mp4',
 *   prompt: 'Describe what happens in this video'
 * });
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
export * as imageGeneration from './image-generation/index.js';
export * as videoGeneration from './video-generation/index.js';
export * as audioGeneration from './audio-generation/index.js';
export * as videoAnalysis from './video-analysis/index.js';
export { callTool, getTool, uploadTool, getApiBaseUrl } from './client.js';
//# sourceMappingURL=index.d.ts.map
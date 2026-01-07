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
 * ### 3D Model Generation (Meshy AI)
 * Generate, retexture, rig, and animate 3D models.
 * ```typescript
 * import { textTo3D, imageTo3D, rig3D, animate3D } from '/opt/ai-tools/dist/model3d-generation/index.js';
 *
 * // Generate from text
 * const model = await textTo3D({
 *   prompt: 'A medieval castle',
 *   wait_for_completion: true
 * });
 *
 * // Convert image to 3D
 * const fromImage = await imageTo3D({
 *   image_path: '/path/to/character.png',
 *   wait_for_completion: true
 * });
 *
 * // Rig and animate a character
 * const rigged = await rig3D({
 *   model_path_or_task_id: fromImage.task_id,
 *   wait_for_completion: true
 * });
 * const animated = await animate3D({
 *   rig_task_id: rigged.task_id,
 *   action_id: 'walk_forward',
 *   wait_for_completion: true
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
 *
 * ## Note on TTS/STT
 *
 * Text-to-Speech and Speech-to-Text are NOT model tools - they are app features:
 * - TTS: For reading Claude's messages aloud to the user
 * - STT: For voice input (already implemented in the chat UI)
 * These are handled by the backend API directly, not as tools Claude calls.
 */

// Re-export all tools for convenience
export * as imageGeneration from './image-generation/index.js';
export * as videoGeneration from './video-generation/index.js';
export * as videoAnalysis from './video-analysis/index.js';
export * as model3dGeneration from './model3d-generation/index.js';

// Export the client utilities
export { callTool, getTool, uploadTool, getApiBaseUrl } from './client.js';

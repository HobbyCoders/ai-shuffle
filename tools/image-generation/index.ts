/**
 * Image Generation Tools
 *
 * AI-powered image generation and editing using Nano Banana (Google Gemini).
 * Configure your API key in Settings > Integrations before use.
 *
 * Available functions:
 * - generateImage: Generate an image from a text prompt
 * - editImage: Edit an existing image with AI
 *
 * @example
 * ```typescript
 * import * as imageGen from './tools/image-generation';
 *
 * // Generate a new image
 * const result = await imageGen.generateImage({
 *   prompt: 'A serene Japanese garden with cherry blossoms'
 * });
 *
 * // Edit an existing image
 * const edited = await imageGen.editImage({
 *   prompt: 'Add a koi pond in the foreground',
 *   image_path: result.file_path
 * });
 *
 * if (edited.success) {
 *   console.log('Edited image URL:', edited.image_url);
 * }
 * ```
 */

export { generateImage, type GenerateImageInput, type GenerateImageResponse } from './generateImage.js';
export { editImage, type EditImageInput, type EditImageResponse } from './editImage.js';

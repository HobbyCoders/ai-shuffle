/**
 * Image Generation Tools
 *
 * AI-powered image generation and editing using Nano Banana (Google Gemini).
 * Configure your API key in Settings > Integrations before use.
 *
 * Available functions:
 * - generateImage: Generate an image from a text prompt
 * - editImage: Edit an existing image with AI
 * - generateWithReference: Generate images with character/style consistency
 *
 * @example
 * ```typescript
 * import { generateImage, editImage, generateWithReference } from './tools/image-generation';
 *
 * // Generate a new image
 * const result = await generateImage({
 *   prompt: 'A serene Japanese garden with cherry blossoms',
 *   image_size: '2K',
 *   number_of_images: 2
 * });
 *
 * // Edit an existing image
 * const edited = await editImage({
 *   prompt: 'Add a koi pond in the foreground',
 *   image_path: result.file_path
 * });
 *
 * // Generate with character consistency
 * const consistent = await generateWithReference({
 *   prompt: 'The character standing in a forest',
 *   reference_images: ['/path/to/character.png']
 * });
 * ```
 */
// Text-to-image generation
export { generateImage } from './generateImage.js';
// Image editing
export { editImage } from './editImage.js';
// Reference-based generation (character/style consistency)
export { generateWithReference } from './generateWithReference.js';
//# sourceMappingURL=index.js.map
/**
 * Image Generation Tools
 *
 * AI-powered image generation using Nano Banana (Google Gemini).
 * Configure your API key in Settings > Integrations before use.
 *
 * Available functions:
 * - generateImage: Generate an image from a text prompt
 *
 * @example
 * ```typescript
 * import * as imageGen from './tools/image-generation';
 *
 * const result = await imageGen.generateImage({
 *   prompt: 'A serene Japanese garden with cherry blossoms'
 * });
 *
 * if (result.success) {
 *   console.log('Generated image (base64):', result.image_base64?.substring(0, 50) + '...');
 * }
 * ```
 */
export { generateImage } from './generateImage.js';
//# sourceMappingURL=index.js.map
/**
 * Image Generation Tool - Nano Banana (Google Gemini)
 *
 * Generate images using AI. Requires Nano Banana to be configured in
 * Settings > Integrations.
 *
 * Usage:
 *   import { generateImage } from './tools/image-generation/generateImage.js';
 *
 *   const result = await generateImage({
 *     prompt: 'A futuristic city at sunset, cyberpunk style'
 *   });
 *
 *   if (result.success) {
 *     // result.image_base64 contains the base64-encoded image
 *     // result.mime_type is 'image/png'
 *   }
 */
import { callTool } from '../client.js';
/**
 * Generate an image from a text prompt using Nano Banana (Google Gemini).
 *
 * The image is returned as base64-encoded data that can be:
 * - Saved to a file
 * - Displayed in HTML: `<img src="data:${mime_type};base64,${image_base64}">`
 * - Processed further
 *
 * @param input - The generation parameters
 * @returns The generated image or error details
 *
 * @example
 * ```typescript
 * const result = await generateImage({
 *   prompt: 'A cozy coffee shop interior, watercolor style'
 * });
 *
 * if (result.success) {
 *   // Save to file
 *   const buffer = Buffer.from(result.image_base64!, 'base64');
 *   fs.writeFileSync('coffee-shop.png', buffer);
 *   console.log('Image saved!');
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export async function generateImage(input) {
    if (!input.prompt || input.prompt.trim().length === 0) {
        return {
            success: false,
            error: 'Prompt cannot be empty'
        };
    }
    if (input.prompt.length > 10000) {
        return {
            success: false,
            error: 'Prompt is too long. Maximum 10,000 characters.'
        };
    }
    try {
        const response = await callTool('/settings/generate-image', {
            prompt: input.prompt.trim(),
            aspect_ratio: input.aspect_ratio || '1:1'
        });
        return response;
    }
    catch (error) {
        return {
            success: false,
            error: error instanceof Error ? error.message : 'Unknown error occurred'
        };
    }
}
export default generateImage;
//# sourceMappingURL=generateImage.js.map
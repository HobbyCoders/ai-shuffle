/**
 * Image Generation Tool - Nano Banana (Google Gemini)
 *
 * Generate images using AI. The API key is provided via environment variable.
 *
 * Environment Variables:
 *   GEMINI_API_KEY - Google AI API key (injected by AI Hub at runtime)
 *   GEMINI_MODEL - Model to use (optional, defaults to gemini-2.0-flash-exp)
 *
 * Usage:
 *   import { generateImage } from '/workspace/ai-hub/tools/dist/image-generation/generateImage.js';
 *
 *   const result = await generateImage({
 *     prompt: 'A futuristic city at sunset, cyberpunk style'
 *   });
 *
 *   if (result.success) {
 *     // result.image_base64 contains the base64-encoded image
 *     // result.mime_type is 'image/png' or 'image/jpeg'
 *   }
 */
export interface GenerateImageInput {
    /**
     * The text prompt describing the image to generate.
     * Be descriptive! Include style, mood, colors, composition details.
     * Max 10,000 characters.
     *
     * @example "A majestic mountain landscape at golden hour, oil painting style"
     * @example "Cute robot holding a flower, digital art, soft lighting"
     */
    prompt: string;
    /**
     * Aspect ratio of the generated image.
     * @default "1:1"
     */
    aspect_ratio?: '1:1' | '16:9' | '9:16' | '4:3' | '3:4';
}
export interface GenerateImageResponse {
    /** Whether the image was generated successfully */
    success: boolean;
    /** Base64-encoded image data (only present if success=true) */
    image_base64?: string;
    /** MIME type of the image (e.g., 'image/png') */
    mime_type?: string;
    /** Error message if generation failed */
    error?: string;
}
/**
 * Generate an image from a text prompt using Google Gemini.
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
export declare function generateImage(input: GenerateImageInput): Promise<GenerateImageResponse>;
export default generateImage;
//# sourceMappingURL=generateImage.d.ts.map
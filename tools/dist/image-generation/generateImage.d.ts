/**
 * Image Generation Tool - Nano Banana (Google Gemini)
 *
 * Generate images using AI. The API key is provided via environment variable.
 * Images are saved to disk and a URL is returned for display.
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
 *     // result.image_url contains the URL to access the image
 *     // result.file_path contains the local file path
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
    /** URL to access the generated image (for display in chat) */
    image_url?: string;
    /** Local file path where the image was saved */
    file_path?: string;
    /** Filename of the generated image */
    filename?: string;
    /** MIME type of the image (e.g., 'image/png') */
    mime_type?: string;
    /** Error message if generation failed */
    error?: string;
}
/**
 * Generate an image from a text prompt using Google Gemini.
 *
 * The image is saved to disk and a URL is returned for display in the chat UI.
 * This avoids context window limitations with large base64 strings.
 *
 * @param input - The generation parameters
 * @returns The generated image URL and file path, or error details
 *
 * @example
 * ```typescript
 * const result = await generateImage({
 *   prompt: 'A cozy coffee shop interior, watercolor style'
 * });
 *
 * if (result.success) {
 *   console.log('Image URL:', result.image_url);
 *   console.log('Saved to:', result.file_path);
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export declare function generateImage(input: GenerateImageInput): Promise<GenerateImageResponse>;
export default generateImage;
//# sourceMappingURL=generateImage.d.ts.map
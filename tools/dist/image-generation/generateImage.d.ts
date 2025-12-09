/**
 * Image Generation Tool - Unified Provider Interface
 *
 * Generate images using AI from multiple providers (Google Gemini/Nano Banana, etc.)
 * The provider and model are selected based on environment variables or explicit input.
 * Images are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   IMAGE_PROVIDER - Provider ID (e.g., "google-gemini")
 *   IMAGE_API_KEY - API key for the selected provider
 *   IMAGE_MODEL - Model to use (e.g., "gemini-2.5-flash-image")
 *
 *   Legacy (backwards compatible):
 *   GEMINI_API_KEY - Google AI API key (used if IMAGE_API_KEY not set)
 *   GEMINI_MODEL - Gemini model (used if IMAGE_MODEL not set)
 *
 * Usage:
 *   import { generateImage } from '/opt/ai-tools/dist/image-generation/generateImage.js';
 *
 *   // Use default provider from settings
 *   const result = await generateImage({
 *     prompt: 'A futuristic city at sunset, cyberpunk style'
 *   });
 *
 *   // Or explicitly specify provider/model
 *   const result = await generateImage({
 *     prompt: 'A futuristic city at sunset',
 *     provider: 'google-gemini',
 *     model: 'gemini-3-pro-image-preview'
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
     * Override the default image provider.
     * Available: "google-gemini"
     * If not specified, uses IMAGE_PROVIDER env var or defaults to "google-gemini"
     */
    provider?: string;
    /**
     * Override the default model for the selected provider.
     * Google Gemini: "gemini-2.5-flash-image", "gemini-3-pro-image-preview"
     * If not specified, uses IMAGE_MODEL/GEMINI_MODEL env var or provider default
     */
    model?: string;
    /**
     * Aspect ratio of the generated image.
     * @default "1:1"
     */
    aspect_ratio?: '1:1' | '16:9' | '9:16' | '4:3' | '3:4' | '2:3' | '3:2' | '4:5' | '5:4' | '21:9';
    /**
     * Output image size/resolution.
     * - "1K" = ~1024px (default, fastest)
     * - "2K" = ~2048px (higher quality)
     * - "4K" = ~4096px (highest quality, slower)
     * @default "1K"
     */
    image_size?: '1K' | '2K' | '4K';
    /**
     * Number of images to generate (1-4).
     * When > 1, response will contain multiple images array.
     * @default 1
     */
    number_of_images?: 1 | 2 | 3 | 4;
    /**
     * Control generation of people in the image.
     * - "dont_allow": No people allowed
     * - "allow_adult": Only adults allowed (default)
     * - "allow_all": All people including children
     * @default "allow_adult"
     */
    person_generation?: 'dont_allow' | 'allow_adult' | 'allow_all';
}
/** Single generated image result */
export interface GeneratedImage {
    /** URL to access the generated image (for display in chat) */
    image_url: string;
    /** Local file path where the image was saved */
    file_path: string;
    /** Filename of the generated image */
    filename: string;
    /** MIME type of the image (e.g., 'image/png') */
    mime_type: string;
}
export interface GenerateImageResponse {
    /** Whether the image was generated successfully */
    success: boolean;
    /** URL to access the generated image (for display in chat) - first image when multiple */
    image_url?: string;
    /** Local file path where the image was saved - first image when multiple */
    file_path?: string;
    /** Filename of the generated image - first image when multiple */
    filename?: string;
    /** MIME type of the image (e.g., 'image/png') - first image when multiple */
    mime_type?: string;
    /** All generated images when number_of_images > 1 */
    images?: GeneratedImage[];
    /** Error message if generation failed */
    error?: string;
}
/**
 * Generate an image from a text prompt.
 *
 * Uses the configured provider (Google Gemini, etc.) or allows
 * explicit override via the provider/model parameters.
 *
 * The image is saved to disk and a URL is returned for display in the chat UI.
 * This avoids context window limitations with large base64 strings.
 *
 * @param input - The generation parameters
 * @returns The generated image URL and file path, or error details
 *
 * @example
 * ```typescript
 * // Generate a single image with default provider
 * const result = await generateImage({
 *   prompt: 'A cozy coffee shop interior, watercolor style'
 * });
 *
 * // Generate multiple images with specific provider
 * const result = await generateImage({
 *   prompt: 'A cute robot',
 *   provider: 'google-gemini',
 *   model: 'gemini-3-pro-image-preview',
 *   number_of_images: 4,
 *   image_size: '2K'
 * });
 *
 * if (result.success) {
 *   console.log('Image URL:', result.image_url);
 *   if (result.images) {
 *     result.images.forEach((img, i) => console.log(`Image ${i+1}:`, img.image_url));
 *   }
 * }
 * ```
 */
export declare function generateImage(input: GenerateImageInput): Promise<GenerateImageResponse>;
export default generateImage;
//# sourceMappingURL=generateImage.d.ts.map
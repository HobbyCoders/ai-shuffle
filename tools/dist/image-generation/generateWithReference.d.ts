/**
 * Reference-Based Image Generation Tool - Nano Banana (Google Gemini)
 *
 * Generate images while maintaining character/style consistency using reference images.
 * Use up to 14 reference images (6 objects, 5 humans) to guide generation.
 *
 * Environment Variables:
 *   GEMINI_API_KEY - Google AI API key (injected by AI Hub at runtime)
 *   GEMINI_MODEL - Model to use (optional, defaults to gemini-2.0-flash-exp)
 *
 * Usage:
 *   import { generateWithReference } from '/opt/ai-tools/dist/image-generation/generateWithReference.js';
 *
 *   const result = await generateWithReference({
 *     prompt: 'The character standing in a forest',
 *     reference_images: ['/path/to/character.png']
 *   });
 */
export interface ReferenceImage {
    /**
     * Path to the reference image file.
     * Supported formats: PNG, JPEG, GIF, WebP
     */
    path: string;
    /**
     * Optional label/description for this reference.
     * Helps the model understand what aspect to reference.
     * @example "main character" or "art style reference"
     */
    label?: string;
}
export interface GenerateWithReferenceInput {
    /**
     * The text prompt describing the image to generate.
     * Reference your images by their labels or position.
     * Max 10,000 characters.
     *
     * @example "The character from the reference standing in a forest"
     * @example "Generate a new scene in the style of the reference image"
     */
    prompt: string;
    /**
     * Reference images for character/style consistency.
     * Can be simple string paths or objects with path + label.
     * Maximum 14 images (6 objects, 5 humans recommended).
     *
     * @example ["/path/to/character.png"]
     * @example [{ path: "/path/to/character.png", label: "main hero" }]
     */
    reference_images: (string | ReferenceImage)[];
    /**
     * Aspect ratio of the generated image.
     * @default "1:1"
     */
    aspect_ratio?: '1:1' | '16:9' | '9:16' | '4:3' | '3:4' | '2:3' | '3:2' | '4:5' | '5:4' | '21:9';
    /**
     * Output image size/resolution.
     * @default "1K"
     */
    image_size?: '1K' | '2K' | '4K';
    /**
     * Number of images to generate (1-4).
     * @default 1
     */
    number_of_images?: 1 | 2 | 3 | 4;
    /**
     * Control generation of people in the image.
     * @default "allow_adult"
     */
    person_generation?: 'dont_allow' | 'allow_adult' | 'allow_all';
}
/** Single generated image result */
export interface GeneratedImage {
    image_url: string;
    file_path: string;
    filename: string;
    mime_type: string;
}
export interface GenerateWithReferenceResponse {
    success: boolean;
    image_url?: string;
    file_path?: string;
    filename?: string;
    mime_type?: string;
    images?: GeneratedImage[];
    error?: string;
}
/**
 * Generate an image using reference images for consistency.
 *
 * This is useful for:
 * - Maintaining character consistency across multiple generations
 * - Applying a specific art style from a reference
 * - Placing the same subject in different environments
 * - Creating consistent product shots or brand assets
 *
 * @param input - The generation parameters including reference images
 * @returns The generated image URL and file path, or error details
 *
 * @example
 * ```typescript
 * // Use a character reference
 * const result = await generateWithReference({
 *   prompt: 'The robot character exploring a jungle',
 *   reference_images: ['/workspace/robot-character.png']
 * });
 *
 * // Use multiple references with labels
 * const result = await generateWithReference({
 *   prompt: 'The hero fighting the villain in a city',
 *   reference_images: [
 *     { path: '/workspace/hero.png', label: 'hero character' },
 *     { path: '/workspace/villain.png', label: 'villain character' }
 *   ]
 * });
 *
 * // Style transfer
 * const result = await generateWithReference({
 *   prompt: 'A mountain landscape in this art style',
 *   reference_images: [{ path: '/workspace/style.png', label: 'art style' }]
 * });
 * ```
 */
export declare function generateWithReference(input: GenerateWithReferenceInput): Promise<GenerateWithReferenceResponse>;
export default generateWithReference;
//# sourceMappingURL=generateWithReference.d.ts.map
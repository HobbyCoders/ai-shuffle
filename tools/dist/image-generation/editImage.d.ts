/**
 * Image Editing Tool - Nano Banana (Google Gemini)
 *
 * Edit existing images using AI. The API key is provided via environment variable.
 * Images are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   GEMINI_API_KEY - Google AI API key (injected by AI Hub at runtime)
 *   GEMINI_MODEL - Model to use (optional, defaults to gemini-2.0-flash-exp)
 *
 * Usage:
 *   import { editImage } from '/workspace/ai-hub/tools/dist/image-generation/editImage.js';
 *
 *   const result = await editImage({
 *     prompt: 'Add a rainbow in the sky',
 *     image_path: '/path/to/existing/image.png'
 *   });
 *
 *   if (result.success) {
 *     // result.image_url contains the URL to access the edited image
 *     // result.file_path contains the local file path
 *   }
 */
export interface EditImageInput {
    /**
     * The editing instruction describing what changes to make to the image.
     * Be specific! Describe what to add, remove, change, or modify.
     * Max 10,000 characters.
     *
     * @example "Add a rainbow in the sky"
     * @example "Change the background to a beach sunset"
     * @example "Remove the person on the left"
     * @example "Make it look like a watercolor painting"
     */
    prompt: string;
    /**
     * Path to the image file to edit.
     * Supported formats: PNG, JPEG, GIF, WebP
     *
     * @example "/workspace/my-project/generated-images/image-123.png"
     */
    image_path?: string;
    /**
     * Base64-encoded image data to edit.
     * Use this if you have the image data directly instead of a file path.
     * Do not include the data URL prefix (e.g., "data:image/png;base64,")
     */
    image_base64?: string;
    /**
     * MIME type of the image when using image_base64.
     * Required if using image_base64.
     * @default "image/png"
     */
    image_mime_type?: 'image/png' | 'image/jpeg' | 'image/gif' | 'image/webp';
    /**
     * Aspect ratio of the output image.
     * If not specified, maintains the original aspect ratio.
     */
    aspect_ratio?: '1:1' | '16:9' | '9:16' | '4:3' | '3:4' | '2:3' | '3:2' | '4:5' | '5:4' | '21:9';
}
export interface EditImageResponse {
    /** Whether the image was edited successfully */
    success: boolean;
    /** URL to access the edited image (for display in chat) */
    image_url?: string;
    /** Local file path where the edited image was saved */
    file_path?: string;
    /** Filename of the edited image */
    filename?: string;
    /** MIME type of the image (e.g., 'image/png') */
    mime_type?: string;
    /** Error message if editing failed */
    error?: string;
}
/**
 * Edit an existing image using Google Gemini.
 *
 * The edited image is saved to disk and a URL is returned for display in the chat UI.
 * This avoids context window limitations with large base64 strings.
 *
 * @param input - The editing parameters
 * @returns The edited image URL and file path, or error details
 *
 * @example
 * ```typescript
 * // Edit using a file path
 * const result = await editImage({
 *   prompt: 'Add a cat sitting on the couch',
 *   image_path: '/path/to/living-room.png'
 * });
 *
 * // Edit using base64 data
 * const result = await editImage({
 *   prompt: 'Make it black and white',
 *   image_base64: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB...',
 *   image_mime_type: 'image/png'
 * });
 *
 * if (result.success) {
 *   console.log('Edited image URL:', result.image_url);
 *   console.log('Saved to:', result.file_path);
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export declare function editImage(input: EditImageInput): Promise<EditImageResponse>;
export default editImage;
//# sourceMappingURL=editImage.d.ts.map
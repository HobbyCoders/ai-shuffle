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
 *   import { editImage } from '/opt/ai-tools/dist/image-generation/editImage.js';
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
import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { join, extname } from 'path';
/**
 * Get the directory for storing generated images.
 * Uses GENERATED_IMAGES_DIR env var if set, otherwise creates a 'generated-images'
 * folder in the current working directory.
 */
function getGeneratedImagesDir() {
    // Allow override via environment variable
    if (process.env.GENERATED_IMAGES_DIR) {
        return process.env.GENERATED_IMAGES_DIR;
    }
    // Default to a folder in the current working directory
    return join(process.cwd(), 'generated-images');
}
/**
 * Get MIME type from file extension
 */
function getMimeType(filePath) {
    const ext = extname(filePath).toLowerCase();
    const mimeTypes = {
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
    };
    return mimeTypes[ext] || 'image/png';
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
export async function editImage(input) {
    // Get API key from environment
    const apiKey = process.env.GEMINI_API_KEY;
    if (!apiKey) {
        return {
            success: false,
            error: 'GEMINI_API_KEY environment variable not set. Image editing is not configured.'
        };
    }
    // Get model from environment or use default
    const model = process.env.GEMINI_MODEL || 'gemini-2.0-flash-exp';
    // Validate input - need prompt
    if (!input.prompt || input.prompt.trim().length === 0) {
        return {
            success: false,
            error: 'Prompt cannot be empty. Describe what changes you want to make to the image.'
        };
    }
    if (input.prompt.length > 10000) {
        return {
            success: false,
            error: 'Prompt is too long. Maximum 10,000 characters.'
        };
    }
    // Validate input - need image
    if (!input.image_path && !input.image_base64) {
        return {
            success: false,
            error: 'Either image_path or image_base64 must be provided.'
        };
    }
    const prompt = input.prompt.trim();
    // Load image data
    let imageBase64;
    let imageMimeType;
    try {
        if (input.image_path) {
            // Load from file path
            if (!existsSync(input.image_path)) {
                return {
                    success: false,
                    error: `Image file not found: ${input.image_path}`
                };
            }
            const imageBuffer = readFileSync(input.image_path);
            imageBase64 = imageBuffer.toString('base64');
            imageMimeType = getMimeType(input.image_path);
        }
        else {
            // Use provided base64 data
            imageBase64 = input.image_base64;
            imageMimeType = input.image_mime_type || 'image/png';
            // Remove data URL prefix if present
            if (imageBase64.includes(',')) {
                imageBase64 = imageBase64.split(',')[1];
            }
        }
    }
    catch (error) {
        return {
            success: false,
            error: `Failed to load image: ${error instanceof Error ? error.message : 'Unknown error'}`
        };
    }
    try {
        // Build the request body
        const requestBody = {
            contents: [
                {
                    parts: [
                        { text: prompt },
                        {
                            inline_data: {
                                mime_type: imageMimeType,
                                data: imageBase64
                            }
                        }
                    ]
                }
            ],
            generationConfig: {
                responseModalities: ['TEXT', 'IMAGE']
            }
        };
        // Build imageConfig with all options
        const imageConfig = {};
        if (input.aspect_ratio) {
            imageConfig.aspectRatio = input.aspect_ratio;
        }
        if (input.image_size) {
            imageConfig.imageSize = input.image_size;
        }
        if (input.person_generation) {
            imageConfig.personGeneration = input.person_generation;
        }
        if (input.number_of_images && input.number_of_images > 1) {
            imageConfig.numberOfImages = input.number_of_images;
        }
        if (Object.keys(imageConfig).length > 0) {
            requestBody.generationConfig.imageConfig = imageConfig;
        }
        // Call Gemini API directly
        const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(requestBody),
        });
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            const errorMsg = errorData?.error?.message || `HTTP ${response.status}`;
            // Check for safety filters
            if (response.status === 400) {
                const errorStr = JSON.stringify(errorData);
                if (errorStr.includes('SAFETY') || errorStr.toLowerCase().includes('blocked')) {
                    return {
                        success: false,
                        error: 'Image editing was blocked by safety filters. Please try a different prompt or image.'
                    };
                }
            }
            if (response.status === 401 || response.status === 403) {
                return {
                    success: false,
                    error: 'API key is invalid or expired.'
                };
            }
            if (response.status === 429) {
                return {
                    success: false,
                    error: 'Rate limit exceeded. Please wait a moment and try again.'
                };
            }
            return {
                success: false,
                error: `API error: ${errorMsg}`
            };
        }
        const result = await response.json();
        // Extract image from response
        const candidates = result.candidates || [];
        if (!candidates.length) {
            return {
                success: false,
                error: 'No edited image was generated. The model may have refused the request.'
            };
        }
        const parts = candidates[0]?.content?.parts || [];
        const editedImages = [];
        // Get the output directory
        const outputDir = getGeneratedImagesDir();
        // Ensure the output directory exists
        if (!existsSync(outputDir)) {
            mkdirSync(outputDir, { recursive: true });
        }
        // Process all image parts
        for (const part of parts) {
            if (part.inlineData) {
                const base64Data = part.inlineData.data;
                const mimeType = part.inlineData.mimeType || 'image/png';
                // Determine file extension from mime type
                const ext = mimeType === 'image/jpeg' ? 'jpg' : 'png';
                // Generate unique filename with timestamp - prefix with 'edited-'
                const timestamp = Date.now();
                const randomSuffix = Math.random().toString(36).substring(2, 8);
                const filename = `edited-${timestamp}-${randomSuffix}.${ext}`;
                // Save the image to disk
                const filePath = join(outputDir, filename);
                const buffer = Buffer.from(base64Data, 'base64');
                writeFileSync(filePath, buffer);
                // Build the URL
                const encodedPath = encodeURIComponent(filePath);
                editedImages.push({
                    image_url: `/api/generated-images/by-path?path=${encodedPath}`,
                    file_path: filePath,
                    filename: filename,
                    mime_type: mimeType
                });
            }
        }
        if (editedImages.length === 0) {
            // No image found - check if there's a text response (model might have refused)
            for (const part of parts) {
                if (part.text) {
                    return {
                        success: false,
                        error: `Model response: ${part.text.substring(0, 500)}`
                    };
                }
            }
            return {
                success: false,
                error: 'No edited image was generated. Please try a different prompt.'
            };
        }
        // Return response with first image as primary + all images array
        const firstImage = editedImages[0];
        return {
            success: true,
            image_url: firstImage.image_url,
            file_path: firstImage.file_path,
            filename: firstImage.filename,
            mime_type: firstImage.mime_type,
            ...(editedImages.length > 1 && { images: editedImages })
        };
    }
    catch (error) {
        return {
            success: false,
            error: error instanceof Error ? error.message : 'Unknown error occurred'
        };
    }
}
export default editImage;
//# sourceMappingURL=editImage.js.map
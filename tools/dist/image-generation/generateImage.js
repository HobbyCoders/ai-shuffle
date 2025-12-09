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
import { writeFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';
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
export async function generateImage(input) {
    // Get API key from environment
    const apiKey = process.env.GEMINI_API_KEY;
    if (!apiKey) {
        return {
            success: false,
            error: 'GEMINI_API_KEY environment variable not set. Image generation is not configured.'
        };
    }
    // Get model from environment or use default
    const model = process.env.GEMINI_MODEL || 'gemini-2.0-flash-exp';
    // Validate input
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
    const prompt = input.prompt.trim();
    try {
        // Call Gemini API directly
        const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                contents: [
                    {
                        parts: [
                            { text: prompt }
                        ]
                    }
                ],
                generationConfig: {
                    responseModalities: ['TEXT', 'IMAGE']
                }
            }),
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
                        error: 'Image generation was blocked by safety filters. Please try a different prompt.'
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
                error: 'No image was generated. The model may have refused the request.'
            };
        }
        const parts = candidates[0]?.content?.parts || [];
        // Look for inline image data
        for (const part of parts) {
            if (part.inlineData) {
                const base64Data = part.inlineData.data;
                const mimeType = part.inlineData.mimeType || 'image/png';
                // Determine file extension from mime type
                const ext = mimeType === 'image/jpeg' ? 'jpg' : 'png';
                // Generate unique filename with timestamp
                const timestamp = Date.now();
                const randomSuffix = Math.random().toString(36).substring(2, 8);
                const filename = `image-${timestamp}-${randomSuffix}.${ext}`;
                // Get the output directory (uses env var or cwd)
                const outputDir = getGeneratedImagesDir();
                // Ensure the output directory exists
                if (!existsSync(outputDir)) {
                    mkdirSync(outputDir, { recursive: true });
                }
                // Save the image to disk
                const filePath = join(outputDir, filename);
                const buffer = Buffer.from(base64Data, 'base64');
                writeFileSync(filePath, buffer);
                // Return the URL that the API will serve
                // Use the /by-path endpoint with full path for flexibility across projects
                const encodedPath = encodeURIComponent(filePath);
                return {
                    success: true,
                    image_url: `/api/generated-images/by-path?path=${encodedPath}`,
                    file_path: filePath,
                    filename: filename,
                    mime_type: mimeType
                };
            }
        }
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
            error: 'No image was generated. Please try a different prompt.'
        };
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
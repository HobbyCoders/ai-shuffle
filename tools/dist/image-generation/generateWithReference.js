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
 *   import { generateWithReference } from '/workspace/ai-hub/tools/dist/image-generation/generateWithReference.js';
 *
 *   const result = await generateWithReference({
 *     prompt: 'The character standing in a forest',
 *     reference_images: ['/path/to/character.png']
 *   });
 */
import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { join, extname } from 'path';
/**
 * Get the directory for storing generated images.
 */
function getGeneratedImagesDir() {
    if (process.env.GENERATED_IMAGES_DIR) {
        return process.env.GENERATED_IMAGES_DIR;
    }
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
 * Load image and convert to base64
 */
function loadImageAsBase64(imagePath) {
    try {
        if (!existsSync(imagePath)) {
            return { error: `Image file not found: ${imagePath}` };
        }
        const imageBuffer = readFileSync(imagePath);
        return {
            base64: imageBuffer.toString('base64'),
            mimeType: getMimeType(imagePath)
        };
    }
    catch (error) {
        return { error: error instanceof Error ? error.message : 'Failed to read image' };
    }
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
export async function generateWithReference(input) {
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
    if (!input.reference_images || input.reference_images.length === 0) {
        return {
            success: false,
            error: 'At least one reference image is required'
        };
    }
    if (input.reference_images.length > 14) {
        return {
            success: false,
            error: 'Maximum 14 reference images allowed'
        };
    }
    const prompt = input.prompt.trim();
    const numberOfImages = input.number_of_images || 1;
    try {
        // Build parts array with prompt and reference images
        const parts = [{ text: prompt }];
        // Load and add each reference image
        for (const ref of input.reference_images) {
            const imagePath = typeof ref === 'string' ? ref : ref.path;
            const label = typeof ref === 'string' ? undefined : ref.label;
            const imageResult = loadImageAsBase64(imagePath);
            if ('error' in imageResult) {
                return {
                    success: false,
                    error: imageResult.error
                };
            }
            // Add label as text part if provided
            if (label) {
                parts.push({ text: `[Reference: ${label}]` });
            }
            // Add the image
            parts.push({
                inline_data: {
                    mime_type: imageResult.mimeType,
                    data: imageResult.base64
                }
            });
        }
        // Build generation config
        const generationConfig = {
            responseModalities: ['TEXT', 'IMAGE']
        };
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
        if (numberOfImages > 1) {
            imageConfig.numberOfImages = numberOfImages;
        }
        if (Object.keys(imageConfig).length > 0) {
            generationConfig.imageConfig = imageConfig;
        }
        // Call Gemini API
        const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                contents: [{ parts }],
                generationConfig
            }),
        });
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            const errorMsg = errorData?.error?.message || `HTTP ${response.status}`;
            if (response.status === 400) {
                const errorStr = JSON.stringify(errorData);
                if (errorStr.includes('SAFETY') || errorStr.toLowerCase().includes('blocked')) {
                    return {
                        success: false,
                        error: 'Image generation was blocked by safety filters. Please try a different prompt or reference images.'
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
        // Extract images from response
        const candidates = result.candidates || [];
        if (!candidates.length) {
            return {
                success: false,
                error: 'No image was generated. The model may have refused the request.'
            };
        }
        const responseParts = candidates[0]?.content?.parts || [];
        const generatedImages = [];
        // Get the output directory
        const outputDir = getGeneratedImagesDir();
        if (!existsSync(outputDir)) {
            mkdirSync(outputDir, { recursive: true });
        }
        // Process all image parts
        for (const part of responseParts) {
            if (part.inlineData) {
                const base64Data = part.inlineData.data;
                const mimeType = part.inlineData.mimeType || 'image/png';
                const ext = mimeType === 'image/jpeg' ? 'jpg' : 'png';
                const timestamp = Date.now();
                const randomSuffix = Math.random().toString(36).substring(2, 8);
                const filename = `ref-${timestamp}-${randomSuffix}.${ext}`;
                const filePath = join(outputDir, filename);
                const buffer = Buffer.from(base64Data, 'base64');
                writeFileSync(filePath, buffer);
                const encodedPath = encodeURIComponent(filePath);
                generatedImages.push({
                    image_url: `/api/generated-images/by-path?path=${encodedPath}`,
                    file_path: filePath,
                    filename: filename,
                    mime_type: mimeType
                });
            }
        }
        if (generatedImages.length === 0) {
            for (const part of responseParts) {
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
        const firstImage = generatedImages[0];
        return {
            success: true,
            image_url: firstImage.image_url,
            file_path: firstImage.file_path,
            filename: firstImage.filename,
            mime_type: firstImage.mime_type,
            ...(generatedImages.length > 1 && { images: generatedImages })
        };
    }
    catch (error) {
        return {
            success: false,
            error: error instanceof Error ? error.message : 'Unknown error occurred'
        };
    }
}
export default generateWithReference;
//# sourceMappingURL=generateWithReference.js.map
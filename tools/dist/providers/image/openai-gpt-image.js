/**
 * OpenAI GPT Image Provider (gpt-image-1)
 *
 * Adapter for OpenAI's GPT-4o image generation API.
 * Supports text-to-image and image editing.
 *
 * API Reference: https://platform.openai.com/docs/api-reference/images
 */
import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';
// ============================================================================
// Helpers
// ============================================================================
function getGeneratedImagesDir() {
    if (process.env.GENERATED_IMAGES_DIR) {
        return process.env.GENERATED_IMAGES_DIR;
    }
    return join(process.cwd(), 'generated-images');
}
function getMimeType(format) {
    const mimeTypes = {
        'png': 'image/png',
        'jpeg': 'image/jpeg',
        'jpg': 'image/jpeg',
        'webp': 'image/webp',
    };
    return mimeTypes[format] || 'image/png';
}
function getExtension(format) {
    if (format === 'jpeg')
        return 'jpg';
    return format;
}
/**
 * Convert unified aspect ratio to OpenAI size format
 */
function aspectRatioToSize(aspectRatio = '1:1') {
    // OpenAI gpt-image-1 supports: 1024x1024, 1024x1536, 1536x1024, auto
    switch (aspectRatio) {
        case '1:1':
            return '1024x1024';
        case '9:16':
        case '2:3':
        case '3:4':
        case '4:5':
            return '1024x1536'; // Portrait
        case '16:9':
        case '3:2':
        case '4:3':
        case '5:4':
        case '21:9':
            return '1536x1024'; // Landscape
        default:
            return '1024x1024';
    }
}
/**
 * Convert unified image size to OpenAI quality
 */
function imageSizeToQuality(imageSize = '1K') {
    // Map our size options to OpenAI quality levels
    switch (imageSize) {
        case '4K':
            return 'high';
        case '2K':
            return 'medium';
        case '1K':
        default:
            return 'low';
    }
}
function handleApiError(response, errorData) {
    const errorMsg = errorData?.error?.message || `HTTP ${response.status}`;
    if (response.status === 400) {
        const errorStr = JSON.stringify(errorData);
        if (errorStr.includes('content_policy') || errorStr.toLowerCase().includes('safety')) {
            return {
                success: false,
                error: 'Image generation was blocked by content policy. Please try a different prompt.'
            };
        }
        if (errorStr.includes('invalid_size')) {
            return {
                success: false,
                error: 'Invalid image size. Supported sizes: 1024x1024, 1024x1536, 1536x1024'
            };
        }
    }
    if (response.status === 401) {
        return { success: false, error: 'API key is invalid or expired.' };
    }
    if (response.status === 403) {
        return { success: false, error: 'API key does not have access to image generation.' };
    }
    if (response.status === 429) {
        return { success: false, error: 'Rate limit exceeded. Please wait a moment and try again.' };
    }
    return { success: false, error: `API error: ${errorMsg}` };
}
function saveImages(imagesData, prefix, format = 'png') {
    const outputDir = getGeneratedImagesDir();
    if (!existsSync(outputDir)) {
        mkdirSync(outputDir, { recursive: true });
    }
    const generatedImages = [];
    for (const imageData of imagesData) {
        if (imageData.b64_json) {
            const ext = getExtension(format);
            const mimeType = getMimeType(format);
            const timestamp = Date.now();
            const randomSuffix = Math.random().toString(36).substring(2, 8);
            const filename = `${prefix}-${timestamp}-${randomSuffix}.${ext}`;
            const filePath = join(outputDir, filename);
            const buffer = Buffer.from(imageData.b64_json, 'base64');
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
    return generatedImages;
}
// ============================================================================
// Provider Implementation
// ============================================================================
export const openaiGptImageProvider = {
    id: 'openai-gpt-image',
    name: 'OpenAI GPT Image',
    models: [
        {
            id: 'gpt-image-1',
            name: 'GPT Image 1',
            description: 'GPT-4o native image generation - high quality, accurate text rendering',
            capabilities: ['text-to-image', 'image-edit'],
            pricing: { unit: 'image', price: 0.07 } // Medium quality average
        }
    ],
    async generate(input, credentials, model) {
        const apiKey = credentials.apiKey;
        // Validate input
        if (!input.prompt || input.prompt.trim().length === 0) {
            return { success: false, error: 'Prompt cannot be empty' };
        }
        if (input.prompt.length > 32000) {
            return { success: false, error: 'Prompt is too long. Maximum 32,000 characters.' };
        }
        const prompt = input.prompt.trim();
        const numberOfImages = input.number_of_images || 1;
        const size = aspectRatioToSize(input.aspect_ratio);
        const quality = imageSizeToQuality(input.image_size);
        // Determine output format - use webp for transparency support, png otherwise
        const outputFormat = 'png';
        try {
            // gpt-image-1 API parameters - note: response_format is NOT supported
            // It always returns base64-encoded images
            const requestBody = {
                model: model,
                prompt: prompt,
                size: size,
                quality: quality,
                n: numberOfImages,
                output_format: outputFormat // png, jpeg, or webp
            };
            const response = await fetch('https://api.openai.com/v1/images/generations', {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${apiKey}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(requestBody)
            });
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                return handleApiError(response, errorData);
            }
            const result = await response.json();
            const imagesData = result.data || [];
            if (imagesData.length === 0) {
                return { success: false, error: 'No image was generated. Please try a different prompt.' };
            }
            const generatedImages = saveImages(imagesData, 'gpt-image', outputFormat);
            if (generatedImages.length === 0) {
                return { success: false, error: 'Failed to save generated images.' };
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
    },
    async edit(input, credentials, model) {
        const apiKey = credentials.apiKey;
        // Validate input
        if (!input.prompt || input.prompt.trim().length === 0) {
            return { success: false, error: 'Prompt cannot be empty' };
        }
        if (!input.source_image) {
            return { success: false, error: 'Source image is required for editing' };
        }
        if (!existsSync(input.source_image)) {
            return { success: false, error: `Source image not found: ${input.source_image}` };
        }
        const prompt = input.prompt.trim();
        const size = aspectRatioToSize(input.aspect_ratio);
        try {
            // Read the source image
            const imageBuffer = readFileSync(input.source_image);
            const imageBase64 = imageBuffer.toString('base64');
            // Determine mime type from extension
            const ext = input.source_image.toLowerCase().split('.').pop() || 'png';
            const mimeType = getMimeType(ext);
            // Build multipart form data manually for fetch
            const boundary = '----FormBoundary' + Math.random().toString(36).substring(2);
            // For the images/edit endpoint, we need to use multipart/form-data
            // Note: gpt-image-1 does NOT support the images/edits endpoint as of April 2025
            // It only supports text-to-image. For editing, users should use the text prompt
            // to describe the desired changes along with the image.
            const formData = new FormData();
            // Create a Blob from the image buffer
            const imageBlob = new Blob([imageBuffer], { type: mimeType });
            formData.append('image', imageBlob, `image.${ext}`);
            formData.append('model', model);
            formData.append('prompt', prompt);
            formData.append('size', size);
            // Add mask if provided
            if (input.mask_image && existsSync(input.mask_image)) {
                const maskBuffer = readFileSync(input.mask_image);
                const maskExt = input.mask_image.toLowerCase().split('.').pop() || 'png';
                const maskMimeType = getMimeType(maskExt);
                const maskBlob = new Blob([maskBuffer], { type: maskMimeType });
                formData.append('mask', maskBlob, `mask.${maskExt}`);
            }
            const response = await fetch('https://api.openai.com/v1/images/edits', {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${apiKey}`
                    // Don't set Content-Type - let fetch handle it for FormData
                },
                body: formData
            });
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                return handleApiError(response, errorData);
            }
            const result = await response.json();
            const imagesData = result.data || [];
            if (imagesData.length === 0) {
                return { success: false, error: 'No image was generated. Please try a different prompt.' };
            }
            const generatedImages = saveImages(imagesData, 'gpt-edited', 'png');
            if (generatedImages.length === 0) {
                return { success: false, error: 'Failed to save edited image.' };
            }
            const firstImage = generatedImages[0];
            return {
                success: true,
                image_url: firstImage.image_url,
                file_path: firstImage.file_path,
                filename: firstImage.filename,
                mime_type: firstImage.mime_type
            };
        }
        catch (error) {
            return {
                success: false,
                error: error instanceof Error ? error.message : 'Unknown error occurred'
            };
        }
    },
    // Note: gpt-image-1 doesn't support reference-based generation like Gemini
    // generateWithReference is not implemented for this provider
    async validateCredentials(credentials) {
        try {
            const response = await fetch('https://api.openai.com/v1/models', {
                method: 'GET',
                headers: {
                    'Authorization': `Bearer ${credentials.apiKey}`
                }
            });
            if (response.status === 401) {
                return { valid: false, error: 'Invalid API key' };
            }
            if (response.status === 403) {
                return { valid: false, error: 'API key does not have sufficient permissions' };
            }
            if (!response.ok) {
                return { valid: false, error: `API returned status ${response.status}` };
            }
            return { valid: true };
        }
        catch (error) {
            return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
        }
    }
};
export default openaiGptImageProvider;
//# sourceMappingURL=openai-gpt-image.js.map
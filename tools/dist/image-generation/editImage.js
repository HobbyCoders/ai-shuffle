/**
 * Image Editing Tool - Unified Provider Interface
 *
 * Edit existing images using AI from multiple providers:
 * - Google Gemini (Nano Banana): gemini-2.5-flash-image, gemini-3-pro-image-preview
 * - OpenAI GPT Image: gpt-image-1 (GPT-4o native image editing)
 *
 * The provider and model are selected based on environment variables or explicit input.
 * Images are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   IMAGE_PROVIDER - Provider ID (e.g., "google-gemini", "openai-gpt-image")
 *   IMAGE_API_KEY - API key for the selected provider
 *   IMAGE_MODEL - Model to use (e.g., "gemini-2.5-flash-image", "gpt-image-1")
 *
 *   Legacy (backwards compatible):
 *   GEMINI_API_KEY - Google AI API key (used if IMAGE_API_KEY not set for Gemini)
 *   OPENAI_API_KEY - OpenAI API key (used if IMAGE_API_KEY not set for GPT Image)
 *   GEMINI_MODEL - Gemini model (used if IMAGE_MODEL not set)
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
import { registry } from '../providers/registry.js';
/**
 * Determine the provider ID to use based on input and environment
 */
function getProviderId(inputProvider) {
    // Explicit input takes priority
    if (inputProvider) {
        return inputProvider;
    }
    // Check environment variables
    if (process.env.IMAGE_PROVIDER) {
        return process.env.IMAGE_PROVIDER;
    }
    // Default to Google Gemini
    return 'google-gemini';
}
/**
 * Get API key for the specified provider
 */
function getApiKey(providerId) {
    // Check provider-specific env var first
    if (process.env.IMAGE_API_KEY) {
        return process.env.IMAGE_API_KEY;
    }
    // Legacy fallbacks based on provider
    if (providerId === 'google-gemini' || providerId === 'google-imagen') {
        return process.env.GEMINI_API_KEY || '';
    }
    if (providerId === 'openai-gpt-image') {
        return process.env.OPENAI_API_KEY || '';
    }
    return '';
}
/**
 * Get model ID for the specified provider
 */
function getModelId(providerId, inputModel) {
    // Explicit input takes priority
    if (inputModel) {
        return inputModel;
    }
    // Check environment variables
    if (process.env.IMAGE_MODEL) {
        return process.env.IMAGE_MODEL;
    }
    // Legacy fallbacks
    if (providerId === 'google-gemini' && process.env.GEMINI_MODEL) {
        return process.env.GEMINI_MODEL;
    }
    // Provider defaults
    const provider = registry.getImageProvider(providerId);
    if (provider && provider.models.length > 0) {
        return provider.models[0].id;
    }
    // Ultimate fallback
    if (providerId === 'google-gemini') {
        return 'gemini-2.5-flash-image';
    }
    if (providerId === 'openai-gpt-image') {
        return 'gpt-image-1';
    }
    return '';
}
/**
 * Convert provider result to our response format, including provider metadata
 */
function toResponse(result, providerId, modelId) {
    if (!result.success) {
        return { success: false, error: result.error };
    }
    return {
        success: true,
        image_url: result.image_url,
        file_path: result.file_path,
        filename: result.filename,
        mime_type: result.mime_type,
        ...(result.images && { images: result.images }),
        provider_used: providerId,
        model_used: modelId
    };
}
/**
 * Edit an existing image using AI.
 *
 * Uses the configured provider (Google Gemini, OpenAI GPT Image, etc.) or allows
 * explicit override via the provider/model parameters.
 *
 * The edited image is saved to disk and a URL is returned for display in the chat UI.
 * This avoids context window limitations with large base64 strings.
 *
 * @param input - The editing parameters
 * @returns The edited image URL and file path, or error details
 *
 * @example
 * ```typescript
 * // Edit using default provider
 * const result = await editImage({
 *   prompt: 'Add a cat sitting on the couch',
 *   image_path: '/path/to/living-room.png'
 * });
 *
 * // Edit using specific provider
 * const result = await editImage({
 *   prompt: 'Make it black and white',
 *   image_path: '/path/to/image.png',
 *   provider: 'openai-gpt-image',
 *   model: 'gpt-image-1'
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
    // Determine provider
    const providerId = getProviderId(input.provider);
    const provider = registry.getImageProvider(providerId);
    if (!provider) {
        const availableProviders = registry.listImageProviders().map(p => p.id);
        return {
            success: false,
            error: `Image provider '${providerId}' not found. Available providers: ${availableProviders.join(', ')}`
        };
    }
    // Check if provider supports editing
    if (!provider.edit) {
        return {
            success: false,
            error: `Provider '${provider.name}' does not support image editing. Try a different provider.`
        };
    }
    // Get API key
    const apiKey = getApiKey(providerId);
    if (!apiKey) {
        return {
            success: false,
            error: `No API key configured for ${provider.name}. Set IMAGE_API_KEY or provider-specific key.`
        };
    }
    // Get model
    const modelId = getModelId(providerId, input.model);
    const modelInfo = provider.models.find(m => m.id === modelId);
    if (!modelInfo) {
        const availableModels = provider.models.map(m => m.id);
        return {
            success: false,
            error: `Model '${modelId}' not found for ${provider.name}. Available models: ${availableModels.join(', ')}`
        };
    }
    // Check if model supports editing
    if (!modelInfo.capabilities.includes('image-edit')) {
        return {
            success: false,
            error: `Model '${modelId}' does not support image editing. Try a different model.`
        };
    }
    // Build credentials
    const credentials = { apiKey };
    // Delegate to provider
    const result = await provider.edit({
        prompt: input.prompt,
        source_image: input.image_path,
        mask_image: input.mask_path,
        aspect_ratio: input.aspect_ratio,
        image_size: input.image_size,
        number_of_images: input.number_of_images,
        person_generation: input.person_generation
    }, credentials, modelId);
    return toResponse(result, providerId, modelId);
}
export default editImage;
//# sourceMappingURL=editImage.js.map
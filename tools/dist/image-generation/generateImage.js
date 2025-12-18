/**
 * Image Generation Tool - Unified Provider Interface
 *
 * Generate images using AI from multiple providers:
 * - Google Gemini (Nano Banana): gemini-2.5-flash-image, gemini-3-pro-image-preview
 * - OpenAI GPT Image: gpt-image-1 (GPT-4o native image generation)
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
function toResponse(result, providerId, modelId, capabilities) {
    if (!result.success) {
        return { success: false, error: result.error };
    }
    // Determine which providers can edit this image
    const supportsEdit = capabilities.includes('image-edit');
    const editWith = supportsEdit
        ? providerId // Same provider can edit
        : 'google-gemini or openai-gpt-image'; // Suggest providers that support editing
    return {
        success: true,
        image_url: result.image_url,
        file_path: result.file_path,
        filename: result.filename,
        mime_type: result.mime_type,
        ...(result.images && { images: result.images }),
        provider_used: providerId,
        model_used: modelId,
        capabilities: capabilities,
        edit_with: editWith
    };
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
export async function generateImage(input) {
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
    // Build credentials
    const credentials = { apiKey };
    // Delegate to provider
    const result = await provider.generate({
        prompt: input.prompt,
        aspect_ratio: input.aspect_ratio,
        image_size: input.image_size,
        number_of_images: input.number_of_images,
        person_generation: input.person_generation
    }, credentials, modelId);
    // Get capabilities from the model info
    const capabilities = modelInfo.capabilities.map(c => c.toString());
    return toResponse(result, providerId, modelId, capabilities);
}
export default generateImage;
//# sourceMappingURL=generateImage.js.map
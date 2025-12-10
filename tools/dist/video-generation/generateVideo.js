/**
 * Video Generation Tool - Unified Provider Interface
 *
 * Generate videos using AI from multiple providers (Google Veo, OpenAI Sora, etc.)
 * The provider and model are selected based on environment variables or explicit input.
 * Videos are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   VIDEO_PROVIDER - Provider ID (e.g., "google-veo", "openai-sora")
 *   VIDEO_API_KEY - API key for the selected provider
 *   VIDEO_MODEL - Model to use (e.g., "veo-3.1-fast-generate-preview", "sora-2")
 *
 *   Legacy (backwards compatible):
 *   GEMINI_API_KEY - Google AI API key (used if VIDEO_API_KEY not set)
 *   VEO_MODEL - Veo model (used if VIDEO_MODEL not set for Google provider)
 *   OPENAI_API_KEY - OpenAI API key (used for Sora if VIDEO_API_KEY not set)
 *
 * Usage:
 *   import { generateVideo } from '/opt/ai-tools/dist/video-generation/generateVideo.js';
 *
 *   // Use default provider from settings
 *   const result = await generateVideo({
 *     prompt: 'A cat playing with a ball of yarn'
 *   });
 *
 *   // Or explicitly specify provider/model
 *   const result = await generateVideo({
 *     prompt: 'A cat playing with a ball of yarn',
 *     provider: 'openai-sora',
 *     model: 'sora-2-pro'
 *   });
 *
 *   if (result.success) {
 *     // result.video_url contains the URL to access the video
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
    if (process.env.VIDEO_PROVIDER) {
        return process.env.VIDEO_PROVIDER;
    }
    // Legacy: If OPENAI_API_KEY is set but not GEMINI_API_KEY, default to Sora
    if (process.env.OPENAI_API_KEY && !process.env.GEMINI_API_KEY) {
        return 'openai-sora';
    }
    // Default to Google Veo
    return 'google-veo';
}
/**
 * Get API key for the specified provider
 */
function getApiKey(providerId) {
    // Check provider-specific env var first
    if (process.env.VIDEO_API_KEY) {
        return process.env.VIDEO_API_KEY;
    }
    // Legacy fallbacks based on provider
    if (providerId === 'google-veo') {
        return process.env.GEMINI_API_KEY || '';
    }
    if (providerId === 'openai-sora') {
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
    if (process.env.VIDEO_MODEL) {
        return process.env.VIDEO_MODEL;
    }
    // Legacy fallbacks
    if (providerId === 'google-veo' && process.env.VEO_MODEL) {
        return process.env.VEO_MODEL;
    }
    // Provider defaults
    const provider = registry.getVideoProvider(providerId);
    if (provider && provider.models.length > 0) {
        return provider.models[0].id;
    }
    // Ultimate fallbacks
    if (providerId === 'google-veo') {
        return 'veo-3.1-fast-generate-preview';
    }
    if (providerId === 'openai-sora') {
        return 'sora-2';
    }
    return '';
}
/**
 * Generate a video from a text prompt.
 *
 * Uses the configured provider (Google Veo, OpenAI Sora, etc.) or allows
 * explicit override via the provider/model parameters.
 *
 * The video is saved to disk and a URL is returned for display in the chat UI.
 * Video generation is asynchronous - this function polls until complete.
 *
 * @param input - The generation parameters
 * @returns The generated video URL and file path, or error details
 *
 * @example
 * ```typescript
 * // Use default provider
 * const result = await generateVideo({
 *   prompt: 'A butterfly landing on a flower, macro shot, cinematic',
 *   duration: 8,
 *   aspect_ratio: '16:9'
 * });
 *
 * // Use specific provider
 * const result = await generateVideo({
 *   prompt: 'A butterfly landing on a flower',
 *   provider: 'openai-sora',
 *   model: 'sora-2-pro'
 * });
 *
 * if (result.success) {
 *   console.log('Video URL:', result.video_url);
 *   console.log('Saved to:', result.file_path);
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export async function generateVideo(input) {
    // Determine provider
    const providerId = getProviderId(input.provider);
    const provider = registry.getVideoProvider(providerId);
    if (!provider) {
        const availableProviders = registry.listVideoProviders().map(p => p.id);
        return {
            success: false,
            error: `Video provider '${providerId}' not found. Available providers: ${availableProviders.join(', ')}`
        };
    }
    // Get API key
    const apiKey = getApiKey(providerId);
    if (!apiKey) {
        return {
            success: false,
            error: `No API key configured for ${provider.name}. Set VIDEO_API_KEY or provider-specific key.`
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
        duration: input.duration,
        resolution: input.resolution,
        negative_prompt: input.negative_prompt,
        seed: input.seed,
        person_generation: input.person_generation
    }, credentials, modelId);
    // Add provider metadata to the result
    if (result.success) {
        const capabilities = modelInfo.capabilities.map(c => c.toString());
        const supportsExtend = capabilities.includes('video-extend');
        result.provider_metadata = {
            ...result.provider_metadata,
            provider_used: providerId,
            model_used: modelId,
            capabilities: capabilities,
            can_extend: supportsExtend,
            extend_with: supportsExtend ? providerId : 'google-veo (Veo only supports extend)'
        };
    }
    return result;
}
export default generateVideo;
//# sourceMappingURL=generateVideo.js.map
/**
 * Speech-to-Text Tool
 *
 * Transcribes audio files to text using AI models.
 *
 * Usage:
 * ```typescript
 * import { speechToText } from '/opt/ai-tools/dist/audio-generation/speechToText.js';
 * const result = await speechToText({
 *   audio_path: '/path/to/audio.mp3',
 *   language: 'en'  // Optional
 * });
 * console.log(JSON.stringify(result));
 * ```
 */
import { registry } from '../providers/registry.js';
// ============================================================================
// Helper Functions
// ============================================================================
function getProviderId(inputProvider) {
    if (inputProvider)
        return inputProvider;
    if (process.env.AUDIO_PROVIDER)
        return process.env.AUDIO_PROVIDER;
    return 'openai-audio';
}
function getApiKey(providerId) {
    if (process.env.AUDIO_API_KEY)
        return process.env.AUDIO_API_KEY;
    if (process.env.OPENAI_API_KEY)
        return process.env.OPENAI_API_KEY;
    return '';
}
function getModelId(providerId, inputModel) {
    if (inputModel)
        return inputModel;
    if (process.env.AUDIO_MODEL)
        return process.env.AUDIO_MODEL;
    const provider = registry.getAudioProvider(providerId);
    if (provider && provider.models.length > 0) {
        // Find first STT model
        const sttModel = provider.models.find(m => m.capabilities.includes('speech-to-text'));
        if (sttModel)
            return sttModel.id;
    }
    // Default to the best transcription model
    return 'gpt-4o-transcribe';
}
// ============================================================================
// Main Function
// ============================================================================
export async function speechToText(input) {
    // Validate input
    if (!input.audio_path) {
        return { success: false, error: 'Audio path is required' };
    }
    // Resolve provider, API key, model
    const providerId = getProviderId(input.provider);
    const apiKey = getApiKey(providerId);
    const modelId = getModelId(providerId, input.model);
    if (!apiKey) {
        return {
            success: false,
            error: 'No API key configured. Set AUDIO_API_KEY or OPENAI_API_KEY environment variable.'
        };
    }
    // Get provider
    const provider = registry.getAudioProvider(providerId);
    if (!provider) {
        return {
            success: false,
            error: `Audio provider '${providerId}' not found. Available: ${registry.listAudioProviders().map(p => p.id).join(', ')}`
        };
    }
    // Check if provider supports STT
    if (!provider.speechToText) {
        return {
            success: false,
            error: `Provider '${providerId}' does not support speech-to-text.`
        };
    }
    // Call provider
    const result = await provider.speechToText({
        audio_path: input.audio_path,
        language: input.language,
        prompt: input.prompt,
        response_format: input.response_format,
        timestamp_granularities: input.timestamp_granularities
    }, { apiKey }, modelId);
    return {
        success: result.success,
        text: result.text,
        language: result.language,
        duration_seconds: result.duration_seconds,
        words: result.words,
        segments: result.segments,
        error: result.error
    };
}
export default speechToText;
//# sourceMappingURL=speechToText.js.map
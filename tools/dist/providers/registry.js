/**
 * Provider Registry
 *
 * Central registry for all AI generation providers.
 * Providers register themselves on import, and tools query the registry
 * to get the appropriate provider based on configuration.
 */
/**
 * Central registry for managing AI providers
 */
class ProviderRegistry {
    imageProviders = new Map();
    videoProviders = new Map();
    audioProviders = new Map();
    videoAnalysisProviders = new Map();
    // ============================================================================
    // Registration
    // ============================================================================
    /**
     * Register an image generation provider
     */
    registerImageProvider(provider) {
        if (this.imageProviders.has(provider.id)) {
            console.warn(`Image provider '${provider.id}' is already registered. Overwriting.`);
        }
        this.imageProviders.set(provider.id, provider);
    }
    /**
     * Register a video generation provider
     */
    registerVideoProvider(provider) {
        if (this.videoProviders.has(provider.id)) {
            console.warn(`Video provider '${provider.id}' is already registered. Overwriting.`);
        }
        this.videoProviders.set(provider.id, provider);
    }
    /**
     * Register an audio provider
     */
    registerAudioProvider(provider) {
        if (this.audioProviders.has(provider.id)) {
            console.warn(`Audio provider '${provider.id}' is already registered. Overwriting.`);
        }
        this.audioProviders.set(provider.id, provider);
    }
    /**
     * Register a video analysis provider
     */
    registerVideoAnalysisProvider(provider) {
        if (this.videoAnalysisProviders.has(provider.id)) {
            console.warn(`Video analysis provider '${provider.id}' is already registered. Overwriting.`);
        }
        this.videoAnalysisProviders.set(provider.id, provider);
    }
    // ============================================================================
    // Retrieval
    // ============================================================================
    /**
     * Get an image provider by ID
     */
    getImageProvider(id) {
        return this.imageProviders.get(id);
    }
    /**
     * Get a video provider by ID
     */
    getVideoProvider(id) {
        return this.videoProviders.get(id);
    }
    /**
     * Get an audio provider by ID
     */
    getAudioProvider(id) {
        return this.audioProviders.get(id);
    }
    /**
     * Get a video analysis provider by ID
     */
    getVideoAnalysisProvider(id) {
        return this.videoAnalysisProviders.get(id);
    }
    /**
     * List all registered image providers
     */
    listImageProviders() {
        return Array.from(this.imageProviders.values());
    }
    /**
     * List all registered video providers
     */
    listVideoProviders() {
        return Array.from(this.videoProviders.values());
    }
    /**
     * List all registered audio providers
     */
    listAudioProviders() {
        return Array.from(this.audioProviders.values());
    }
    /**
     * List all registered video analysis providers
     */
    listVideoAnalysisProviders() {
        return Array.from(this.videoAnalysisProviders.values());
    }
    // ============================================================================
    // Model Discovery
    // ============================================================================
    /**
     * Get all available image models across all providers
     */
    getAllImageModels() {
        const models = [];
        for (const provider of this.imageProviders.values()) {
            for (const model of provider.models) {
                models.push({ ...model, providerId: provider.id });
            }
        }
        return models;
    }
    /**
     * Get all available video models across all providers
     */
    getAllVideoModels() {
        const models = [];
        for (const provider of this.videoProviders.values()) {
            for (const model of provider.models) {
                models.push({ ...model, providerId: provider.id });
            }
        }
        return models;
    }
    /**
     * Find which provider owns a specific model
     */
    findImageProviderByModel(modelId) {
        for (const provider of this.imageProviders.values()) {
            if (provider.models.some(m => m.id === modelId)) {
                return provider;
            }
        }
        return undefined;
    }
    /**
     * Find which provider owns a specific video model
     */
    findVideoProviderByModel(modelId) {
        for (const provider of this.videoProviders.values()) {
            if (provider.models.some(m => m.id === modelId)) {
                return provider;
            }
        }
        return undefined;
    }
    // ============================================================================
    // Helpers
    // ============================================================================
    /**
     * Check if a provider supports a specific capability
     */
    imageProviderSupports(providerId, capability) {
        const provider = this.imageProviders.get(providerId);
        if (!provider)
            return false;
        return provider.models.some(m => m.capabilities.includes(capability));
    }
    /**
     * Check if a video provider supports a specific capability
     */
    videoProviderSupports(providerId, capability) {
        const provider = this.videoProviders.get(providerId);
        if (!provider)
            return false;
        return provider.models.some(m => m.capabilities.includes(capability));
    }
}
// Singleton instance
export const registry = new ProviderRegistry();
// ============================================================================
// Auto-registration of providers
// ============================================================================
// Import and register all providers
// These imports will trigger provider registration via side effects
// Image providers
import { googleGeminiProvider } from './image/google-gemini.js';
import { googleImagenProvider } from './image/google-imagen.js';
import { openaiGptImageProvider } from './image/openai-gpt-image.js';
// Video providers
import { googleVeoProvider } from './video/google-veo.js';
import { googleGeminiVideoProvider } from './video/google-gemini-video.js';
import { openaiSoraProvider } from './video/openai-sora.js';
// Audio providers (for app features like TTS/STT, not model tools)
import { openaiAudioProvider } from './audio/openai-audio.js';
// Register image providers
registry.registerImageProvider(googleGeminiProvider);
registry.registerImageProvider(googleImagenProvider);
registry.registerImageProvider(openaiGptImageProvider);
// Register video providers
registry.registerVideoProvider(googleVeoProvider);
registry.registerVideoProvider(openaiSoraProvider);
// Register video analysis providers
registry.registerVideoAnalysisProvider(googleGeminiVideoProvider);
// Register audio providers (for app features)
registry.registerAudioProvider(openaiAudioProvider);
// Future providers:
// import { stabilityAiProvider } from './image/stability-ai.js';
// import { runwayMlProvider } from './video/runway-ml.js';
// import { elevenlabsProvider } from './audio/elevenlabs.js';
// registry.registerImageProvider(stabilityAiProvider);
// registry.registerVideoProvider(runwayMlProvider);
// registry.registerAudioProvider(elevenlabsProvider);
export default registry;
//# sourceMappingURL=registry.js.map
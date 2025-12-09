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
import { googleGeminiProvider } from './image/google-gemini.js';
import { googleVeoProvider } from './video/google-veo.js';
import { openaiSoraProvider } from './video/openai-sora.js';
// Register image providers
registry.registerImageProvider(googleGeminiProvider);
// Register video providers
registry.registerVideoProvider(googleVeoProvider);
registry.registerVideoProvider(openaiSoraProvider);
// Future providers:
// import { openaiDalleProvider } from './image/openai-dalle.js';
// import { stabilityAiProvider } from './image/stability-ai.js';
// import { runwayMlProvider } from './video/runway-ml.js';
// registry.registerImageProvider(openaiDalleProvider);
// registry.registerImageProvider(stabilityAiProvider);
// registry.registerVideoProvider(runwayMlProvider);
export default registry;
//# sourceMappingURL=registry.js.map
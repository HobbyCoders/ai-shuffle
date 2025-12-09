/**
 * Provider Registry
 *
 * Central registry for all AI generation providers.
 * Providers register themselves on import, and tools query the registry
 * to get the appropriate provider based on configuration.
 */
import { ImageProvider, VideoProvider, ModelInfo } from './types.js';
/**
 * Central registry for managing AI providers
 */
declare class ProviderRegistry {
    private imageProviders;
    private videoProviders;
    /**
     * Register an image generation provider
     */
    registerImageProvider(provider: ImageProvider): void;
    /**
     * Register a video generation provider
     */
    registerVideoProvider(provider: VideoProvider): void;
    /**
     * Get an image provider by ID
     */
    getImageProvider(id: string): ImageProvider | undefined;
    /**
     * Get a video provider by ID
     */
    getVideoProvider(id: string): VideoProvider | undefined;
    /**
     * List all registered image providers
     */
    listImageProviders(): ImageProvider[];
    /**
     * List all registered video providers
     */
    listVideoProviders(): VideoProvider[];
    /**
     * Get all available image models across all providers
     */
    getAllImageModels(): Array<ModelInfo & {
        providerId: string;
    }>;
    /**
     * Get all available video models across all providers
     */
    getAllVideoModels(): Array<ModelInfo & {
        providerId: string;
    }>;
    /**
     * Find which provider owns a specific model
     */
    findImageProviderByModel(modelId: string): ImageProvider | undefined;
    /**
     * Find which provider owns a specific video model
     */
    findVideoProviderByModel(modelId: string): VideoProvider | undefined;
    /**
     * Check if a provider supports a specific capability
     */
    imageProviderSupports(providerId: string, capability: string): boolean;
    /**
     * Check if a video provider supports a specific capability
     */
    videoProviderSupports(providerId: string, capability: string): boolean;
}
export declare const registry: ProviderRegistry;
export default registry;
//# sourceMappingURL=registry.d.ts.map
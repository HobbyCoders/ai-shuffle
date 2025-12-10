/**
 * Provider Registry
 *
 * Central registry for all AI generation providers.
 * Providers register themselves on import, and tools query the registry
 * to get the appropriate provider based on configuration.
 */
import { ImageProvider, VideoProvider, AudioProvider, VideoAnalysisProvider, ModelInfo } from './types.js';
/**
 * Central registry for managing AI providers
 */
declare class ProviderRegistry {
    private imageProviders;
    private videoProviders;
    private audioProviders;
    private videoAnalysisProviders;
    /**
     * Register an image generation provider
     */
    registerImageProvider(provider: ImageProvider): void;
    /**
     * Register a video generation provider
     */
    registerVideoProvider(provider: VideoProvider): void;
    /**
     * Register an audio provider
     */
    registerAudioProvider(provider: AudioProvider): void;
    /**
     * Register a video analysis provider
     */
    registerVideoAnalysisProvider(provider: VideoAnalysisProvider): void;
    /**
     * Get an image provider by ID
     */
    getImageProvider(id: string): ImageProvider | undefined;
    /**
     * Get a video provider by ID
     */
    getVideoProvider(id: string): VideoProvider | undefined;
    /**
     * Get an audio provider by ID
     */
    getAudioProvider(id: string): AudioProvider | undefined;
    /**
     * Get a video analysis provider by ID
     */
    getVideoAnalysisProvider(id: string): VideoAnalysisProvider | undefined;
    /**
     * List all registered image providers
     */
    listImageProviders(): ImageProvider[];
    /**
     * List all registered video providers
     */
    listVideoProviders(): VideoProvider[];
    /**
     * List all registered audio providers
     */
    listAudioProviders(): AudioProvider[];
    /**
     * List all registered video analysis providers
     */
    listVideoAnalysisProviders(): VideoAnalysisProvider[];
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
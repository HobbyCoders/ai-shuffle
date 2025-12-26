/**
 * Provider Registry
 *
 * Central registry for all AI generation providers.
 * Providers register themselves on import, and tools query the registry
 * to get the appropriate provider based on configuration.
 */
import { ImageProvider, VideoProvider, AudioProvider, VideoAnalysisProvider, Model3DProvider, ModelInfo } from './types.js';
/**
 * Central registry for managing AI providers
 */
declare class ProviderRegistry {
    private imageProviders;
    private videoProviders;
    private audioProviders;
    private videoAnalysisProviders;
    private model3DProviders;
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
     * Register a 3D model provider
     */
    registerModel3DProvider(provider: Model3DProvider): void;
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
     * Get a 3D model provider by ID
     */
    getModel3DProvider(id: string): Model3DProvider | undefined;
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
     * List all registered 3D model providers
     */
    listModel3DProviders(): Model3DProvider[];
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
     * Get all available 3D models across all providers
     */
    getAllModel3DModels(): Array<ModelInfo & {
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
     * Find which provider owns a specific 3D model
     */
    findModel3DProviderByModel(modelId: string): Model3DProvider | undefined;
    /**
     * Check if a provider supports a specific capability
     */
    imageProviderSupports(providerId: string, capability: string): boolean;
    /**
     * Check if a video provider supports a specific capability
     */
    videoProviderSupports(providerId: string, capability: string): boolean;
    /**
     * Check if a 3D model provider supports a specific capability
     */
    model3DProviderSupports(providerId: string, capability: string): boolean;
}
export declare const registry: ProviderRegistry;
export default registry;
//# sourceMappingURL=registry.d.ts.map
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
class ProviderRegistry {
  private imageProviders = new Map<string, ImageProvider>();
  private videoProviders = new Map<string, VideoProvider>();
  private audioProviders = new Map<string, AudioProvider>();
  private videoAnalysisProviders = new Map<string, VideoAnalysisProvider>();
  private model3DProviders = new Map<string, Model3DProvider>();

  // ============================================================================
  // Registration
  // ============================================================================

  /**
   * Register an image generation provider
   */
  registerImageProvider(provider: ImageProvider): void {
    if (this.imageProviders.has(provider.id)) {
      console.warn(`Image provider '${provider.id}' is already registered. Overwriting.`);
    }
    this.imageProviders.set(provider.id, provider);
  }

  /**
   * Register a video generation provider
   */
  registerVideoProvider(provider: VideoProvider): void {
    if (this.videoProviders.has(provider.id)) {
      console.warn(`Video provider '${provider.id}' is already registered. Overwriting.`);
    }
    this.videoProviders.set(provider.id, provider);
  }

  /**
   * Register an audio provider
   */
  registerAudioProvider(provider: AudioProvider): void {
    if (this.audioProviders.has(provider.id)) {
      console.warn(`Audio provider '${provider.id}' is already registered. Overwriting.`);
    }
    this.audioProviders.set(provider.id, provider);
  }

  /**
   * Register a video analysis provider
   */
  registerVideoAnalysisProvider(provider: VideoAnalysisProvider): void {
    if (this.videoAnalysisProviders.has(provider.id)) {
      console.warn(`Video analysis provider '${provider.id}' is already registered. Overwriting.`);
    }
    this.videoAnalysisProviders.set(provider.id, provider);
  }

  /**
   * Register a 3D model provider
   */
  registerModel3DProvider(provider: Model3DProvider): void {
    if (this.model3DProviders.has(provider.id)) {
      console.warn(`3D model provider '${provider.id}' is already registered. Overwriting.`);
    }
    this.model3DProviders.set(provider.id, provider);
  }


  // ============================================================================
  // Retrieval
  // ============================================================================

  /**
   * Get an image provider by ID
   */
  getImageProvider(id: string): ImageProvider | undefined {
    return this.imageProviders.get(id);
  }

  /**
   * Get a video provider by ID
   */
  getVideoProvider(id: string): VideoProvider | undefined {
    return this.videoProviders.get(id);
  }

  /**
   * Get an audio provider by ID
   */
  getAudioProvider(id: string): AudioProvider | undefined {
    return this.audioProviders.get(id);
  }

  /**
   * Get a video analysis provider by ID
   */
  getVideoAnalysisProvider(id: string): VideoAnalysisProvider | undefined {
    return this.videoAnalysisProviders.get(id);
  }

  /**
   * Get a 3D model provider by ID
   */
  getModel3DProvider(id: string): Model3DProvider | undefined {
    return this.model3DProviders.get(id);
  }


  /**
   * List all registered image providers
   */
  listImageProviders(): ImageProvider[] {
    return Array.from(this.imageProviders.values());
  }

  /**
   * List all registered video providers
   */
  listVideoProviders(): VideoProvider[] {
    return Array.from(this.videoProviders.values());
  }

  /**
   * List all registered audio providers
   */
  listAudioProviders(): AudioProvider[] {
    return Array.from(this.audioProviders.values());
  }

  /**
   * List all registered video analysis providers
   */
  listVideoAnalysisProviders(): VideoAnalysisProvider[] {
    return Array.from(this.videoAnalysisProviders.values());
  }

  /**
   * List all registered 3D model providers
   */
  listModel3DProviders(): Model3DProvider[] {
    return Array.from(this.model3DProviders.values());
  }


  // ============================================================================
  // Model Discovery
  // ============================================================================

  /**
   * Get all available image models across all providers
   */
  getAllImageModels(): Array<ModelInfo & { providerId: string }> {
    const models: Array<ModelInfo & { providerId: string }> = [];
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
  getAllVideoModels(): Array<ModelInfo & { providerId: string }> {
    const models: Array<ModelInfo & { providerId: string }> = [];
    for (const provider of this.videoProviders.values()) {
      for (const model of provider.models) {
        models.push({ ...model, providerId: provider.id });
      }
    }
    return models;
  }

  /**
   * Get all available 3D models across all providers
   */
  getAllModel3DModels(): Array<ModelInfo & { providerId: string }> {
    const models: Array<ModelInfo & { providerId: string }> = [];
    for (const provider of this.model3DProviders.values()) {
      for (const model of provider.models) {
        models.push({ ...model, providerId: provider.id });
      }
    }
    return models;
  }

  /**
   * Find which provider owns a specific model
   */
  findImageProviderByModel(modelId: string): ImageProvider | undefined {
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
  findVideoProviderByModel(modelId: string): VideoProvider | undefined {
    for (const provider of this.videoProviders.values()) {
      if (provider.models.some(m => m.id === modelId)) {
        return provider;
      }
    }
    return undefined;
  }

  /**
   * Find which provider owns a specific 3D model
   */
  findModel3DProviderByModel(modelId: string): Model3DProvider | undefined {
    for (const provider of this.model3DProviders.values()) {
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
  imageProviderSupports(providerId: string, capability: string): boolean {
    const provider = this.imageProviders.get(providerId);
    if (!provider) return false;
    return provider.models.some(m => m.capabilities.includes(capability as any));
  }

  /**
   * Check if a video provider supports a specific capability
   */
  videoProviderSupports(providerId: string, capability: string): boolean {
    const provider = this.videoProviders.get(providerId);
    if (!provider) return false;
    return provider.models.some(m => m.capabilities.includes(capability as any));
  }

  /**
   * Check if a 3D model provider supports a specific capability
   */
  model3DProviderSupports(providerId: string, capability: string): boolean {
    const provider = this.model3DProviders.get(providerId);
    if (!provider) return false;
    return provider.models.some(m => m.capabilities.includes(capability as any));
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

// 3D model providers
import { meshyProvider } from './model3d/meshy.js';

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

// Register 3D model providers
registry.registerModel3DProvider(meshyProvider);

// Future providers:
// import { stabilityAiProvider } from './image/stability-ai.js';
// import { runwayMlProvider } from './video/runway-ml.js';
// import { elevenlabsProvider } from './audio/elevenlabs.js';
// registry.registerImageProvider(stabilityAiProvider);
// registry.registerVideoProvider(runwayMlProvider);
// registry.registerAudioProvider(elevenlabsProvider);

export default registry;

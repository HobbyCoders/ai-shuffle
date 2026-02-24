/**
 * Provider Registry
 *
 * Central registry for all AI generation providers.
 * All providers use fal.ai as the sole backend through a single API key.
 */

import { ImageProvider, VideoProvider, AudioProvider, Model3DProvider, ModelInfo } from './types.js';

/**
 * Central registry for managing AI providers
 */
class ProviderRegistry {
  private imageProviders = new Map<string, ImageProvider>();
  private videoProviders = new Map<string, VideoProvider>();
  private audioProviders = new Map<string, AudioProvider>();
  private model3DProviders = new Map<string, Model3DProvider>();

  // ============================================================================
  // Registration
  // ============================================================================

  registerImageProvider(provider: ImageProvider): void {
    if (this.imageProviders.has(provider.id)) {
      console.warn(`Image provider '${provider.id}' is already registered. Overwriting.`);
    }
    this.imageProviders.set(provider.id, provider);
  }

  registerVideoProvider(provider: VideoProvider): void {
    if (this.videoProviders.has(provider.id)) {
      console.warn(`Video provider '${provider.id}' is already registered. Overwriting.`);
    }
    this.videoProviders.set(provider.id, provider);
  }

  registerAudioProvider(provider: AudioProvider): void {
    if (this.audioProviders.has(provider.id)) {
      console.warn(`Audio provider '${provider.id}' is already registered. Overwriting.`);
    }
    this.audioProviders.set(provider.id, provider);
  }

  registerModel3DProvider(provider: Model3DProvider): void {
    if (this.model3DProviders.has(provider.id)) {
      console.warn(`3D model provider '${provider.id}' is already registered. Overwriting.`);
    }
    this.model3DProviders.set(provider.id, provider);
  }

  // ============================================================================
  // Retrieval
  // ============================================================================

  getImageProvider(id: string): ImageProvider | undefined {
    return this.imageProviders.get(id);
  }

  getVideoProvider(id: string): VideoProvider | undefined {
    return this.videoProviders.get(id);
  }

  getAudioProvider(id: string): AudioProvider | undefined {
    return this.audioProviders.get(id);
  }

  getModel3DProvider(id: string): Model3DProvider | undefined {
    return this.model3DProviders.get(id);
  }

  listImageProviders(): ImageProvider[] {
    return Array.from(this.imageProviders.values());
  }

  listVideoProviders(): VideoProvider[] {
    return Array.from(this.videoProviders.values());
  }

  listAudioProviders(): AudioProvider[] {
    return Array.from(this.audioProviders.values());
  }

  listModel3DProviders(): Model3DProvider[] {
    return Array.from(this.model3DProviders.values());
  }

  // ============================================================================
  // Model Discovery
  // ============================================================================

  getAllImageModels(): Array<ModelInfo & { providerId: string }> {
    const models: Array<ModelInfo & { providerId: string }> = [];
    for (const provider of this.imageProviders.values()) {
      for (const model of provider.models) {
        models.push({ ...model, providerId: provider.id });
      }
    }
    return models;
  }

  getAllVideoModels(): Array<ModelInfo & { providerId: string }> {
    const models: Array<ModelInfo & { providerId: string }> = [];
    for (const provider of this.videoProviders.values()) {
      for (const model of provider.models) {
        models.push({ ...model, providerId: provider.id });
      }
    }
    return models;
  }

  getAllModel3DModels(): Array<ModelInfo & { providerId: string }> {
    const models: Array<ModelInfo & { providerId: string }> = [];
    for (const provider of this.model3DProviders.values()) {
      for (const model of provider.models) {
        models.push({ ...model, providerId: provider.id });
      }
    }
    return models;
  }

  findImageProviderByModel(modelId: string): ImageProvider | undefined {
    for (const provider of this.imageProviders.values()) {
      if (provider.models.some(m => m.id === modelId)) {
        return provider;
      }
    }
    return undefined;
  }

  findVideoProviderByModel(modelId: string): VideoProvider | undefined {
    for (const provider of this.videoProviders.values()) {
      if (provider.models.some(m => m.id === modelId)) {
        return provider;
      }
    }
    return undefined;
  }

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

  imageProviderSupports(providerId: string, capability: string): boolean {
    const provider = this.imageProviders.get(providerId);
    if (!provider) return false;
    return provider.models.some(m => m.capabilities.includes(capability as any));
  }

  videoProviderSupports(providerId: string, capability: string): boolean {
    const provider = this.videoProviders.get(providerId);
    if (!provider) return false;
    return provider.models.some(m => m.capabilities.includes(capability as any));
  }

  model3DProviderSupports(providerId: string, capability: string): boolean {
    const provider = this.model3DProviders.get(providerId);
    if (!provider) return false;
    return provider.models.some(m => m.capabilities.includes(capability as any));
  }
}

// Singleton instance
export const registry = new ProviderRegistry();

// ============================================================================
// Provider Registration — fal.ai only
// ============================================================================

// fal.ai providers (single API key covers all modalities)
import { falImageProvider } from './image/fal-ai.js';
import { falVideoProvider } from './video/fal-ai.js';
import { fal3DProvider } from './model3d/fal-ai.js';
import { falAudioProvider } from './audio/fal-ai.js';

registry.registerImageProvider(falImageProvider);
registry.registerVideoProvider(falVideoProvider);
registry.registerModel3DProvider(fal3DProvider);
registry.registerAudioProvider(falAudioProvider);

export default registry;

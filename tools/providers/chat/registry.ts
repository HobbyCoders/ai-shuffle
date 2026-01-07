/**
 * Chat Provider Registry
 *
 * Central registry for LLM chat providers.
 * Enables Claude to collaborate with other AI models for specialized tasks.
 */

import { ChatProvider, ChatModelInfo, ChatCapability } from './types.js';

/**
 * Registry for managing chat providers
 */
class ChatProviderRegistry {
  private providers = new Map<string, ChatProvider>();

  // ============================================================================
  // Registration
  // ============================================================================

  /**
   * Register a chat provider
   */
  register(provider: ChatProvider): void {
    if (this.providers.has(provider.id)) {
      console.warn(`Chat provider '${provider.id}' is already registered. Overwriting.`);
    }
    this.providers.set(provider.id, provider);
  }

  /**
   * Unregister a chat provider
   */
  unregister(providerId: string): boolean {
    return this.providers.delete(providerId);
  }

  // ============================================================================
  // Retrieval
  // ============================================================================

  /**
   * Get a chat provider by ID
   */
  get(id: string): ChatProvider | undefined {
    return this.providers.get(id);
  }

  /**
   * Get a chat provider, throwing if not found
   */
  getOrThrow(id: string): ChatProvider {
    const provider = this.providers.get(id);
    if (!provider) {
      const available = this.listIds().join(', ');
      throw new Error(`Chat provider '${id}' not found. Available providers: ${available || 'none'}`);
    }
    return provider;
  }

  /**
   * List all registered provider IDs
   */
  listIds(): string[] {
    return Array.from(this.providers.keys());
  }

  /**
   * List all registered providers
   */
  list(): ChatProvider[] {
    return Array.from(this.providers.values());
  }

  /**
   * Check if a provider is registered
   */
  has(id: string): boolean {
    return this.providers.has(id);
  }

  // ============================================================================
  // Model Discovery
  // ============================================================================

  /**
   * Get all available models across all providers
   */
  getAllModels(): Array<ChatModelInfo & { providerId: string }> {
    const models: Array<ChatModelInfo & { providerId: string }> = [];
    for (const provider of this.providers.values()) {
      for (const model of provider.models) {
        models.push({ ...model, providerId: provider.id });
      }
    }
    return models;
  }

  /**
   * Get models filtered by capability
   */
  getModelsByCapability(capability: ChatCapability): Array<ChatModelInfo & { providerId: string }> {
    return this.getAllModels().filter(m => m.capabilities.includes(capability));
  }

  /**
   * Find which provider owns a specific model
   */
  findProviderByModel(modelId: string): ChatProvider | undefined {
    for (const provider of this.providers.values()) {
      if (provider.models.some(m => m.id === modelId)) {
        return provider;
      }
    }
    return undefined;
  }

  /**
   * Get the default model for a provider
   */
  getDefaultModel(providerId: string): string | undefined {
    const provider = this.providers.get(providerId);
    if (!provider || provider.models.length === 0) {
      return undefined;
    }
    // Return the first model as default
    return provider.models[0].id;
  }

  // ============================================================================
  // Capability Checking
  // ============================================================================

  /**
   * Check if a provider supports a specific capability
   */
  providerSupports(providerId: string, capability: ChatCapability): boolean {
    const provider = this.providers.get(providerId);
    if (!provider) return false;
    return provider.models.some(m => m.capabilities.includes(capability));
  }

  /**
   * Get providers that support a specific capability
   */
  getProvidersByCapability(capability: ChatCapability): ChatProvider[] {
    return this.list().filter(provider =>
      provider.models.some(m => m.capabilities.includes(capability))
    );
  }

  /**
   * Check if a specific model supports a capability
   */
  modelSupports(providerId: string, modelId: string, capability: ChatCapability): boolean {
    const provider = this.providers.get(providerId);
    if (!provider) return false;
    const model = provider.models.find(m => m.id === modelId);
    if (!model) return false;
    return model.capabilities.includes(capability);
  }

  // ============================================================================
  // Provider Selection
  // ============================================================================

  /**
   * Get the best provider for a specific task
   * Priority: DeepSeek for reasoning, Gemini for long context, OpenAI as default
   */
  getBestProviderForTask(task: 'reasoning' | 'memory' | 'review' | 'general'): ChatProvider | undefined {
    switch (task) {
      case 'reasoning':
        // Prefer DeepSeek for reasoning tasks
        return this.get('deepseek') || this.get('openai') || this.list()[0];

      case 'memory':
        // Prefer Gemini for long context memory tasks
        return this.get('google') || this.get('openai') || this.list()[0];

      case 'review':
        // OpenAI is good for structured review
        return this.get('openai') || this.get('google') || this.list()[0];

      case 'general':
      default:
        // Default to OpenAI, then Google, then first available
        return this.get('openai') || this.get('google') || this.list()[0];
    }
  }

  // ============================================================================
  // Statistics
  // ============================================================================

  /**
   * Get registry statistics
   */
  getStats(): {
    providerCount: number;
    modelCount: number;
    providers: Array<{ id: string; name: string; modelCount: number }>;
  } {
    const providers = this.list().map(p => ({
      id: p.id,
      name: p.name,
      modelCount: p.models.length
    }));

    return {
      providerCount: this.providers.size,
      modelCount: this.getAllModels().length,
      providers
    };
  }
}

// Singleton instance
export const chatRegistry = new ChatProviderRegistry();

export default chatRegistry;

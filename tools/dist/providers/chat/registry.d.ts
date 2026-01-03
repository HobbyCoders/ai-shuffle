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
declare class ChatProviderRegistry {
    private providers;
    /**
     * Register a chat provider
     */
    register(provider: ChatProvider): void;
    /**
     * Unregister a chat provider
     */
    unregister(providerId: string): boolean;
    /**
     * Get a chat provider by ID
     */
    get(id: string): ChatProvider | undefined;
    /**
     * Get a chat provider, throwing if not found
     */
    getOrThrow(id: string): ChatProvider;
    /**
     * List all registered provider IDs
     */
    listIds(): string[];
    /**
     * List all registered providers
     */
    list(): ChatProvider[];
    /**
     * Check if a provider is registered
     */
    has(id: string): boolean;
    /**
     * Get all available models across all providers
     */
    getAllModels(): Array<ChatModelInfo & {
        providerId: string;
    }>;
    /**
     * Get models filtered by capability
     */
    getModelsByCapability(capability: ChatCapability): Array<ChatModelInfo & {
        providerId: string;
    }>;
    /**
     * Find which provider owns a specific model
     */
    findProviderByModel(modelId: string): ChatProvider | undefined;
    /**
     * Get the default model for a provider
     */
    getDefaultModel(providerId: string): string | undefined;
    /**
     * Check if a provider supports a specific capability
     */
    providerSupports(providerId: string, capability: ChatCapability): boolean;
    /**
     * Get providers that support a specific capability
     */
    getProvidersByCapability(capability: ChatCapability): ChatProvider[];
    /**
     * Check if a specific model supports a capability
     */
    modelSupports(providerId: string, modelId: string, capability: ChatCapability): boolean;
    /**
     * Get the best provider for a specific task
     * Priority: DeepSeek for reasoning, Gemini for long context, OpenAI as default
     */
    getBestProviderForTask(task: 'reasoning' | 'memory' | 'review' | 'general'): ChatProvider | undefined;
    /**
     * Get registry statistics
     */
    getStats(): {
        providerCount: number;
        modelCount: number;
        providers: Array<{
            id: string;
            name: string;
            modelCount: number;
        }>;
    };
}
export declare const chatRegistry: ChatProviderRegistry;
export default chatRegistry;
//# sourceMappingURL=registry.d.ts.map
/**
 * AI Provider Registry
 *
 * Unified interface for all AI generation providers.
 * Import the registry to access any registered provider.
 */
// Export types
export * from './types.js';
// Export registry
export { registry } from './registry.js';
export { default } from './registry.js';
// Export individual providers for direct access
export { googleGeminiProvider } from './image/google-gemini.js';
export { googleVeoProvider } from './video/google-veo.js';
export { openaiSoraProvider } from './video/openai-sora.js';
//# sourceMappingURL=index.js.map
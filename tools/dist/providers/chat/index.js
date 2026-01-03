/**
 * Chat Providers
 *
 * Export all chat provider types, registry, and implementations.
 * These providers enable Claude to collaborate with other LLMs.
 */
// Types
export * from './types.js';
// Registry
export { chatRegistry } from './registry.js';
// Providers
export { openaiChatProvider } from './openai-chat.js';
export { googleChatProvider } from './google-chat.js';
export { deepseekChatProvider } from './deepseek-chat.js';
// Register providers
import { chatRegistry } from './registry.js';
import { openaiChatProvider } from './openai-chat.js';
import { googleChatProvider } from './google-chat.js';
import { deepseekChatProvider } from './deepseek-chat.js';
chatRegistry.register(openaiChatProvider);
chatRegistry.register(googleChatProvider);
chatRegistry.register(deepseekChatProvider);
//# sourceMappingURL=index.js.map
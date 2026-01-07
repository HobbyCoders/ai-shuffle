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
// Image providers
export { googleGeminiProvider } from './image/google-gemini.js';
export { googleImagenProvider } from './image/google-imagen.js';
export { openaiGptImageProvider } from './image/openai-gpt-image.js';

// Video providers
export { googleVeoProvider } from './video/google-veo.js';
export { googleGeminiVideoProvider } from './video/google-gemini-video.js';
export { openaiSoraProvider } from './video/openai-sora.js';

// Audio providers (for app features like TTS/STT, not model tools)
export { openaiAudioProvider } from './audio/openai-audio.js';

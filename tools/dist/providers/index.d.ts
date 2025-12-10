/**
 * AI Provider Registry
 *
 * Unified interface for all AI generation providers.
 * Import the registry to access any registered provider.
 */
export * from './types.js';
export { registry } from './registry.js';
export { default } from './registry.js';
export { googleGeminiProvider } from './image/google-gemini.js';
export { googleImagenProvider } from './image/google-imagen.js';
export { openaiGptImageProvider } from './image/openai-gpt-image.js';
export { googleVeoProvider } from './video/google-veo.js';
export { googleGeminiVideoProvider } from './video/google-gemini-video.js';
export { openaiSoraProvider } from './video/openai-sora.js';
export { openaiAudioProvider } from './audio/openai-audio.js';
//# sourceMappingURL=index.d.ts.map
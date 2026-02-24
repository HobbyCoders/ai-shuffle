/**
 * AI Provider Registry
 *
 * Unified interface for all AI generation providers.
 * All providers use fal.ai as the sole backend.
 */

// Export types
export * from './types.js';

// Export registry
export { registry } from './registry.js';
export { default } from './registry.js';

// Export individual providers for direct access
export { falImageProvider } from './image/fal-ai.js';
export { falVideoProvider } from './video/fal-ai.js';
export { fal3DProvider } from './model3d/fal-ai.js';
export { falAudioProvider } from './audio/fal-ai.js';

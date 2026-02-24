/**
 * 3D Model Generation Tools
 *
 * AI-powered 3D model generation via fal.ai.
 * Configure your FAL_KEY in Settings > Integrations before use.
 *
 * Available functions:
 * - textTo3D: Generate a 3D model from a text prompt
 * - imageTo3D: Convert an image to a 3D model
 *
 * @example
 * ```typescript
 * import { textTo3D, imageTo3D } from './tools/model3d-generation';
 *
 * // Generate a 3D model from text
 * const model = await textTo3D({
 *   prompt: 'A medieval castle with stone walls',
 *   art_style: 'realistic',
 *   wait_for_completion: true
 * });
 *
 * // Convert an image to 3D
 * const fromImage = await imageTo3D({
 *   image_path: '/path/to/character.png',
 *   prompt: 'A fantasy warrior character',
 *   enable_pbr: true,
 *   wait_for_completion: true
 * });
 * ```
 */

// Shared types and utilities
export { Model3DResponse, TaskType } from './shared.js';

// Text to 3D
export {
  textTo3D,
  type TextTo3DInput,
  type TextTo3DResponse
} from './textTo3D.js';

// Image to 3D
export {
  imageTo3D,
  type ImageTo3DInput,
  type ImageTo3DResponse
} from './imageTo3D.js';

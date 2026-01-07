/**
 * 3D Model Generation Tools
 *
 * AI-powered 3D model generation using Meshy AI.
 * Configure your API key in Settings > Integrations before use.
 *
 * Available functions:
 * - textTo3D: Generate a 3D model from a text prompt
 * - imageTo3D: Convert an image to a 3D model
 * - retexture3D: Apply AI textures to an existing 3D model
 * - rig3D: Automatically rig a humanoid model for animation
 * - animate3D: Apply animation to a rigged model
 * - getTask3D: Check the status of a 3D generation task
 *
 * @example
 * ```typescript
 * import { textTo3D, imageTo3D, rig3D, animate3D } from './tools/model3d-generation';
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
 *
 * // Rig a character for animation
 * const rigged = await rig3D({
 *   model_path_or_task_id: fromImage.task_id,
 *   height_meters: 1.8,
 *   wait_for_completion: true
 * });
 *
 * // Apply animation
 * const animated = await animate3D({
 *   rig_task_id: rigged.task_id,
 *   action_id: 'walk_forward',
 *   fps: 30,
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

// Retexturing
export {
  retexture3D,
  type Retexture3DInput,
  type Retexture3DResponse
} from './retexture3D.js';

// Rigging
export {
  rig3D,
  type Rig3DInput,
  type Rig3DResponse
} from './rig3D.js';

// Animation
export {
  animate3D,
  type Animate3DInput,
  type Animate3DResponse
} from './animate3D.js';

// Task status checking
export {
  getTask3D,
  type GetTask3DInput,
  type GetTask3DResponse
} from './getTask3D.js';

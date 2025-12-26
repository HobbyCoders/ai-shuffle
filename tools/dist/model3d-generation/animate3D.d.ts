/**
 * Animate 3D Tool - Meshy AI
 *
 * Apply animation to a rigged 3D model using Meshy AI.
 * Requires a model that has been rigged with rig3D first.
 *
 * Environment Variables:
 *   MESHY_API_KEY - Meshy AI API key (injected by AI Hub at runtime)
 *   MODEL3D_API_KEY - Alternative API key (used if MESHY_API_KEY not set)
 *   GENERATED_MODELS_DIR - Output directory (optional, defaults to ./generated-models)
 *
 * Usage:
 *   import { animate3D } from '/opt/ai-tools/dist/model3d-generation/animate3D.js';
 *
 *   // First rig the model, then animate
 *   const rigged = await rig3D({
 *     model_path_or_task_id: 'character-task-id',
 *     wait_for_completion: true
 *   });
 *
 *   const animated = await animate3D({
 *     rig_task_id: rigged.task_id,
 *     action_id: 'walk_forward',
 *     fps: 30,
 *     wait_for_completion: true
 *   });
 *
 * Note: Meshy has 500+ animation actions available. Common actions include:
 *   - walk_forward, walk_backward
 *   - run_forward
 *   - jump, jump_forward
 *   - idle, idle_breathing
 *   - wave, clap
 *   - punch, kick
 *   - dance_hip_hop, dance_salsa
 */
import { Model3DResponse } from './shared.js';
export interface Animate3DInput {
    /**
     * The task ID from a successful rig3D operation.
     * The model must be rigged before animations can be applied.
     */
    rig_task_id: string;
    /**
     * The animation action ID to apply.
     * Meshy has 500+ animations available.
     *
     * Common action IDs include:
     * - "walk_forward", "walk_backward", "walk_left", "walk_right"
     * - "run_forward", "run_backward"
     * - "jump", "jump_forward", "jump_in_place"
     * - "idle", "idle_breathing", "idle_happy"
     * - "wave", "clap", "bow"
     * - "punch", "kick", "block"
     * - "dance_hip_hop", "dance_salsa", "dance_robot"
     * - "sit", "sit_down", "stand_up"
     * - "crouch", "crawl"
     *
     * @example "walk_forward"
     * @example "dance_hip_hop"
     */
    action_id: string;
    /**
     * Frames per second for the animation output.
     * Higher FPS = smoother animation, larger file size.
     * @default 30
     */
    fps?: 24 | 25 | 30 | 60;
    /**
     * Whether to wait for the animation to complete.
     * - true: Poll until complete and return the animated model (may take 1-3 minutes)
     * - false: Return immediately with task_id for later checking
     * @default false
     */
    wait_for_completion?: boolean;
}
export type Animate3DResponse = Model3DResponse;
/**
 * Apply animation to a rigged 3D model.
 *
 * The animated model is saved to disk as FBX with embedded animation.
 * Animation is asynchronous by default - use wait_for_completion: true
 * to wait for the result, or check the task status with getTask3D.
 *
 * @param input - The animation parameters
 * @returns The animated model URL and file path, or task_id for async mode
 *
 * @example
 * ```typescript
 * // First rig the model
 * const rigged = await rig3D({
 *   model_path_or_task_id: 'character-task-id',
 *   wait_for_completion: true
 * });
 *
 * // Then animate it
 * const animated = await animate3D({
 *   rig_task_id: rigged.task_id,
 *   action_id: 'dance_hip_hop',
 *   fps: 30,
 *   wait_for_completion: true
 * });
 *
 * if (animated.success) {
 *   console.log('Animated model:', animated.model_url);
 *   console.log('Saved to:', animated.file_path);
 * }
 *
 * // Create multiple animations from the same rig
 * const walking = await animate3D({
 *   rig_task_id: rigged.task_id,
 *   action_id: 'walk_forward',
 *   wait_for_completion: true
 * });
 *
 * const jumping = await animate3D({
 *   rig_task_id: rigged.task_id,
 *   action_id: 'jump',
 *   wait_for_completion: true
 * });
 * ```
 */
export declare function animate3D(input: Animate3DInput): Promise<Animate3DResponse>;
export default animate3D;
//# sourceMappingURL=animate3D.d.ts.map
/**
 * Retexture 3D Tool - Meshy AI
 *
 * Apply AI-generated textures to an existing 3D model using Meshy AI.
 * Can retexture models from previous Meshy tasks or from uploaded files.
 *
 * Environment Variables:
 *   MESHY_API_KEY - Meshy AI API key (injected by AI Hub at runtime)
 *   MODEL3D_API_KEY - Alternative API key (used if MESHY_API_KEY not set)
 *   MODEL3D_MODEL - Model to use (optional, defaults to meshy-5)
 *   GENERATED_MODELS_DIR - Output directory (optional, defaults to ./generated-models)
 *
 * Usage:
 *   import { retexture3D } from '/opt/ai-tools/dist/model3d-generation/retexture3D.js';
 *
 *   // Retexture a model from a previous task
 *   const result = await retexture3D({
 *     model_path_or_task_id: 'task-id-from-previous-generation',
 *     style_prompt: 'Medieval stone texture with moss',
 *     wait_for_completion: true
 *   });
 *
 *   // Or use a style image
 *   const result = await retexture3D({
 *     model_path_or_task_id: '/path/to/model.glb',
 *     style_image: '/path/to/style.png',
 *     enable_pbr: true,
 *     wait_for_completion: true
 *   });
 */
import { Model3DResponse } from './shared.js';
export interface Retexture3DInput {
    /**
     * Either a Meshy task ID from a previous generation, or a path to a local model file.
     * - Task ID: Use the task_id from textTo3D or imageTo3D
     * - File path: Path to a .glb, .fbx, or .obj file
     */
    model_path_or_task_id: string;
    /**
     * Text prompt describing the desired texture style.
     * Either style_prompt or style_image is required.
     * Max 600 characters.
     *
     * @example "Weathered bronze with green patina"
     * @example "Cartoon style with bright colors and cel shading"
     */
    style_prompt?: string;
    /**
     * Path to a style reference image.
     * Either style_prompt or style_image is required.
     * The texture will match the style of this image.
     */
    style_image?: string;
    /**
     * AI model to use for retexturing.
     * - "meshy-5": Balanced quality and speed
     * - "meshy-4": Fast generation
     * - "latest": Alias for the latest model
     * @default "meshy-5"
     */
    model?: 'meshy-4' | 'meshy-5' | 'latest';
    /**
     * Whether to enable PBR (Physically Based Rendering) textures.
     * Generates metallic, roughness, and normal maps.
     * @default false
     */
    enable_pbr?: boolean;
    /**
     * Whether to wait for the generation to complete.
     * - true: Poll until complete and return the model (may take 1-3 minutes)
     * - false: Return immediately with task_id for later checking
     * @default false
     */
    wait_for_completion?: boolean;
}
export type Retexture3DResponse = Model3DResponse;
/**
 * Apply AI-generated textures to an existing 3D model.
 *
 * The retextured model is saved to disk and a URL is returned for display.
 * Retexturing is asynchronous by default - use wait_for_completion: true
 * to wait for the result, or check the task status with getTask3D.
 *
 * @param input - The retexturing parameters
 * @returns The retextured model URL and file path, or task_id for async mode
 *
 * @example
 * ```typescript
 * // Retexture with a text prompt
 * const result = await retexture3D({
 *   model_path_or_task_id: 'abc123-task-id',
 *   style_prompt: 'Futuristic chrome with neon accents',
 *   enable_pbr: true,
 *   wait_for_completion: true
 * });
 *
 * // Retexture with a style image
 * const result = await retexture3D({
 *   model_path_or_task_id: '/workspace/model.glb',
 *   style_image: '/workspace/reference.png',
 *   wait_for_completion: true
 * });
 *
 * if (result.success) {
 *   console.log('Model URL:', result.model_url);
 *   console.log('Textures:', result.texture_urls);
 * }
 * ```
 */
export declare function retexture3D(input: Retexture3DInput): Promise<Retexture3DResponse>;
export default retexture3D;
//# sourceMappingURL=retexture3D.d.ts.map
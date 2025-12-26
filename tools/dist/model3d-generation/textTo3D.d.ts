/**
 * Text to 3D Tool - Meshy AI
 *
 * Generate a 3D model from a text prompt using Meshy AI.
 * The API key is provided via environment variable.
 * Models are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   MESHY_API_KEY - Meshy AI API key (injected by AI Hub at runtime)
 *   MODEL3D_API_KEY - Alternative API key (used if MESHY_API_KEY not set)
 *   MODEL3D_MODEL - Model to use (optional, defaults to meshy-6)
 *   GENERATED_MODELS_DIR - Output directory (optional, defaults to ./generated-models)
 *
 * Usage:
 *   import { textTo3D } from '/opt/ai-tools/dist/model3d-generation/textTo3D.js';
 *
 *   // Quick async mode (returns task_id immediately)
 *   const result = await textTo3D({
 *     prompt: 'A medieval castle with stone walls and towers'
 *   });
 *
 *   // Sync mode (waits for completion)
 *   const result = await textTo3D({
 *     prompt: 'A wooden treasure chest',
 *     wait_for_completion: true
 *   });
 *
 *   if (result.success) {
 *     // result.model_url contains the URL to the GLB file
 *     // result.file_path contains the local file path
 *   }
 */
import { Model3DResponse } from './shared.js';
export interface TextTo3DInput {
    /**
     * Text prompt describing the 3D model to generate.
     * Be descriptive! Include details about shape, style, materials, and colors.
     * Max 600 characters.
     *
     * @example "A medieval castle with stone walls and tall towers"
     * @example "A cute cartoon robot with big eyes and chrome body"
     */
    prompt: string;
    /**
     * Art style for the generated model.
     * - "realistic": Photorealistic textures and proportions
     * - "sculpture": Stylized, artistic interpretation
     * @default "realistic"
     */
    art_style?: 'realistic' | 'sculpture';
    /**
     * AI model to use for generation.
     * - "meshy-6": Latest model, best quality (20 credits)
     * - "meshy-5": Balanced quality and speed (5 credits)
     * - "meshy-4": Fast generation (5 credits)
     * - "latest": Alias for the latest model
     * @default "meshy-6"
     */
    model?: 'meshy-4' | 'meshy-5' | 'meshy-6' | 'latest';
    /**
     * Mesh topology type.
     * - "quad": Four-sided polygons, better for subdivision
     * - "triangle": Three-sided polygons, more compatible
     * @default "triangle"
     */
    topology?: 'quad' | 'triangle';
    /**
     * Target polygon count for the mesh.
     * Higher values = more detail, larger file size.
     * Range: 100 to 300,000
     * @default 30000
     */
    target_polycount?: number;
    /**
     * Whether to wait for the generation to complete.
     * - true: Poll until complete and return the model (may take 1-5 minutes)
     * - false: Return immediately with task_id for later checking
     * @default false
     */
    wait_for_completion?: boolean;
}
export type TextTo3DResponse = Model3DResponse;
/**
 * Generate a 3D model from a text prompt using Meshy AI.
 *
 * The model is saved to disk and a URL is returned for display.
 * 3D generation is asynchronous by default - use wait_for_completion: true
 * to wait for the result, or check the task status with getTask3D.
 *
 * @param input - The generation parameters
 * @returns The generated model URL and file path, or task_id for async mode
 *
 * @example
 * ```typescript
 * // Async mode - returns immediately with task_id
 * const result = await textTo3D({
 *   prompt: 'A wooden pirate ship with cannons',
 *   art_style: 'realistic'
 * });
 *
 * if (!result.success && result.task_id) {
 *   console.log('Task started:', result.task_id);
 *   // Check later with getTask3D({ task_id: result.task_id, task_type: 'text-to-3d' })
 * }
 *
 * // Sync mode - waits for completion
 * const result = await textTo3D({
 *   prompt: 'A medieval sword with ornate handle',
 *   model: 'meshy-6',
 *   topology: 'quad',
 *   target_polycount: 50000,
 *   wait_for_completion: true
 * });
 *
 * if (result.success) {
 *   console.log('Model URL:', result.model_url);
 *   console.log('Saved to:', result.file_path);
 * }
 * ```
 */
export declare function textTo3D(input: TextTo3DInput): Promise<TextTo3DResponse>;
export default textTo3D;
//# sourceMappingURL=textTo3D.d.ts.map
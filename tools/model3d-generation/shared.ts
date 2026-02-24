/**
 * Shared types and utilities for 3D model generation tools
 */

import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';

// ============================================================================
// TYPES
// ============================================================================

/**
 * Common response interface for 3D model generation
 */
export interface Model3DResponse {
  success: boolean;
  task_id?: string;
  model_url?: string;
  file_path?: string;
  filename?: string;
  progress?: number;
  status?: 'PENDING' | 'IN_PROGRESS' | 'SUCCEEDED' | 'FAILED' | 'CANCELED';
  model_urls?: {
    glb?: string;
    fbx?: string;
    obj?: string;
    usdz?: string;
  };
  texture_urls?: {
    base_color?: string;
    metallic?: string;
    roughness?: string;
    normal?: string;
  };
  thumbnail_url?: string;
  error?: string;
}

/**
 * Task types for 3D operations
 */
export type TaskType = 'text-to-3d' | 'image-to-3d' | 'retexture' | 'rigging' | 'animation';

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

/**
 * Get the directory for storing generated 3D models.
 * Uses GENERATED_MODELS_DIR env var if set, otherwise creates a 'generated-models'
 * folder in the current working directory.
 */
export function getGeneratedModelsDir(): string {
  if (process.env.GENERATED_MODELS_DIR) {
    return process.env.GENERATED_MODELS_DIR;
  }
  return join(process.cwd(), 'generated-models');
}

/**
 * Sleep for a given number of milliseconds
 */
export function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Read an image file and convert to base64 data URI
 */
export function imageToDataUri(imagePath: string): { dataUri: string; mimeType: string } | { error: string } {
  try {
    if (!existsSync(imagePath)) {
      return { error: `Image file not found: ${imagePath}` };
    }

    const imageBuffer = readFileSync(imagePath);
    const base64 = imageBuffer.toString('base64');

    const ext = imagePath.toLowerCase().split('.').pop();
    const mimeTypes: Record<string, string> = {
      'jpg': 'image/jpeg',
      'jpeg': 'image/jpeg',
      'png': 'image/png',
      'gif': 'image/gif',
      'webp': 'image/webp',
    };

    const mimeType = mimeTypes[ext || ''] || 'image/jpeg';
    const dataUri = `data:${mimeType};base64,${base64}`;
    return { dataUri, mimeType };
  } catch (error) {
    return { error: error instanceof Error ? error.message : 'Failed to read image file' };
  }
}

/**
 * Read a 3D model file and convert to base64 data URI
 */
export function modelToDataUri(modelPath: string): { dataUri: string; mimeType: string } | { error: string } {
  try {
    if (!existsSync(modelPath)) {
      return { error: `Model file not found: ${modelPath}` };
    }

    const modelBuffer = readFileSync(modelPath);
    const base64 = modelBuffer.toString('base64');

    const ext = modelPath.toLowerCase().split('.').pop();
    const mimeTypes: Record<string, string> = {
      'glb': 'model/gltf-binary',
      'gltf': 'model/gltf+json',
      'fbx': 'application/octet-stream',
      'obj': 'text/plain',
      'stl': 'application/sla',
    };

    const mimeType = mimeTypes[ext || ''] || 'application/octet-stream';
    const dataUri = `data:${mimeType};base64,${base64}`;
    return { dataUri, mimeType };
  } catch (error) {
    return { error: error instanceof Error ? error.message : 'Failed to read model file' };
  }
}

/**
 * Download a 3D model from URL and save to disk
 */
export async function downloadAndSaveModel(
  modelUrl: string,
  prefix: string = 'model'
): Promise<{ file_path: string; filename: string } | { error: string }> {
  try {
    const response = await fetch(modelUrl);
    if (!response.ok) {
      return { error: `Failed to download model: HTTP ${response.status}` };
    }

    const modelBuffer = Buffer.from(await response.arrayBuffer());

    const timestamp = Date.now();
    const randomSuffix = crypto.randomUUID().slice(0, 8);

    const urlPath = new URL(modelUrl).pathname;
    const ext = urlPath.split('.').pop() || 'glb';
    const filename = `${prefix}-${timestamp}-${randomSuffix}.${ext}`;

    const outputDir = getGeneratedModelsDir();
    if (!existsSync(outputDir)) {
      mkdirSync(outputDir, { recursive: true });
    }

    const filePath = join(outputDir, filename);
    writeFileSync(filePath, modelBuffer);

    return { file_path: filePath, filename };
  } catch (error) {
    return { error: error instanceof Error ? error.message : 'Failed to download model' };
  }
}

/**
 * Save 3D model response with downloaded file
 */
export async function saveModelFromResponse(
  response: Model3DResponse,
  prefix: string = 'model'
): Promise<Model3DResponse> {
  if (!response.success || !response.model_url) {
    return response;
  }

  const downloadResult = await downloadAndSaveModel(response.model_url, prefix);

  if ('error' in downloadResult) {
    return {
      ...response,
      error: downloadResult.error
    };
  }

  return {
    ...response,
    file_path: downloadResult.file_path,
    filename: downloadResult.filename
  };
}

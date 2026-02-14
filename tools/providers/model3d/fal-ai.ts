/**
 * fal.ai 3D Model Provider
 *
 * Adapter for fal.ai's 3D model generation API.
 * Supports Meshy v6 (via fal) and Hunyuan-3D through a single API key.
 */

import { fal } from '@fal-ai/client';
import { readFileSync, existsSync } from 'fs';
import {
  Model3DProvider,
  Model3DResult,
  Unified3DInput,
  UnifiedImage3DInput,
  ProviderCredentials,
} from '../types.js';
import {
  downloadToFile,
  handleFalError,
  getGeneratedModelsDir,
} from '../shared/fal-helpers.js';

// ============================================================================
// Helpers
// ============================================================================

function imageToDataUri(imagePath: string): string | null {
  try {
    if (!existsSync(imagePath)) return null;
    const buffer = readFileSync(imagePath);
    const ext = imagePath.toLowerCase().split('.').pop();
    const mimeTypes: Record<string, string> = {
      'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
      'gif': 'image/gif', 'webp': 'image/webp',
    };
    const mimeType = mimeTypes[ext || ''] || 'image/jpeg';
    return `data:${mimeType};base64,${buffer.toString('base64')}`;
  } catch {
    return null;
  }
}

/**
 * Extract model URLs from fal.ai 3D generation result.
 * Different models return results in different shapes.
 */
function extractModelUrls(result: any): Record<string, string> {
  const urls: Record<string, string> = {};

  // Meshy via fal returns model_urls or similar
  if (result.model_urls) {
    if (result.model_urls.glb) urls.glb = result.model_urls.glb;
    if (result.model_urls.fbx) urls.fbx = result.model_urls.fbx;
    if (result.model_urls.obj) urls.obj = result.model_urls.obj;
    if (result.model_urls.usdz) urls.usdz = result.model_urls.usdz;
  }

  // Hunyuan-3D returns a glb field directly
  if (result.glb?.url) urls.glb = result.glb.url;
  if (result.model_mesh?.url) urls.glb = result.model_mesh.url;

  // Generic model_url fallback
  if (result.model_url) urls.glb = result.model_url;

  return urls;
}

// ============================================================================
// Provider Implementation
// ============================================================================

export const fal3DProvider: Model3DProvider = {
  id: 'fal-ai',
  name: 'fal.ai (3D)',
  models: [
    {
      id: 'fal-ai/meshy/v6/text-to-3d',
      name: 'Meshy v6 (via fal.ai)',
      description: 'Latest Meshy model through fal.ai - async (2-5 min)',
      capabilities: ['text-to-3d'],
      pricing: { unit: 'generation', price: 0.20 },
    },
    {
      id: 'fal-ai/hunyuan-3d/v2/turbo',
      name: 'Hunyuan-3D v2 Turbo',
      description: 'Fast 3D generation from text or image - ~$0.10/generation',
      capabilities: ['text-to-3d', 'image-to-3d'],
      pricing: { unit: 'generation', price: 0.10 },
    },
  ],

  async textTo3D(
    input: Unified3DInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<Model3DResult> {
    if (!input.prompt || input.prompt.trim().length === 0) {
      return { success: false, error: 'Prompt cannot be empty' };
    }

    try {
      fal.config({ credentials: credentials.apiKey });

      const falInput: Record<string, any> = {
        prompt: input.prompt.trim(),
      };

      // Meshy-specific params
      if (model.includes('meshy')) {
        if (input.art_style) falInput.art_style = input.art_style;
        if (input.topology) falInput.topology = input.topology;
        if (input.target_polycount) falInput.target_polycount = input.target_polycount;
      }

      const result = await fal.subscribe(model, {
        input: falInput,
      }) as any;

      const data = result.data || result;
      const modelUrls = extractModelUrls(data);

      if (Object.keys(modelUrls).length === 0) {
        return { success: false, error: 'No 3D model was generated. Please try a different prompt.' };
      }

      // Download the primary model (GLB preferred)
      const primaryUrl = modelUrls.glb || Object.values(modelUrls)[0];
      const format = modelUrls.glb ? 'glb' : 'fbx';
      const outputDir = getGeneratedModelsDir();
      const downloaded = await downloadToFile(primaryUrl, outputDir, 'fal-3d', format);

      if ('error' in downloaded) {
        // Model generated but download failed — return URLs for manual download
        return {
          success: true,
          model_urls: modelUrls,
          error: `Model generated but failed to save locally: ${downloaded.error}`,
          status: 'SUCCEEDED',
        };
      }

      return {
        success: true,
        model_urls: modelUrls,
        file_path: downloaded.file_path,
        filename: downloaded.filename,
        status: 'SUCCEEDED',
        progress: 100,
        thumbnail_url: data.thumbnail_url || data.thumbnail?.url,
      };

    } catch (error) {
      return { success: false, error: handleFalError(error) };
    }
  },

  async imageTo3D(
    input: UnifiedImage3DInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<Model3DResult> {
    if (!input.image_path) {
      return { success: false, error: 'Image path is required' };
    }

    const dataUri = imageToDataUri(input.image_path);
    if (!dataUri) {
      return { success: false, error: `Image file not found: ${input.image_path}` };
    }

    try {
      fal.config({ credentials: credentials.apiKey });

      // Use image-to-3d variant if available
      const i3dModel = model.includes('text-to-3d')
        ? model.replace('text-to-3d', 'image-to-3d')
        : model;

      const falInput: Record<string, any> = {
        image_url: dataUri,
      };

      if (input.prompt) falInput.prompt = input.prompt.trim();

      const result = await fal.subscribe(i3dModel, {
        input: falInput,
      }) as any;

      const data = result.data || result;
      const modelUrls = extractModelUrls(data);

      if (Object.keys(modelUrls).length === 0) {
        return { success: false, error: 'No 3D model was generated from image.' };
      }

      const primaryUrl = modelUrls.glb || Object.values(modelUrls)[0];
      const format = modelUrls.glb ? 'glb' : 'fbx';
      const outputDir = getGeneratedModelsDir();
      const downloaded = await downloadToFile(primaryUrl, outputDir, 'fal-i2-3d', format);

      if ('error' in downloaded) {
        return {
          success: true,
          model_urls: modelUrls,
          error: `Model generated but failed to save locally: ${downloaded.error}`,
          status: 'SUCCEEDED',
        };
      }

      return {
        success: true,
        model_urls: modelUrls,
        file_path: downloaded.file_path,
        filename: downloaded.filename,
        status: 'SUCCEEDED',
        progress: 100,
        thumbnail_url: data.thumbnail_url || data.thumbnail?.url,
      };

    } catch (error) {
      return { success: false, error: handleFalError(error) };
    }
  },

  async getTask(
    taskId: string,
    taskType: 'text-to-3d' | 'image-to-3d' | 'retexture' | 'rigging' | 'animation',
    credentials: ProviderCredentials
  ): Promise<Model3DResult> {
    // fal.ai handles async internally via subscribe() — tasks complete before returning.
    // This method is here to satisfy the interface but isn't typically needed.
    return {
      success: false,
      task_id: taskId,
      error: 'fal.ai provider handles async tasks internally. Use textTo3D/imageTo3D directly — they wait for completion.',
    };
  },

  async validateCredentials(credentials: ProviderCredentials): Promise<{ valid: boolean; error?: string }> {
    try {
      fal.config({ credentials: credentials.apiKey });
      await fal.queue.status('fal-ai/hunyuan-3d/v2/turbo', {
        requestId: 'test-validation',
      }).catch((e: any) => {
        if (e?.status === 401 || e?.status === 403) {
          throw new Error('Invalid API key');
        }
        return null;
      });
      return { valid: true };
    } catch (error) {
      if (error instanceof Error && error.message.includes('Invalid API key')) {
        return { valid: false, error: 'Invalid fal.ai API key' };
      }
      return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
    }
  }
};

export default fal3DProvider;

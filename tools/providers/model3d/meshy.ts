/**
 * Meshy AI 3D Model Provider
 *
 * Adapter for Meshy's 3D model generation API.
 * Supports text-to-3D, image-to-3D, retexturing, rigging, and animation.
 */

import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';
import {
  Model3DProvider,
  Model3DResult,
  Unified3DInput,
  UnifiedImage3DInput,
  UnifiedRetextureInput,
  UnifiedRiggingInput,
  UnifiedAnimationInput,
  ProviderCredentials,
  ModelInfo,
} from '../types.js';

// ============================================================================
// Constants
// ============================================================================

const MESHY_API_BASE = 'https://api.meshy.ai';
const DEFAULT_POLL_INTERVAL = 5000; // 5 seconds
const MAX_POLL_TIME = 10 * 60 * 1000; // 10 minutes

// ============================================================================
// Helpers
// ============================================================================

function getGeneratedModelsDir(): string {
  if (process.env.GENERATED_MODELS_DIR) {
    return process.env.GENERATED_MODELS_DIR;
  }
  return join(process.cwd(), 'generated-models');
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function imageToBase64DataUri(imagePath: string): { dataUri: string } | { error: string } {
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
    return { dataUri: `data:${mimeType};base64,${base64}` };
  } catch (error) {
    return { error: error instanceof Error ? error.message : 'Failed to read image file' };
  }
}

function handleApiError(response: Response, errorData: any): Model3DResult {
  const errorMsg = errorData?.message || errorData?.error?.message || `HTTP ${response.status}`;

  if (response.status === 400) {
    const errorStr = JSON.stringify(errorData);
    if (errorStr.includes('SAFETY') || errorStr.toLowerCase().includes('blocked')) {
      return {
        success: false,
        error: '3D model generation was blocked by safety filters. Please try a different prompt.'
      };
    }
  }

  if (response.status === 401 || response.status === 403) {
    return { success: false, error: 'Meshy API key is invalid or expired.' };
  }

  if (response.status === 429) {
    return { success: false, error: 'Rate limit exceeded. Please wait a moment and try again.' };
  }

  if (response.status === 402) {
    return { success: false, error: 'Insufficient credits. Please add credits to your Meshy account.' };
  }

  return { success: false, error: `Meshy API error: ${errorMsg}` };
}

async function meshyFetch(
  endpoint: string,
  apiKey: string,
  options: RequestInit = {}
): Promise<Response> {
  const url = endpoint.startsWith('http') ? endpoint : `${MESHY_API_BASE}${endpoint}`;

  return fetch(url, {
    ...options,
    headers: {
      'Authorization': `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
      ...options.headers,
    },
  });
}

async function pollForCompletion(
  taskId: string,
  taskType: 'text-to-3d' | 'image-to-3d' | 'retexture' | 'rigging' | 'animation',
  apiKey: string,
  maxPollTime: number = MAX_POLL_TIME,
  pollInterval: number = DEFAULT_POLL_INTERVAL
): Promise<Model3DResult> {
  const startTime = Date.now();

  // Map task type to API version
  const apiVersions: Record<string, string> = {
    'text-to-3d': 'v2',
    'image-to-3d': 'v1',
    'retexture': 'v1',
    'rigging': 'v1',
    'animation': 'v1',
  };

  const apiVersion = apiVersions[taskType];
  const endpoint = `/openapi/${apiVersion}/${taskType}/${taskId}`;

  while (Date.now() - startTime < maxPollTime) {
    await sleep(pollInterval);

    const response = await meshyFetch(endpoint, apiKey, { method: 'GET' });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      return handleApiError(response, errorData);
    }

    const taskResult = await response.json() as any;

    if (taskResult.status === 'SUCCEEDED') {
      return parseTaskResult(taskResult, taskType);
    }

    if (taskResult.status === 'FAILED') {
      return {
        success: false,
        task_id: taskId,
        status: 'FAILED',
        error: taskResult.error_message || taskResult.message || '3D model generation failed'
      };
    }

    if (taskResult.status === 'CANCELED') {
      return {
        success: false,
        task_id: taskId,
        status: 'CANCELED',
        error: 'Task was canceled'
      };
    }

    // Still in progress, continue polling
  }

  return {
    success: false,
    task_id: taskId,
    status: 'IN_PROGRESS',
    error: '3D model generation timed out. Use getTask to check status later.'
  };
}

function parseTaskResult(taskResult: any, taskType: string): Model3DResult {
  const result: Model3DResult = {
    success: true,
    task_id: taskResult.id,
    status: taskResult.status,
    progress: taskResult.progress || 100,
    thumbnail_url: taskResult.thumbnail_url,
  };

  // Extract model URLs based on task type
  if (taskResult.model_urls || taskResult.model_url) {
    result.model_urls = {};

    if (typeof taskResult.model_urls === 'object') {
      result.model_urls = taskResult.model_urls;
    } else if (taskResult.model_url) {
      // Single URL, determine format from URL or default to GLB
      result.model_urls.glb = taskResult.model_url;
    }

    // Also check for individual format URLs
    if (taskResult.glb_url) result.model_urls!.glb = taskResult.glb_url;
    if (taskResult.fbx_url) result.model_urls!.fbx = taskResult.fbx_url;
    if (taskResult.obj_url) result.model_urls!.obj = taskResult.obj_url;
    if (taskResult.usdz_url) result.model_urls!.usdz = taskResult.usdz_url;
  }

  // Extract texture URLs if available
  if (taskResult.texture_urls) {
    result.texture_urls = taskResult.texture_urls;
  }

  // Store provider-specific metadata
  result.provider_metadata = {
    created_at: taskResult.created_at,
    started_at: taskResult.started_at,
    finished_at: taskResult.finished_at,
    art_style: taskResult.art_style,
    topology: taskResult.topology,
    ai_model: taskResult.ai_model,
  };

  return result;
}

async function downloadAndSaveModel(
  modelUrl: string,
  apiKey: string,
  prefix: string,
  format: string = 'glb'
): Promise<{ file_path: string; filename: string } | { error: string }> {
  try {
    const response = await fetch(modelUrl, {
      headers: {
        'Authorization': `Bearer ${apiKey}`,
      },
    });

    if (!response.ok) {
      return { error: `Failed to download model: HTTP ${response.status}` };
    }

    const modelBuffer = Buffer.from(await response.arrayBuffer());

    const timestamp = Date.now();
    const randomSuffix = Math.random().toString(36).substring(2, 8);
    const filename = `${prefix}-${timestamp}-${randomSuffix}.${format}`;

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

// ============================================================================
// Provider Implementation
// ============================================================================

export const meshyProvider: Model3DProvider = {
  id: 'meshy',
  name: 'Meshy AI',
  models: [
    {
      id: 'meshy-6',
      name: 'Meshy 6 (Latest)',
      description: 'Latest Meshy model with best quality - 20 credits per generation',
      capabilities: ['text-to-3d', 'image-to-3d', '3d-texturing'],
      pricing: { unit: 'generation', price: 0.20 },
    },
    {
      id: 'meshy-5',
      name: 'Meshy 5',
      description: 'Balanced quality and speed - 10 credits per generation',
      capabilities: ['text-to-3d', 'image-to-3d', '3d-texturing'],
      pricing: { unit: 'generation', price: 0.10 },
    },
    {
      id: 'meshy-4',
      name: 'Meshy 4',
      description: 'Fast generation - 5 credits per generation',
      capabilities: ['text-to-3d', 'image-to-3d', '3d-texturing'],
      pricing: { unit: 'generation', price: 0.05 },
    },
  ] as ModelInfo[],

  async textTo3D(
    input: Unified3DInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<Model3DResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.prompt || input.prompt.trim().length === 0) {
      return { success: false, error: 'Prompt cannot be empty' };
    }

    if (input.prompt.length > 600) {
      return { success: false, error: 'Prompt is too long. Maximum 600 characters allowed.' };
    }

    // Validate polycount range
    if (input.target_polycount !== undefined) {
      if (input.target_polycount < 100 || input.target_polycount > 300000) {
        return { success: false, error: 'Target polycount must be between 100 and 300,000.' };
      }
    }

    try {
      // Determine the AI model to use
      const aiModel = input.ai_model || model || 'meshy-6';
      const mode = input.mode || 'preview';

      const requestBody: any = {
        prompt: input.prompt.trim(),
        mode,
        ai_model: aiModel === 'latest' ? 'meshy-6' : aiModel,
      };

      // Add optional parameters
      if (input.art_style) requestBody.art_style = input.art_style;
      if (input.topology) requestBody.topology = input.topology;
      if (input.target_polycount) requestBody.target_polycount = input.target_polycount;
      if (input.preview_task_id && mode === 'refine') {
        requestBody.preview_task_id = input.preview_task_id;
      }

      const response = await meshyFetch('/openapi/v2/text-to-3d', apiKey, {
        method: 'POST',
        body: JSON.stringify(requestBody),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const createResult = await response.json() as any;
      const taskId = createResult.result;

      if (!taskId) {
        return { success: false, error: 'Failed to start 3D generation: no task ID returned.' };
      }

      // Poll for completion
      const pollResult = await pollForCompletion(taskId, 'text-to-3d', apiKey);

      if (!pollResult.success) {
        return pollResult;
      }

      // Download and save the primary model (GLB)
      if (pollResult.model_urls?.glb) {
        const saveResult = await downloadAndSaveModel(pollResult.model_urls.glb, apiKey, 'text-to-3d');
        if ('error' in saveResult) {
          // Model generated but failed to save - still return success with URLs
          pollResult.error = `Model generated but failed to save locally: ${saveResult.error}`;
        } else {
          pollResult.file_path = saveResult.file_path;
          pollResult.filename = saveResult.filename;
        }
      }

      return pollResult;

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async imageTo3D(
    input: UnifiedImage3DInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<Model3DResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.image_path) {
      return { success: false, error: 'Image path is required' };
    }

    // Convert image to base64 data URI
    const imageData = imageToBase64DataUri(input.image_path);
    if ('error' in imageData) {
      return { success: false, error: imageData.error };
    }

    try {
      const aiModel = input.ai_model || model || 'meshy-6';

      const requestBody: any = {
        image_url: imageData.dataUri,
        ai_model: aiModel === 'latest' ? 'meshy-6' : aiModel,
      };

      // Add optional parameters
      if (input.topology) requestBody.topology = input.topology;
      if (input.target_polycount) requestBody.target_polycount = input.target_polycount;
      if (input.symmetry_mode) requestBody.symmetry_mode = input.symmetry_mode;
      if (input.should_remesh !== undefined) requestBody.should_remesh = input.should_remesh;
      if (input.should_texture !== undefined) requestBody.should_texture = input.should_texture;
      if (input.enable_pbr !== undefined) requestBody.enable_pbr = input.enable_pbr;
      if (input.pose_mode) requestBody.pose_mode = input.pose_mode;
      if (input.texture_prompt) requestBody.texture_prompt = input.texture_prompt;

      const response = await meshyFetch('/openapi/v1/image-to-3d', apiKey, {
        method: 'POST',
        body: JSON.stringify(requestBody),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const createResult = await response.json() as any;
      const taskId = createResult.result;

      if (!taskId) {
        return { success: false, error: 'Failed to start image-to-3D generation: no task ID returned.' };
      }

      // Poll for completion
      const pollResult = await pollForCompletion(taskId, 'image-to-3d', apiKey);

      if (!pollResult.success) {
        return pollResult;
      }

      // Download and save the primary model (GLB)
      if (pollResult.model_urls?.glb) {
        const saveResult = await downloadAndSaveModel(pollResult.model_urls.glb, apiKey, 'image-to-3d');
        if ('error' in saveResult) {
          pollResult.error = `Model generated but failed to save locally: ${saveResult.error}`;
        } else {
          pollResult.file_path = saveResult.file_path;
          pollResult.filename = saveResult.filename;
        }
      }

      return pollResult;

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async retexture(
    input: UnifiedRetextureInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<Model3DResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.model_source) {
      return { success: false, error: 'Model source (task_id or model_url) is required' };
    }

    if (!input.style_prompt && !input.style_image_url) {
      return { success: false, error: 'Either style_prompt or style_image_url is required' };
    }

    try {
      const aiModel = input.ai_model || model || 'meshy-5';

      const requestBody: any = {
        ai_model: aiModel === 'latest' ? 'meshy-5' : aiModel,
      };

      // Determine if model_source is a task_id or URL
      if (input.model_source.startsWith('http')) {
        requestBody.model_url = input.model_source;
      } else {
        requestBody.input_task_id = input.model_source;
      }

      // Add style parameters
      if (input.style_prompt) requestBody.text_style_prompt = input.style_prompt;
      if (input.style_image_url) requestBody.image_style_url = input.style_image_url;
      if (input.enable_original_uv !== undefined) requestBody.enable_original_uv = input.enable_original_uv;
      if (input.enable_pbr !== undefined) requestBody.enable_pbr = input.enable_pbr;

      const response = await meshyFetch('/openapi/v1/retexture', apiKey, {
        method: 'POST',
        body: JSON.stringify(requestBody),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const createResult = await response.json() as any;
      const taskId = createResult.result;

      if (!taskId) {
        return { success: false, error: 'Failed to start retexturing: no task ID returned.' };
      }

      // Poll for completion
      const pollResult = await pollForCompletion(taskId, 'retexture', apiKey);

      if (!pollResult.success) {
        return pollResult;
      }

      // Download and save the primary model (GLB)
      if (pollResult.model_urls?.glb) {
        const saveResult = await downloadAndSaveModel(pollResult.model_urls.glb, apiKey, 'retexture');
        if ('error' in saveResult) {
          pollResult.error = `Model generated but failed to save locally: ${saveResult.error}`;
        } else {
          pollResult.file_path = saveResult.file_path;
          pollResult.filename = saveResult.filename;
        }
      }

      return pollResult;

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async rig(
    input: UnifiedRiggingInput,
    credentials: ProviderCredentials
  ): Promise<Model3DResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.model_source) {
      return { success: false, error: 'Model source (task_id or model_url) is required' };
    }

    try {
      const requestBody: any = {};

      // Determine if model_source is a task_id or URL
      if (input.model_source.startsWith('http')) {
        requestBody.model_url = input.model_source;
      } else {
        requestBody.input_task_id = input.model_source;
      }

      // Add optional parameters
      if (input.height_meters !== undefined) requestBody.height_meters = input.height_meters;
      if (input.texture_image_url) requestBody.texture_image_url = input.texture_image_url;

      const response = await meshyFetch('/openapi/v1/rigging', apiKey, {
        method: 'POST',
        body: JSON.stringify(requestBody),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const createResult = await response.json() as any;
      const taskId = createResult.result;

      if (!taskId) {
        return { success: false, error: 'Failed to start rigging: no task ID returned.' };
      }

      // Poll for completion
      const pollResult = await pollForCompletion(taskId, 'rigging', apiKey);

      if (!pollResult.success) {
        return pollResult;
      }

      // Download and save the rigged model (FBX is common for rigged models)
      const modelUrl = pollResult.model_urls?.fbx || pollResult.model_urls?.glb;
      const format = pollResult.model_urls?.fbx ? 'fbx' : 'glb';

      if (modelUrl) {
        const saveResult = await downloadAndSaveModel(modelUrl, apiKey, 'rigged', format);
        if ('error' in saveResult) {
          pollResult.error = `Model rigged but failed to save locally: ${saveResult.error}`;
        } else {
          pollResult.file_path = saveResult.file_path;
          pollResult.filename = saveResult.filename;
        }
      }

      return pollResult;

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async animate(
    input: UnifiedAnimationInput,
    credentials: ProviderCredentials
  ): Promise<Model3DResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.rig_task_id) {
      return { success: false, error: 'Rig task ID is required' };
    }

    if (!input.action_id) {
      return { success: false, error: 'Animation action ID is required' };
    }

    try {
      const requestBody: any = {
        rig_task_id: input.rig_task_id,
        action_id: input.action_id,
      };

      // Add optional parameters
      if (input.fps) requestBody.fps = input.fps;
      if (input.operation_type) requestBody.operation_type = input.operation_type;

      const response = await meshyFetch('/openapi/v1/animations', apiKey, {
        method: 'POST',
        body: JSON.stringify(requestBody),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const createResult = await response.json() as any;
      const taskId = createResult.result;

      if (!taskId) {
        return { success: false, error: 'Failed to start animation: no task ID returned.' };
      }

      // Poll for completion
      const pollResult = await pollForCompletion(taskId, 'animation', apiKey);

      if (!pollResult.success) {
        return pollResult;
      }

      // Download and save the animated model
      const modelUrl = pollResult.model_urls?.fbx || pollResult.model_urls?.glb;
      const format = pollResult.model_urls?.fbx ? 'fbx' : 'glb';

      if (modelUrl) {
        const saveResult = await downloadAndSaveModel(modelUrl, apiKey, 'animated', format);
        if ('error' in saveResult) {
          pollResult.error = `Animation created but failed to save locally: ${saveResult.error}`;
        } else {
          pollResult.file_path = saveResult.file_path;
          pollResult.filename = saveResult.filename;
        }
      }

      return pollResult;

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async getTask(
    taskId: string,
    taskType: 'text-to-3d' | 'image-to-3d' | 'retexture' | 'rigging' | 'animation',
    credentials: ProviderCredentials
  ): Promise<Model3DResult> {
    const apiKey = credentials.apiKey;

    if (!taskId) {
      return { success: false, error: 'Task ID is required' };
    }

    try {
      // Map task type to API version
      const apiVersions: Record<string, string> = {
        'text-to-3d': 'v2',
        'image-to-3d': 'v1',
        'retexture': 'v1',
        'rigging': 'v1',
        'animation': 'v1',
      };

      const apiVersion = apiVersions[taskType];
      const endpoint = `/openapi/${apiVersion}/${taskType}/${taskId}`;

      const response = await meshyFetch(endpoint, apiKey, { method: 'GET' });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const taskResult = await response.json() as any;
      return parseTaskResult(taskResult, taskType);

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async validateCredentials(credentials: ProviderCredentials): Promise<{ valid: boolean; error?: string }> {
    try {
      // Try to list text-to-3d tasks as a validation method
      const response = await meshyFetch('/openapi/v2/text-to-3d?pageSize=1', credentials.apiKey, {
        method: 'GET',
      });

      if (response.status === 401 || response.status === 403) {
        return { valid: false, error: 'Invalid Meshy API key' };
      }

      if (response.status === 402) {
        // API key is valid but no credits - still valid
        return { valid: true };
      }

      return { valid: response.ok };
    } catch (error) {
      return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
    }
  }
};

export default meshyProvider;
